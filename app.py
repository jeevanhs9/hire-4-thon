"""
DAM - Disaster Alert Monitor
Flask Backend: API endpoints, SQLite DB, keyword-based ML classifier
"""

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from functools import wraps
import sqlite3
import datetime
import hashlib
import os
import re
from urllib.parse import quote
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SECRET_KEY = hashlib.sha256(f"{BASE_DIR}|dam-session".encode()).hexdigest()
app.config.update(
    SECRET_KEY=os.environ.get("DAM_SECRET_KEY", os.environ.get("FLASK_SECRET_KEY", DEFAULT_SECRET_KEY)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("DAM_SESSION_SECURE", "0") == "1",
)
DB_PATH = os.environ.get("DAM_DB_PATH", os.path.join(BASE_DIR, "dam.db"))
INDIA_DEMO_SEED_KEY = "india_demo_pack_v1"
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEMO_IMAGE_DIR = os.path.join(STATIC_DIR, "demo_images")
DEMO_IMAGE_EXTENSIONS = (".avif", ".webp", ".jpg", ".jpeg", ".png")
ADMIN_EMAIL = os.environ.get("DAM_ADMIN_EMAIL", "admin@dam.local").strip().lower()
ADMIN_PHONE = os.environ.get("DAM_ADMIN_PHONE", "9999999999").strip()
ADMIN_PASSWORD = os.environ.get("DAM_ADMIN_PASSWORD", "admin123")
RESET_ADMIN_PASSWORD = os.environ.get("DAM_RESET_ADMIN_PASSWORD", "0") == "1"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
THEME_IMAGE_KEYWORDS = {
    "heatwave": ["heat", "heatwave", "sun", "temperature"],
    "landslide": ["landslide", "slide", "debris", "mud"],
    "cyclone": ["cyclone", "storm", "hurricane", "amphan", "wind"],
    "flood": ["flood", "water", "rescue", "boat", "river", "overflow"],
    "fire": ["fire", "wildfire", "smoke", "burn", "flame"],
    "cloudburst": ["cloudburst", "flood", "landslide", "debris", "water"],
    "avalanche": ["avalanche", "snow", "glide"],
    "river-flood": ["flood", "river", "water", "boat", "rescue"],
    "storm-surge": ["storm", "surge", "cyclone", "flood", "water"],
    "dust-storm": ["dust", "sand", "storm", "haboob"],
    "lake-overflow": ["overflow", "flood", "water", "lake"],
    "cyclone-recovery": ["cyclone", "storm", "damage", "recovery", "wind"],
}
REMOTE_THEME_IMAGE_URLS = {
    "dust-storm": [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Phoenix%20Dust%20Storm%20%281%29%20%285999766358%29.jpg",
    ],
    "avalanche": [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Snow%20blower%20v%20avalanche%20%2832281345297%29.jpg",
    ],
    "fire": [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Wildfire%20Smoke%20-%2052959202838.jpg",
    ],
    "cyclone": [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Hurricane%20damage%20%2823538371118%29.jpg",
    ],
    "cyclone-recovery": [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Hurricane%20damage%20%2823538371118%29.jpg",
    ],
    "landslide": [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Wikipedia%20Landslide.jpg",
    ],
    "cloudburst": [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Wikipedia%20Landslide.jpg",
    ],
}

# ---------------------------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            email    TEXT UNIQUE NOT NULL,
            phone    TEXT,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tweets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            text           TEXT NOT NULL,
            classification TEXT NOT NULL,
            intensity      TEXT NOT NULL,
            location       TEXT,
            lat            REAL,
            lng            REAL,
            timestamp      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT, email TEXT, phone TEXT,
            country   TEXT, state TEXT, city TEXT, pincode TEXT,
            details   TEXT,
            image_data TEXT,
            status    TEXT DEFAULT 'pending',
            admin_notes TEXT,
            reviewer_email TEXT,
            reviewed_at TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    user_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "is_admin" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    conn.execute("UPDATE users SET is_admin = 0 WHERE is_admin IS NULL")

    submission_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(submissions)").fetchall()
    }
    if "image_data" not in submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN image_data TEXT")
    status_column_added = False
    if "status" not in submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN status TEXT DEFAULT 'pending'")
        status_column_added = True
    if "admin_notes" not in submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN admin_notes TEXT")
    if "reviewer_email" not in submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN reviewer_email TEXT")
    if "reviewed_at" not in submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN reviewed_at TEXT")

    if status_column_added:
        conn.execute("UPDATE submissions SET status = 'accepted'")
    else:
        conn.execute("""
            UPDATE submissions
            SET status = 'accepted'
            WHERE status IS NULL OR status = ''
        """)

    admin_user = conn.execute(
        "SELECT id FROM users WHERE email=?",
        (ADMIN_EMAIL,)
    ).fetchone()
    if admin_user:
        conn.execute("UPDATE users SET is_admin = 1 WHERE email = ?", (ADMIN_EMAIL,))
        if RESET_ADMIN_PASSWORD:
            conn.execute(
                "UPDATE users SET password = ? WHERE email = ?",
                (hash_password(ADMIN_PASSWORD), ADMIN_EMAIL)
            )
    else:
        conn.execute(
            "INSERT INTO users (email, phone, password, is_admin) VALUES (?,?,?,?)",
            (ADMIN_EMAIL, ADMIN_PHONE, hash_password(ADMIN_PASSWORD), 1)
        )
    seed_demo_records(conn)
    refresh_seeded_demo_images(conn)
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# KEYWORD-BASED CLASSIFIER
# ---------------------------------------------------------------------------

