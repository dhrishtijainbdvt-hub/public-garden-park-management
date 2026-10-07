import os
import tempfile
import unittest

TEST_DIRECTORY = tempfile.mkdtemp(prefix="davangere-park-tests-")
os.environ["DATABASE_PATH"] = os.path.join(TEST_DIRECTORY, "test.db")
os.environ["ADMIN_PASSWORD"] = "Test-Admin-Passphrase-2026!"
os.environ["SECRET_KEY"] = "test-only-secret-key"
os.environ["APP_ENV"] = "test"

from app import app, get_collection  # noqa: E402


JPEG_DATA_URL = "data:image/jpeg;base64,/9j/"


class ParkManagementApiTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.environ["DATABASE_PATH"] + suffix)
            except FileNotFoundError:
                pass
        os.rmdir(TEST_DIRECTORY)

    def setUp(self):
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = app.test_client()
        self.csrf = self.client.get("/api/session").get_json()["csrf_token"]

    def tearDown(self):
        from app import set_collection

        for key in ("complaints", "technicians", "suggestions", "extra_parks"):
            set_collection(key, [])

    def post(self, path, body, csrf=None):
        return self.client.post(
            path,
            json=body,
            headers={"X-CSRF-Token": csrf if csrf is not None else self.csrf},
        )

    def login_admin(self):
        response = self.post("/api/login/admin", {"password": os.environ["ADMIN_PASSWORD"]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.csrf = response.get_json()["csrf_token"]

    def create_report(self, photo_consent=False):
        response = self.post("/api/complaints/new", {
            "citizen_name": "Public User",
            "citizen_phone": "9876543210",
            "park_id": 1,
            "park_en": "Visvesvaraya Park",
            "park_kn": "ವಿಶ್ವೇಶ್ವರಯ್ಯ ಉದ್ಯಾನ",
            "issue_type": "issue_bench",
            "priority": "medium",
            "description": "Broken bench",
            "photo_before": JPEG_DATA_URL,
            "photo_consent": photo_consent,
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def test_private_dashboard_data_requires_a_server_session(self):
        self.assertEqual(self.client.get("/api/complaints").status_code, 401)
        self.assertEqual(self.client.get("/api/technicians").status_code, 401)
        self.assertEqual(self.client.get("/api/suggestions").status_code, 401)
        self.assertEqual(self.post("/api/admin-password", {"value": "attacker"}).status_code, 401)
        self.assertEqual(self.client.get("/app.py").status_code, 404)
        self.assertEqual(self.client.get("/tests/test_app.py").status_code, 404)
        self.assertEqual(self.client.get("/api/admin-password").status_code, 404)

    def test_report_validation_tracking_and_private_fields(self):
        invalid = self.post("/api/complaints/new", {
            "citizen_name": "A",
            "citizen_phone": "bad",
            "park_id": 1,
            "issue_type": "issue_bench",
            "priority": "medium",
            "description": "Issue",
            "photo_before": JPEG_DATA_URL,
        })
        self.assertEqual(invalid.status_code, 400)
        invalid_park = self.post("/api/complaints/new", {
            "citizen_name": "A",
            "citizen_phone": "9876543210",
            "park_id": 9999,
            "park_en": "Fake Park",
            "park_kn": "ಉದ್ಯಾನ",
            "issue_type": "issue_bench",
            "priority": "medium",
            "description": "Issue",
            "photo_before": JPEG_DATA_URL,
        })
        self.assertEqual(invalid_park.status_code, 400)

        complaint_id = self.create_report()
        wrong_phone = self.post("/api/track", {"id": complaint_id, "phone": "0000000000"})
        self.assertEqual(wrong_phone.status_code, 404)

        tracked = self.post("/api/track", {
            "id": complaint_id,
            "phone": "+91 9876543210",
        })
        self.assertEqual(tracked.status_code, 200)
        self.assertNotIn("citizen_name", tracked.get_json())
        self.assertNotIn("citizen_phone", tracked.get_json())

    def test_admin_and_technician_sessions_limit_permissions(self):
        complaint_id = self.create_report(photo_consent=True)
        self.login_admin()
        created = self.post("/api/technicians", [{
            "id": 1, "name": "Park Technician", "username": "park-tech",
            "password": "Technician-Password-42", "park_id": 1,
        }])
        self.assertEqual(created.status_code, 200, created.get_json())
        self.assertNotIn("password_hash", self.client.get("/api/technicians").get_json()[0])

        complaints = self.client.get("/api/complaints").get_json()
        complaints[0]["status"] = "assigned"
        complaints[0]["technician_id"] = 1
        self.assertEqual(self.client.put(
            "/api/complaints",
            json=complaints,
            headers={"X-CSRF-Token": self.csrf},
        ).status_code, 200)

        logout = self.post("/api/logout", {})
        self.csrf = logout.get_json()["csrf_token"]
        self.assertEqual(self.client.get("/api/complaints").status_code, 401)
        login = self.post("/api/login/technician", {
            "username": "park-tech", "password": "Technician-Password-42",
        })
        self.assertEqual(login.status_code, 200, login.get_json())
        self.csrf = login.get_json()["csrf_token"]
        assigned = self.client.get("/api/complaints").get_json()
        self.assertEqual(len(assigned), 1)
        self.assertNotIn("citizen_phone", assigned[0])
        assigned[0]["status"] = "verified"
        self.post("/api/complaints", assigned)
        self.assertEqual(get_collection("complaints")[0]["status"], "assigned")

        assigned[0]["status"] = "progress"
        self.assertEqual(self.post("/api/complaints", assigned).status_code, 200)
        assigned[0]["status"] = "resolved"
        assigned[0]["photo_after"] = JPEG_DATA_URL
        assigned[0]["repair_notes"] = "Bench repaired"
        self.assertEqual(self.post("/api/complaints", assigned).status_code, 200)
        self.assertEqual(get_collection("complaints")[0]["status"], "resolved")

        gallery = self.client.get("/api/gallery").get_json()
        self.assertEqual(len(gallery), 1)
        self.assertNotIn("citizen_phone", gallery[0])
        self.assertEqual(gallery[0]["id"], complaint_id)

    def test_mutations_require_csrf(self):
        response = self.client.post("/api/complaints/new", json={})
        self.assertEqual(response.status_code, 403)

    def test_suggestions_are_validated_and_private_until_municipality_review(self):
        response = self.post("/api/suggestions", {
            "park_id": 1,
            "park_en": "Visvesvaraya Park",
            "park_kn": "ಉದ್ಯಾನ",
            "category": "sug_seating",
            "suggestion": "<script>alert(1)</script>Add a shaded bench",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(self.client.get("/api/suggestions").status_code, 401)
        self.login_admin()
        suggestions = self.client.get("/api/suggestions").get_json()
        self.assertEqual(len(suggestions), 1)
        self.assertNotIn("<script>", suggestions[0]["text"])

    def test_feedback_requires_matching_ticket_and_verified_status(self):
        complaint_id = self.create_report()
        response = self.post("/api/feedback", {
            "id": complaint_id, "phone": "9876543210", "rating": 5, "comment": "Great",
        })
        self.assertEqual(response.status_code, 409)

        self.login_admin()
        complaints = self.client.get("/api/complaints").get_json()
        complaints[0]["status"] = "verified"
        complaints[0]["photo_after"] = JPEG_DATA_URL
        updated = self.client.put(
            "/api/complaints",
            json=complaints,
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(updated.status_code, 200)
        feedback = self.post("/api/feedback", {
            "id": complaint_id,
            "phone": "9876543210",
            "rating": 5,
            "comment": "<script>alert(1)</script>",
        })
        self.assertEqual(feedback.status_code, 200)
        tracked = self.post("/api/track", {"id": complaint_id, "phone": "9876543210"})
        self.assertNotIn("<script>", tracked.get_json()["feedback_comment"])
        self.assertEqual(self.client.get("/api/gallery").get_json(), [])


if __name__ == "__main__":
    unittest.main()
