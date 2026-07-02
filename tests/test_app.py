import importlib.util
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app.py"
SPEC = importlib.util.spec_from_file_location("dam_app", APP_PATH)
dam_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dam_app)


class DamAppSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = dam_app.DB_PATH
        self.original_testing = dam_app.app.config.get("TESTING", False)

        dam_app.DB_PATH = str(Path(self.temp_dir.name) / "dam_test.db")
        dam_app.app.config.update(TESTING=True, SECRET_KEY="dam-test-secret")
        dam_app.init_db()
        self.client = dam_app.app.test_client()

    def tearDown(self):
        dam_app.DB_PATH = self.original_db_path
        dam_app.app.config["TESTING"] = self.original_testing
        self.temp_dir.cleanup()

    def register_user(self, email="field@example.com", password="fieldpass123"):
        return self.client.post(
            "/api/register",
            json={
                "email": email,
                "phone": "9876543210",
                "password": password,
                "confirm_password": password,
            },
        )

    def login_user(self, email, password):
        return self.client.post(
            "/api/login",
            json={"email": email, "password": password},
        )

    def test_predict_requires_login(self):
        response = self.client.post("/predict", json={"text": "Flood warning in Chennai"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["message"], "Login required")

    def test_register_login_and_predict_flow(self):
        register_response = self.register_user()
        self.assertEqual(register_response.status_code, 200)

        login_response = self.login_user("field@example.com", "fieldpass123")
        self.assertEqual(login_response.status_code, 200)

        predict_response = self.client.post(
            "/predict",
            json={"text": "Major flood rescue underway in Chennai streets"},
        )
        self.assertEqual(predict_response.status_code, 200)
        payload = predict_response.get_json()
        self.assertEqual(payload["classification"], "Disaster")
        self.assertEqual(payload["color"], "red")
        self.assertEqual(payload["disaster_type"], "Flood")
        self.assertIn("flood", payload["matched_terms"])
        self.assertTrue(payload["confidence"] >= 0.35)

    def test_submission_moderation_flow(self):
        self.register_user()
        self.login_user("field@example.com", "fieldpass123")

        submit_response = self.client.post(
            "/api/submit",
            json={
                "name": "Field User",
                "email": "field@example.com",
                "phone": "9876543210",
                "country": "India",
                "state": "Karnataka",
                "city": "Mysuru",
                "pincode": "570001",
                "details": "Floodwater has entered low streets and field teams are moving residents to shelters.",
                "image_data": "/static/demo_images/flood-road-1.jpg",
            },
        )
        self.assertEqual(submit_response.status_code, 200)

        self.login_user(dam_app.ADMIN_EMAIL, dam_app.ADMIN_PASSWORD)
        admin_response = self.client.get("/api/admin/submissions")
        self.assertEqual(admin_response.status_code, 200)
        submissions = admin_response.get_json()["submissions"]

        target = next(
            item for item in submissions
            if item["email"] == "field@example.com" and item["city"] == "Mysuru"
        )
        review_response = self.client.post(
            f"/api/admin/submissions/{target['id']}",
            json={
                "status": "accepted",
                "name": target["name"],
                "email": target["email"],
                "phone": target["phone"],
                "country": target["country"],
                "state": target["state"],
                "city": target["city"],
                "pincode": target["pincode"],
                "details": target["details"],
                "admin_notes": "Approved during smoke test.",
            },
        )
        self.assertEqual(review_response.status_code, 200)

        accepted_response = self.client.get("/api/submissions")
        self.assertEqual(accepted_response.status_code, 200)
        accepted_locations = [item["locationLabel"] for item in accepted_response.get_json()]
        self.assertIn("Mysuru, Karnataka, India", accepted_locations)


if __name__ == "__main__":
    unittest.main()