HIGH_KEYWORDS = [
    "earthquake","tsunami","hurricane","tornado","explosion","flood","wildfire",
    "volcano","avalanche","cyclone","disaster","catastrophe","emergency","evacuate",
    "casualties","deaths","collapsed","destroyed","devastating","critical","urgent",
    "sos","trapped","fire","blaze","drowning","landslide","storm","blast","rescue"
]
MEDIUM_KEYWORDS = [
    "warning","alert","damage","injured","accident","crash","leak","power outage",
    "blackout","missing","shelter","evacuation","disruption","hazard","risk",
    "threat","caution","unsafe","debris","heavy rain","strong wind","fog","hail"
]
SAFE_KEYWORDS = [
    "safe", "contained", "stable", "restored", "normal", "reopened",
    "recovery", "under control", "out of danger", "cleared"
]
DISASTER_TYPE_KEYWORDS = {
    "Flood": ["flood", "flooding", "waterlogging", "overflow", "submerged", "river rise"],
    "Cyclone": ["cyclone", "storm surge", "hurricane", "gale", "wind damage"],
    "Fire": ["fire", "wildfire", "blaze", "smoke", "burning"],
    "Landslide": ["landslide", "slope failure", "mudslide", "debris flow"],
    "Heatwave": ["heatwave", "heat wave", "heat stress", "temperature spike"],
    "Avalanche": ["avalanche", "snow slide", "snowfall warning"],
    "Earthquake": ["earthquake", "tremor", "seismic"],
    "Storm": ["storm", "heavy rain", "strong wind", "hail"],
}

CITY_COORDS = {
    "mumbai":    ("Mumbai, India",      19.0760, 72.8777),
    "delhi":     ("Delhi, India",       28.6139, 77.2090),
    "new delhi": ("New Delhi, India",   28.6139, 77.2090),
    "bangalore": ("Bangalore, India",   12.9716, 77.5946),
    "bengaluru": ("Bengaluru, India",   12.9716, 77.5946),
    "chennai":   ("Chennai, India",     13.0827, 80.2707),
    "kolkata":   ("Kolkata, India",     22.5726, 88.3639),
    "hyderabad": ("Hyderabad, India",   17.3850, 78.4867),
    "pune":      ("Pune, India",        18.5204, 73.8567),
    "jaipur":    ("Jaipur, India",      26.9124, 75.7873),
    "ahmedabad": ("Ahmedabad, India",   23.0225, 72.5714),
    "surat":     ("Surat, India",       21.1702, 72.8311),
    "lucknow":   ("Lucknow, India",     26.8467, 80.9462),
    "guwahati":  ("Guwahati, India",    26.1445, 91.7362),
    "bhubaneswar": ("Bhubaneswar, India", 20.2961, 85.8245),
    "kochi":     ("Kochi, India",        9.9312, 76.2673),
    "dehradun":  ("Dehradun, India",    30.3165, 78.0322),
    "srinagar":  ("Srinagar, India",    34.0837, 74.7973),
    "patna":     ("Patna, India",       25.5941, 85.1376),
    "visakhapatnam": ("Visakhapatnam, India", 17.6868, 83.2185),
    "new york":  ("New York, USA",      40.7128, -74.0060),
    "london":    ("London, UK",         51.5074, -0.1278),
    "paris":     ("Paris, France",      48.8566,  2.3522),
    "tokyo":     ("Tokyo, Japan",       35.6762,139.6503),
    "sydney":    ("Sydney, Australia", -33.8688,151.2093),
}

