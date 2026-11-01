import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest
from tests.conftest import seed_initial_data_if_needed

from fastapi.testclient import TestClient
from main import app
from app.database import engine, Base, SessionLocal
from app.migration import run_schema_migration
from app.models import User
from app.auth import sign_session
from app.jobs import job_manager

class BackgroundJobsTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_schema_migration()
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()
        cls.client = TestClient(app)

        db = SessionLocal()
        try:
            cls.admin = db.query(User).filter(User.username == "job_admin").first()
            if not cls.admin:
                cls.admin = User(username="job_admin", password_hash="hash", full_name="Job Admin", role="admin", active=True)
                db.add(cls.admin)
                db.commit()

            cls.admin_token = sign_session({"username": cls.admin.username, "role": "admin"})
        finally:
            db.close()

    def test_01_async_report_generation_lifecycle(self):
        """Submit background report job, poll until completed, download output file."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.admin_token)

        # 1. Trigger async report job
        resp = client.post("/api/reports/async/generate")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        job_id = data["job_id"]
        self.assertTrue(job_id.startswith("job_"))

        # 2. Poll job status
        max_retries = 30
        completed = False
        download_url = None

        for _ in range(max_retries):
            st_resp = client.get(f"/api/reports/async/status/{job_id}")
            self.assertEqual(st_resp.status_code, 200)
            st_data = st_resp.json()
            if st_data["status"] == "completed" or st_data.get("progress") == 100:
                completed = True
                download_url = st_data["download_url"]
                break
            time.sleep(0.2)

        self.assertTrue(completed, "Report job did not complete in time")
        self.assertIsNotNone(download_url)

        # 3. Download the completed file
        dl_resp = client.get(f"/api/reports/async/download/{job_id}")
        self.assertEqual(dl_resp.status_code, 200)
        self.assertEqual(
            dl_resp.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.assertTrue(len(dl_resp.content) > 1000)

    def test_02_unknown_job_returns_404(self):
        """Requesting nonexistent job returns 404."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.admin_token)

        r_st = client.get("/api/reports/async/status/job_nonexistent_xyz")
        self.assertEqual(r_st.status_code, 404)

        r_dl = client.get("/api/reports/async/download/job_nonexistent_xyz")
        self.assertEqual(r_dl.status_code, 404)

    def test_03_legacy_job_routes_compatibility(self):
        """Legacy /api/reports/jobs routes remain fully functional."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.admin_token)

        resp = client.post("/api/reports/jobs")
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()["job_id"]

        st_resp = client.get(f"/api/reports/jobs/{job_id}")
        self.assertEqual(st_resp.status_code, 200)
        self.assertIn("status", st_resp.json())

if __name__ == "__main__":
    unittest.main()