DEMO_INDIA_CASES = [
    {
        "city": "New Delhi",
        "state": "Delhi",
        "country": "India",
        "pincode": "110001",
        "lat": 28.6139,
        "lng": 77.2090,
        "intensity": "medium",
        "theme": "Heatwave",
        "reporter": "Aarav Mehta",
        "email": "delhi.demo@dam.local",
        "phone": "9000000101",
        "details": (
            "Incident: A strong heatwave has pushed afternoon temperatures across New Delhi into the severe range. "
            "Impact: Outdoor workers, elderly residents, and children are facing dehydration risk and public health teams are extending cooling support. "
            "Current situation: The situation is under control, but it still needs watching. "
            "Advisory: Avoid afternoon travel, carry water, and follow local health alerts until temperatures drop."
        ),
        "tweets": [
            "Heatwave conditions continue across New Delhi with public cooling stations operating in the highest-risk zones.",
            "Emergency health teams in New Delhi are advising reduced outdoor movement as heat stress cases rise."
        ]
    },
    {
        "city": "Guwahati",
        "state": "Assam",
        "country": "India",
        "pincode": "781001",
        "lat": 26.1445,
        "lng": 91.7362,
        "intensity": "high",
        "theme": "Landslide",
        "reporter": "Ananya Das",
        "email": "guwahati.demo@dam.local",
        "phone": "9000000102",
        "details": (
            "Incident: Continuous rainfall has triggered landslides along hillside settlements in Guwahati. "
            "Impact: Access roads are blocked, a few homes have structural cracks, and temporary shelters are active for affected families. "
            "Current situation: The situation is not under control. "
            "Advisory: People living close to unstable slopes should remain evacuated until debris removal and rainfall monitoring are complete."
        ),
        "tweets": [
            "Heavy rain in Guwahati triggered landslides and blocked access roads near hillside settlements.",
            "District crews in Guwahati are moving families to safe shelters while slope instability remains high."
        ]
    },
    {
        "city": "Bhubaneswar",
        "state": "Odisha",
        "country": "India",
        "pincode": "751001",
        "lat": 20.2961,
        "lng": 85.8245,
        "intensity": "medium",
        "theme": "Cyclone",
        "reporter": "Priya Nayak",
        "email": "bhubaneswar.demo@dam.local",
        "phone": "9000000103",
        "details": (
            "Incident: Cyclonic winds and heavy rain have damaged utility lines and uprooted roadside trees in Bhubaneswar. "
            "Impact: Traffic is moving slowly and a few low-lying neighborhoods reported water entry overnight. "
            "Current situation: The situation is under control, but it still needs watching. "
            "Advisory: Residents should avoid loose structures and follow coastal weather updates until the wind field weakens."
        ),
        "tweets": [
            "Cyclone-related winds in Bhubaneswar caused tree falls and temporary power disruption in multiple wards.",
            "Municipal teams in Bhubaneswar are clearing roads while rain bands continue over the city."
        ]
    },
    {
        "city": "Kochi",
        "state": "Kerala",
        "country": "India",
        "pincode": "682001",
        "lat": 9.9312,
        "lng": 76.2673,
        "intensity": "medium",
        "theme": "Flood",
        "reporter": "Nikhil Varma",
        "email": "kochi.demo@dam.local",
        "phone": "9000000104",
        "details": (
            "Incident: Intense monsoon rain has caused street flooding and waterlogging in parts of Kochi. "
            "Impact: Commuter traffic is delayed, drainage pumps are active, and local shops in low-lying lanes have reported water entry. "
            "Current situation: The situation is under control, but it still needs watching. "
            "Advisory: Avoid submerged roads, stay alert to drainage backflow, and wait for official clearance before reopening flooded spaces."
        ),
        "tweets": [
            "Waterlogging is affecting low-lying neighborhoods in Kochi after hours of intense monsoon rain.",
            "Civic teams in Kochi have deployed pumps and are warning drivers to avoid flooded underpasses."
        ]
    },
    {
        "city": "Dehradun",
        "state": "Uttarakhand",
        "country": "India",
        "pincode": "248001",
        "lat": 30.3165,
        "lng": 78.0322,
        "intensity": "high",
        "theme": "Cloudburst",
        "reporter": "Ritika Bisht",
        "email": "dehradun.demo@dam.local",
        "phone": "9000000105",
        "details": (
            "Incident: A cloudburst in the hill belt above Dehradun triggered sudden runoff and slope failure. "
            "Impact: Debris has reached approach roads, water channels are running fast, and rescue teams are checking isolated hamlets. "
            "Current situation: The situation is not under control. "
            "Advisory: Travel in the affected hill corridor should stay restricted until terrain stability and runoff levels improve."
        ),
        "tweets": [
            "Cloudburst impact near Dehradun has sent debris across hill roads and increased runoff in nearby channels.",
            "Relief teams in Dehradun are checking isolated settlements while slope movement remains active."
        ]
    },
    {
        "city": "Srinagar",
        "state": "Jammu and Kashmir",
        "country": "India",
        "pincode": "190001",
        "lat": 34.0837,
        "lng": 74.7973,
        "intensity": "medium",
        "theme": "Avalanche",
        "reporter": "Irfan Bhat",
        "email": "srinagar.demo@dam.local",
        "phone": "9000000106",
        "details": (
            "Incident: Snowfall in the higher reaches around Srinagar has raised avalanche risk on connecting mountain routes. "
            "Impact: Access to a few remote stretches is controlled and travelers are being asked to delay movement toward exposed passes. "
            "Current situation: The situation is under control, but it still needs watching. "
            "Advisory: People should avoid steep snow-loaded slopes and follow route advisories from the local control room."
        ),
        "tweets": [
            "Avalanche warning remains active around the mountain approaches connected to Srinagar after fresh snowfall.",
            "Traffic control near Srinagar is restricting movement toward higher-risk snow corridors."
        ]
    },
    {
        "city": "Patna",
        "state": "Bihar",
        "country": "India",
        "pincode": "800001",
        "lat": 25.5941,
        "lng": 85.1376,
        "intensity": "high",
        "theme": "River Flood",
        "reporter": "Sneha Kumar",
        "email": "patna.demo@dam.local",
        "phone": "9000000107",
        "details": (
            "Incident: River levels near Patna have risen sharply and water has started entering vulnerable embankment-side settlements. "
            "Impact: Local evacuation support is active, sanitation risk is increasing, and movement through low-lying roads is limited. "
            "Current situation: The situation is not under control. "
            "Advisory: Residents near the river edge should stay in safe shelters and avoid returning until water levels stabilize."
        ),
        "tweets": [
            "Floodwater near Patna is entering low-lying settlements as river levels continue to rise.",
            "Response teams in Patna are moving families from embankment-side zones to temporary relief shelters."
        ]
    },
    {
        "city": "Kolkata",
        "state": "West Bengal",
        "country": "India",
        "pincode": "700001",
        "lat": 22.5726,
        "lng": 88.3639,
        "intensity": "medium",
        "theme": "Storm Surge",
        "reporter": "Moumita Sen",
        "email": "kolkata.demo@dam.local",
        "phone": "9000000108",
        "details": (
            "Incident: Strong rain bands and upstream pressure have stressed drainage and embankment zones linked to Kolkata. "
            "Impact: Water accumulation has slowed city movement and field teams are monitoring weak points near canal-side stretches. "
            "Current situation: The situation is under control, but it still needs watching. "
            "Advisory: Residents should avoid waterlogged shortcuts and follow civic control-room updates during the next rainfall cycle."
        ),
        "tweets": [
            "Drainage pressure is building in parts of Kolkata as rain bands continue over the city and connected embankment zones.",
            "Monitoring teams in Kolkata are checking vulnerable canal-side stretches and advising caution in waterlogged areas."
        ]
    },
    {
        "city": "Jaipur",
        "state": "Rajasthan",
        "country": "India",
        "pincode": "302001",
        "lat": 26.9124,
        "lng": 75.7873,
        "intensity": "low",
        "theme": "Dust Storm",
        "reporter": "Karan Singh",
        "email": "jaipur.demo@dam.local",
        "phone": "9000000109",
        "details": (
            "Incident: A dust storm passed across Jaipur and briefly affected visibility and local traffic flow. "
            "Impact: Some branches fell and utility teams handled minor cleanup across exposed corridors. "
            "Current situation: The situation is out of danger. "
            "Advisory: Residents can resume normal activity, but should remain careful around any temporary roadside debris."
        ),
        "tweets": [
            "Cleanup after the dust storm in Jaipur is nearly complete and major routes are open again.",
            "Local teams in Jaipur report the dust storm impact is now under control with only minor debris removal pending."
        ]
    },
    {
        "city": "Hyderabad",
        "state": "Telangana",
        "country": "India",
        "pincode": "500001",
        "lat": 17.3850,
        "lng": 78.4867,
        "intensity": "medium",
        "theme": "Lake Overflow",
        "reporter": "Meghana Rao",
        "email": "hyderabad.demo@dam.local",
        "phone": "9000000110",
        "details": (
            "Incident: Water from a swollen lake system has entered feeder drains and low streets in Hyderabad after sustained rain. "
            "Impact: A few colonies are facing water entry and civic teams are using pumps and barricades in the most affected lanes. "
            "Current situation: The situation is under control, but it still needs watching. "
            "Advisory: Commuters should avoid barricaded pockets and monitor rain intensity before travelling through low-lying neighborhoods."
        ),
        "tweets": [
            "Lake overflow in Hyderabad is affecting feeder drains and creating water entry in nearby low streets.",
            "Pump teams in Hyderabad are working in affected colonies while more rain is expected overnight."
        ]
    },
    {
        "city": "Visakhapatnam",
        "state": "Andhra Pradesh",
        "country": "India",
        "pincode": "530001",
        "lat": 17.6868,
        "lng": 83.2185,
        "intensity": "low",
        "theme": "Cyclone Recovery",
        "reporter": "Sai Kiran",
        "email": "vizag.demo@dam.local",
        "phone": "9000000111",
        "details": (
            "Incident: Coastal neighborhoods in Visakhapatnam saw wind damage and flooding during the cyclone phase earlier in the week. "
            "Impact: Restoration teams have reopened most roads and only scattered cleanup work remains in shoreline pockets. "
            "Current situation: The situation is out of danger. "
            "Advisory: Residents can resume normal activity while continuing to report any loose infrastructure or standing water."
        ),
        "tweets": [
            "Cyclone recovery in Visakhapatnam is moving steadily with most transport links restored.",
            "Field teams in Visakhapatnam say the affected coastline is now out of danger and cleanup is in the final stage."
        ]
    }
]

def normalize_text(value):
    return " ".join(str(value or "").strip().lower().split())

def legacy_hash_password(password):
    return hashlib.sha256(str(password or "").encode()).hexdigest()

def hash_password(password):
    return generate_password_hash(str(password or ""))

def verify_password(password, stored_hash):
    stored_hash = str(stored_hash or "").strip()
    if not stored_hash:
        return False
    if stored_hash.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored_hash, str(password or ""))
    if re.fullmatch(r"[a-f0-9]{64}", stored_hash):
        return legacy_hash_password(password) == stored_hash
    return False

def maybe_upgrade_password(conn, user_id, password, stored_hash):
    if stored_hash and re.fullmatch(r"[a-f0-9]{64}", str(stored_hash).strip()):
        conn.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (hash_password(password), user_id)
        )
        conn.commit()

def find_matching_terms(text, terms):
    normalized = normalize_text(text)
    return [term for term in terms if term in normalized]

def detect_disaster_type(text):
    normalized = normalize_text(text)
    best_type = "General Disaster Update"
    best_score = 0
    matched_terms = []

    for disaster_type, terms in DISASTER_TYPE_KEYWORDS.items():
        hits = [term for term in terms if term in normalized]
        if not hits:
            continue
        score = sum(2 if " " in term else 1 for term in hits)
        if score > best_score:
            best_type = disaster_type
            best_score = score
        matched_terms.extend(hits)

    return best_type, sorted(set(matched_terms))

def analyze_tweet(text):
    normalized = normalize_text(text)
    high_hits = find_matching_terms(normalized, HIGH_KEYWORDS)
    medium_hits = find_matching_terms(normalized, MEDIUM_KEYWORDS)
    safe_hits = find_matching_terms(normalized, SAFE_KEYWORDS)
    disaster_type, disaster_hits = detect_disaster_type(normalized)

    severity_score = len(high_hits) * 2 + len(medium_hits)
    if high_hits:
        intensity = "high"
        color = "red"
    elif medium_hits:
        intensity = "medium"
        color = "yellow"
    elif safe_hits:
        intensity = "low"
        color = "green"
    else:
        intensity = "low"
        color = "green"

    classification = classification_for_intensity(intensity)
    confidence = min(0.35 + (severity_score * 0.1) + (0.05 * len(disaster_hits)), 0.98)

    return {
        "classification": classification,
        "intensity": intensity,
        "color": color,
        "disaster_type": disaster_type,
        "matched_terms": sorted(set(high_hits + medium_hits + safe_hits + disaster_hits)),
        "confidence": round(confidence, 2)
    }

def extract_location(text):
    normalized = normalize_text(text)
    for key in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if key in normalized:
            return CITY_COORDS[key]
    return ("Unknown", 20.5937, 78.9629)

def lookup_city_coordinates(city):
    normalized_city = normalize_text(city)
    if not normalized_city:
        return None
    for key in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if normalized_city == key or key in normalized_city or normalized_city in key:
            return CITY_COORDS[key]
    return None

def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def normalize_submission_status(value):
    status = str(value or "").strip().lower()
    if status in {"accepted", "rejected"}:
        return status
    return "pending"

def serialize_submission(row):
    item = dict(row)
    item["status"] = normalize_submission_status(item.get("status"))
    item["admin_notes"] = item.get("admin_notes") or ""
    item["reviewer_email"] = item.get("reviewer_email") or ""
    item["reviewed_at"] = item.get("reviewed_at") or ""
    item["image_data"] = item.get("image_data") or ""
    return item

def classification_for_intensity(intensity):
    if intensity == "high":
        return "Disaster"
    if intensity == "medium":
        return "Warning"
    return "Safe"

def demo_theme_for_city_state(city, state):
    manual_overrides = {
        ("bengaluru", "karnataka"): "Flood",
        ("bangalore", "karnataka"): "Flood",
        ("chennai", "tamil-nadu"): "Cyclone Recovery",
        ("mumbai", "maharashtra"): "Fire",
    }
    key = (slugify_label(city), slugify_label(state))
    if key in manual_overrides:
        return manual_overrides[key]

    for case in DEMO_INDIA_CASES:
        case_key = (slugify_label(case.get("city")), slugify_label(case.get("state")))
        if key == case_key:
            return case.get("theme", "")
    return ""

def slugify_label(value):
    text = "".join(
        ch.lower() if str(ch).isalnum() else "-"
        for ch in str(value or "")
    )
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")

def available_demo_images():
    if not os.path.isdir(DEMO_IMAGE_DIR):
        return []
    files = []
    for name in os.listdir(DEMO_IMAGE_DIR):
        lower_name = name.lower()
        if lower_name.endswith(DEMO_IMAGE_EXTENSIONS):
            files.append(name)
    return sorted(files)

def stable_pick(items, seed_text):
    if not items:
        return None
    total = 0
    for index, char in enumerate(seed_text or "dam-demo"):
        total += (index + 1) * ord(char)
    return items[total % len(items)]

def theme_keywords_for_labels(labels):
    keywords = []
    for label in labels:
        slug = slugify_label(label)
        if not slug:
            continue
        keywords.append(slug)
        for theme_slug, theme_keywords in THEME_IMAGE_KEYWORDS.items():
            if theme_slug in slug or slug in theme_slug:
                keywords.extend(theme_keywords)
    return sorted({keyword for keyword in keywords if keyword})

def remote_theme_images_for_labels(labels):
    urls = []
    for label in labels:
        slug = slugify_label(label)
        if not slug:
            continue
        for theme_slug, theme_urls in REMOTE_THEME_IMAGE_URLS.items():
            if theme_slug in slug or slug in theme_slug:
                urls.extend(theme_urls)
    return list(dict.fromkeys(urls))

def resolve_demo_image_url(*labels):
    files = available_demo_images()

    normalized_files = {
        name: slugify_label(os.path.splitext(name)[0])
        for name in files
    }
    label_slugs = [slugify_label(label) for label in labels if slugify_label(label)]
    keyword_slugs = theme_keywords_for_labels(labels)
    remote_matches = remote_theme_images_for_labels(labels)

    exact_matches = []
    for name, normalized in normalized_files.items():
        if any(label in normalized or normalized in label for label in label_slugs):
            exact_matches.append(name)

    themed_matches = []
    if not exact_matches:
        for name, normalized in normalized_files.items():
            if any(keyword in normalized for keyword in keyword_slugs):
                themed_matches.append(name)

    candidates = exact_matches or themed_matches or remote_matches or files
    chosen = stable_pick(candidates, "|".join(label_slugs) or "dam-demo")
    if not chosen:
        return None
    if str(chosen).startswith("http"):
        return chosen
    return f"/static/demo_images/{chosen}"

def demo_image_palette(intensity):
    if intensity == "high":
        return ("#3d1216", "#9d2f2c", "#ffd8d2", "#ffb3a8")
    if intensity == "medium":
        return ("#5d470d", "#b88310", "#fff1bf", "#ffe08b")
    return ("#123728", "#3e8f5d", "#d8efc9", "#bde5c1")

def build_demo_image_data(case):
    real_photo = resolve_demo_image_url(case.get("city"), case.get("state"), case.get("theme"))
    if real_photo:
        return real_photo

    dark, accent, soft, soft_two = demo_image_palette(case["intensity"])
    status_label = {
        "high": "Not Under Control",
        "medium": "Watch Closely",
        "low": "Out Of Danger",
    }[case["intensity"]]
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 820">
      <defs>
        <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="{dark}" />
          <stop offset="100%" stop-color="{accent}" />
        </linearGradient>
      </defs>
      <rect width="1200" height="820" fill="url(#bg)" />
      <rect x="34" y="34" width="1132" height="752" rx="36" fill="rgba(255,250,242,0.12)" stroke="rgba(255,250,242,0.65)" stroke-width="4" />
      <circle cx="210" cy="170" r="88" fill="{soft}" opacity="0.95" />
      <path d="M0 640 C160 560 260 610 390 540 C520 470 680 520 820 470 C940 430 1040 470 1200 390 L1200 820 L0 820 Z" fill="{soft}" opacity="0.35" />
      <path d="M0 700 C140 650 220 690 370 630 C530 565 700 640 860 590 C980 552 1060 590 1200 545" stroke="{soft_two}" stroke-width="18" fill="none" opacity="0.55" />
      <path d="M110 610 C200 500 280 470 390 410 C520 340 650 350 760 290 C860 235 960 220 1090 180" stroke="rgba(255,255,255,0.32)" stroke-width="12" fill="none" stroke-linecap="round" />
      <rect x="84" y="88" width="278" height="64" rx="32" fill="{soft}" />
      <text x="112" y="131" font-family="Manrope, Arial, sans-serif" font-size="34" font-weight="700" fill="{dark}">{case["theme"]}</text>
      <text x="84" y="270" font-family="Space Grotesk, Arial, sans-serif" font-size="94" font-weight="700" fill="#fffdf8">{case["city"]}</text>
      <text x="84" y="338" font-family="Manrope, Arial, sans-serif" font-size="42" font-weight="600" fill="#fff7ef">{case["state"]}, {case["country"]}</text>
      <text x="84" y="414" font-family="Manrope, Arial, sans-serif" font-size="28" font-weight="600" fill="#fff7ef">DAM DEMO VISUAL</text>
      <rect x="84" y="640" width="320" height="86" rx="24" fill="#fffdf8" />
      <text x="114" y="690" font-family="Manrope, Arial, sans-serif" font-size="34" font-weight="800" fill="{dark}">{status_label}</text>
    </svg>
    """
    return "data:image/svg+xml;utf8," + quote(svg)

def seed_demo_records(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seed_runs (
            seed_key  TEXT PRIMARY KEY,
            seeded_at TEXT NOT NULL
        )
    """)
    existing_seed = conn.execute(
        "SELECT seed_key FROM seed_runs WHERE seed_key = ?",
        (INDIA_DEMO_SEED_KEY,)
    ).fetchone()
    if existing_seed:
        return

    now = datetime.datetime.now()
    tweet_rows = []
    submission_rows = []

    for index, case in enumerate(DEMO_INDIA_CASES):
        location = f"{case['city']}, {case['state']}, {case['country']}"
        review_time = (now - datetime.timedelta(hours=index * 3 + 1)).isoformat()
        submission_rows.append((
            case["reporter"],
            case["email"],
            case["phone"],
            case["country"],
            case["state"],
            case["city"],
            case["pincode"],
            case["details"],
            build_demo_image_data(case),
            "accepted",
            "Seeded demo report for India-wide disaster showcase.",
            "admin@dam.local",
            review_time,
            review_time,
        ))

        for update_index, text in enumerate(case["tweets"]):
            tweet_rows.append((
                text,
                classification_for_intensity(case["intensity"]),
                case["intensity"],
                location,
                case["lat"],
                case["lng"],
                (now - datetime.timedelta(hours=index * 3, minutes=update_index * 24)).isoformat(),
            ))

    conn.executemany(
        """
        INSERT INTO submissions (
            name, email, phone, country, state, city, pincode,
            details, image_data, status, admin_notes, reviewer_email, reviewed_at, timestamp
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        submission_rows,
    )
    conn.executemany(
        """
        INSERT INTO tweets (
            text, classification, intensity, location, lat, lng, timestamp
        ) VALUES (?,?,?,?,?,?,?)
        """,
        tweet_rows,
    )
    conn.execute(
        "INSERT INTO seed_runs (seed_key, seeded_at) VALUES (?, ?)",
        (INDIA_DEMO_SEED_KEY, now.isoformat()),
    )

def refresh_seeded_demo_images(conn):
    demo_rows = conn.execute(
        """
        SELECT id, city, state, country, details, image_data
        FROM submissions
        WHERE email LIKE '%.demo@dam.local'
        """
    ).fetchall()

    for row in demo_rows:
        theme = demo_theme_for_city_state(row["city"], row["state"])
        real_photo = resolve_demo_image_url(
            row["city"],
            row["state"],
            theme,
            row["details"],
        )
        if not real_photo:
            continue
        current_image = row["image_data"] or ""
        if current_image == real_photo:
            continue
        conn.execute(
            "UPDATE submissions SET image_data = ? WHERE id = ?",
            (real_photo, row["id"])
        )

# ---------------------------------------------------------------------------
# AUTH DECORATOR
# ---------------------------------------------------------------------------

def request_wants_json():
    best = request.accept_mimetypes.best or ""
    return (
        request.is_json
        or request.path.startswith("/api/")
        or request.path in {"/predict", "/map-data"}
        or best == "application/json"
    )

def is_valid_email(email):
    return bool(EMAIL_RE.match(str(email or "").strip()))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request_wants_json():
                return jsonify({"success": False, "message": "Login required"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request_wants_json():
                return jsonify({"success": False, "message": "Login required"}), 401
            return redirect(url_for("login_page"))
        if not session.get("is_admin"):
            if request_wants_json():
                return jsonify({"success": False, "message": "Admin access required"}), 403
            return redirect(url_for("home_page"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# PAGE ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def root():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    if session.get("is_admin"):
        return redirect(url_for("admin_page"))
    return redirect(url_for("home_page"))

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/signup")
def signup_page():
    return render_template("signup.html")

@app.route("/home")
@login_required
def home_page():
    conn = get_db()
    tweet_count = conn.execute("SELECT COUNT(*) AS count FROM tweets").fetchone()["count"]
    submission_count = conn.execute("SELECT COUNT(*) AS count FROM submissions").fetchone()["count"]
    area_count = conn.execute(
        "SELECT COUNT(DISTINCT location) AS count FROM tweets WHERE location IS NOT NULL AND location != ''"
    ).fetchone()["count"]
    recent_tweets = conn.execute(
        "SELECT * FROM tweets ORDER BY id DESC LIMIT 5"
    ).fetchall()
    recent_reports = conn.execute(
        "SELECT * FROM submissions ORDER BY id DESC LIMIT 5"
    ).fetchall()
    conn.close()
    return render_template(
        "home.html",
        tweet_count=tweet_count,
        submission_count=submission_count,
        area_count=area_count,
        recent_tweets=recent_tweets,
        recent_reports=recent_reports,
    )

@app.route("/dashboard")
@login_required
def dashboard_page():
    return render_template("dashboard.html")

@app.route("/submission")
@login_required
def submission_page():
    return render_template("submission.html")

@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ---------------------------------------------------------------------------
# AUTH APIs
# ---------------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email = data.get("email","").strip().lower()
    password = data.get("password","").strip()
    if not email or not password:
        return jsonify({"success": False, "message": "All fields required"}), 400
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if user and verify_password(password, user["password"]):
        maybe_upgrade_password(conn, user["id"], password, user["password"])
        session.clear()
        session["user_id"] = user["id"]
        session["email"] = user["email"]
        session["is_admin"] = bool(user["is_admin"])
        session.permanent = True
        conn.close()
        return jsonify({"success": True, "redirect": "/admin" if user["is_admin"] else "/home"})
    conn.close()
    return jsonify({"success": False, "message": "Invalid email or password"}), 401

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    email    = data.get("email","").strip().lower()
    phone    = data.get("phone","").strip()
    password = data.get("password","").strip()
    confirm  = data.get("confirm_password","").strip()
    if not email or not password:
        return jsonify({"success": False, "message": "Email and password required"}), 400
    if not is_valid_email(email):
        return jsonify({"success": False, "message": "Enter a valid email address"}), 400
    if len(password) < 8:
        return jsonify({"success": False, "message": "Password must be at least 8 characters"}), 400
    if password != confirm:
        return jsonify({"success": False, "message": "Passwords do not match"}), 400
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (email, phone, password) VALUES (?,?,?)",
            (email, phone, hash_password(password))
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"success": False, "message": "Email already registered"}), 409
    conn.close()
    return jsonify({"success": True, "redirect": "/login"})

# ---------------------------------------------------------------------------
# PREDICT API
# ---------------------------------------------------------------------------

@app.route("/predict", methods=["POST"])
@login_required
def predict():
    data = request.get_json(silent=True) or {}
    text = data.get("text","").strip()
    if not text:
        return jsonify({"error": "Tweet text is required"}), 400
    if len(text) < 8:
        return jsonify({"error": "Tweet text is too short for analysis"}), 400

    analysis = analyze_tweet(text)
    classification = analysis["classification"]
    intensity = analysis["intensity"]
    color = analysis["color"]
    loc_name, lat, lng = extract_location(text)

    # Override location with form fields if provided
    city    = data.get("city","").strip()
    state   = data.get("state","").strip()
    country = data.get("country","").strip()
    selected_location = data.get("selected_location","").strip()
    selected_lat = safe_float(data.get("selected_lat"))
    selected_lng = safe_float(data.get("selected_lng"))
    if city or state or country:
        parts = [x for x in [city, state, country] if x]
        loc_name = ", ".join(parts)
        known_city = lookup_city_coordinates(city)
        if known_city:
            _, lat, lng = known_city

    if selected_location:
        loc_name = selected_location
    if selected_lat is not None and selected_lng is not None:
        lat, lng = selected_lat, selected_lng

    timestamp = datetime.datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO tweets (text,classification,intensity,location,lat,lng,timestamp) VALUES (?,?,?,?,?,?,?)",
        (text, classification, intensity, loc_name, lat, lng, timestamp)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "classification": classification,
        "intensity": intensity,
        "color": color,
        "disaster_type": analysis["disaster_type"],
        "matched_terms": analysis["matched_terms"],
        "confidence": analysis["confidence"],
        "location": loc_name,
        "lat": lat,
        "lng": lng,
        "timestamp": timestamp
    })

# ---------------------------------------------------------------------------
# MAP DATA API
# ---------------------------------------------------------------------------

@app.route("/map-data", methods=["GET"])
@login_required
def map_data():
    color_map = {"high": "red", "medium": "yellow", "low": "green"}
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tweets ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([{
        "id": r["id"], "text": r["text"],
        "classification": r["classification"],
        "intensity": r["intensity"],
        "color": color_map.get(r["intensity"], "green"),
        "location": r["location"],
        "lat": r["lat"], "lng": r["lng"],
        "timestamp": r["timestamp"]
    } for r in rows])

# ---------------------------------------------------------------------------
# SUBMISSION API
# ---------------------------------------------------------------------------

@app.route("/api/submit", methods=["POST"])
@login_required
def api_submit():
    data = request.get_json(silent=True) or {}
    reporter_email = session.get("email", "").strip().lower()
    email = data.get("email","").strip().lower() or reporter_email
    name = data.get("name","").strip()
    details = data.get("details","").strip()
    city = data.get("city","").strip()
    state = data.get("state","").strip()
    country = data.get("country","").strip()
    if not name or not city or not state or not country or not details:
        return jsonify({"success": False, "message": "Name, location, and disaster details are required."}), 400
    if not email or not is_valid_email(email):
        return jsonify({"success": False, "message": "A valid reporter email is required."}), 400
    conn = get_db()
    conn.execute(
        """
        INSERT INTO submissions (
            name, email, phone, country, state, city, pincode,
            details, image_data, status, admin_notes, reviewer_email, reviewed_at, timestamp
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (name, email, data.get("phone","").strip(),
         country, state, city,
         data.get("pincode","").strip(), details, data.get("image_data",""),
         "pending", "", "", "",
         datetime.datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Submission saved and sent to admin review."})

@app.route("/api/tweets", methods=["GET"])
@login_required
def api_tweets():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tweets ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/submissions", methods=["GET"])
@login_required
def api_submissions():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM submissions WHERE status = 'accepted' ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([serialize_submission(r) for r in rows])

@app.route("/api/admin/submissions", methods=["GET"])
@admin_required
def api_admin_submissions():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM submissions
        ORDER BY
            CASE status
                WHEN 'pending' THEN 0
                WHEN 'accepted' THEN 1
                WHEN 'rejected' THEN 2
                ELSE 3
            END,
            id DESC
        """
    ).fetchall()
    conn.close()
    submissions = [serialize_submission(r) for r in rows]
    stats = {
        "total": len(submissions),
        "pending": sum(1 for item in submissions if item["status"] == "pending"),
        "accepted": sum(1 for item in submissions if item["status"] == "accepted"),
        "rejected": sum(1 for item in submissions if item["status"] == "rejected"),
    }
    return jsonify({"submissions": submissions, "stats": stats})

@app.route("/api/admin/submissions/<int:submission_id>", methods=["POST"])
@admin_required
def api_admin_update_submission(submission_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM submissions WHERE id = ?",
        (submission_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"success": False, "message": "Submission not found"}), 404

    status = normalize_submission_status(data.get("status", existing["status"]))
    admin_notes = str(data.get("admin_notes", existing["admin_notes"] or "")).strip()
    reviewed_at = datetime.datetime.now().isoformat()
    reviewer_email = session.get("email", ADMIN_EMAIL)
    email = str(data.get("email", existing["email"] or "")).strip().lower()
    details = str(data.get("details", existing["details"] or "")).strip()
    city = str(data.get("city", existing["city"] or "")).strip()
    state = str(data.get("state", existing["state"] or "")).strip()
    country = str(data.get("country", existing["country"] or "")).strip()

    if email and not is_valid_email(email):
        conn.close()
        return jsonify({"success": False, "message": "Enter a valid reporter email"}), 400
    if not details:
        conn.close()
        return jsonify({"success": False, "message": "Submission details cannot be empty"}), 400
    if not city or not state or not country:
        conn.close()
        return jsonify({"success": False, "message": "City, state, and country are required"}), 400

    updated_values = (
        str(data.get("name", existing["name"] or "")).strip(),
        email,
        str(data.get("phone", existing["phone"] or "")).strip(),
        country,
        state,
        city,
        str(data.get("pincode", existing["pincode"] or "")).strip(),
        details,
        status,
        admin_notes,
        reviewer_email,
        reviewed_at,
        submission_id,
    )

    conn.execute(
        """
        UPDATE submissions
        SET
            name = ?, email = ?, phone = ?,
            country = ?, state = ?, city = ?, pincode = ?,
            details = ?, status = ?, admin_notes = ?,
            reviewer_email = ?, reviewed_at = ?
        WHERE id = ?
        """,
        updated_values,
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM submissions WHERE id = ?",
        (submission_id,)
    ).fetchone()
    conn.close()
    return jsonify({
        "success": True,
        "message": f"Submission {status}.",
        "submission": serialize_submission(row),
    })

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("\n  DAM Server running → http://127.0.0.1:5000\n")
    app.run(debug=True, port=5000)
