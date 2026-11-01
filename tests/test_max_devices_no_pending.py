import os
import sys
import unittest
import time
from unittest.mock import patch
from datetime import datetime

from fastapi.testclient import TestClient

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from main import app
from app.database import engine, Base, SessionLocal
from app.models import User, Device, Employee, Transaction, DeviceAssignment, SystemSetting, AuditLog
from app.auth import sign_session, ROLE_ADMIN, ROLE_OPERATOR
from app.migration import run_schema_migration

class MaxDevicesNoPendingTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        run_schema_migration()
        tests.conftest.seed_initial_data_if_needed()
        tests.conftest.seed_test_users()

    def setUp(self):
        self.client = TestClient(app)
        self.admin_token = sign_session({"username": "hhdadmin", "role": ROLE_ADMIN})
        self.client.cookies.set("hhd_session", self.admin_token)

        # Ensure baseline test entities
        db = SessionLocal()
        try:
            emp = db.query(Employee).filter(Employee.employee_id == "EMP_NP_01").first()
            if not emp:
                emp = Employee(employee_id="EMP_NP_01", name="No Pending Worker", department="Warehouse", active=True)
                db.add(emp)

            # Ensure 4 devices exist
            for i in range(1, 5):
                s = f"DEV_NP_{i:03d}"
                d = db.query(Device).filter(Device.serial == s).first()
                if not d:
                    d = Device(serial=s, make="Zebra", model="TC21", status="Available")
                    db.add(d)

            # Default max devices to 1
            s_set = db.query(SystemSetting).filter(SystemSetting.key == "max_devices_per_worker").first()
            if s_set:
                s_set.value = "1"
            else:
                db.add(SystemSetting(key="max_devices_per_worker", value="1", description="Max Devices"))

            # Return all devices from this worker
            db.query(Device).filter(Device.assigned_employee_id == "EMP_NP_01").update({"status": "Available", "assigned_employee_id": None})
            db.commit()
        finally:
            db.close()

    def _set_worker_held(self, db, emp_id: str, count: int):
        """Helper to ensure worker holds exactly `count` devices."""
        # First reset all DEV_NP_* devices
        db.query(Device).filter(Device.serial.like("DEV_NP_%")).update({"status": "Available", "assigned_employee_id": None})
        db.commit()

        if count > 0:
            devs = db.query(Device).filter(Device.serial.like("DEV_NP_%")).limit(count).all()
            for d in devs:
                d.status = "Issued"
                d.assigned_employee_id = emp_id
            db.commit()

    # -------------------------------------------------------------
    # 1. Worker-first scan with pending=None when worker holds 1 device (limit 1)
    # -------------------------------------------------------------
    def test_01_worker_first_scan_no_pending_returns_warning_not_500(self):
        """Worker holds 1 device (limit 1). POST /api/scan with code=<worker>, mode=Issue, pending=None -> 200, ok=False, title 'Max Devices Limit'."""
        db = SessionLocal()
        try:
            self._set_worker_held(db, "EMP_NP_01", 1)
        finally:
            db.close()

        res = self.client.post("/api/scan", json={
            "code": "EMP_NP_01",
            "mode": "Issue",
            "pending": None
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("title"), "Max Devices Limit")
        self.assertIn("already holds device", data.get("message", ""))
        pending = data.get("pending")
        self.assertIsNotNone(pending)
        self.assertEqual(pending.get("warn_emp"), "EMP_NP_01")
        self.assertIsNotNone(pending.get("warn_time"))

    # -------------------------------------------------------------
    # 2. Confirm override on second scan within 10s, then scan device
    # -------------------------------------------------------------
    def test_02_worker_first_override_confirmation_and_issue(self):
        """Scan worker again within 10s -> 'Override Confirmed'. Then scan free device -> state 'done', holds 2, audit row exists."""
        db = SessionLocal()
        try:
            self._set_worker_held(db, "EMP_NP_01", 1)
            # Clear previous audit logs for this worker
            db.query(AuditLog).filter(AuditLog.target_id == "EMP_NP_01").delete()
            db.commit()
        finally:
            db.close()

        # Step 1: Scan worker -> Max Devices Limit warning
        r1 = self.client.post("/api/scan", json={
            "code": "EMP_NP_01",
            "mode": "Issue",
            "pending": None
        })
        self.assertEqual(r1.status_code, 200)
        p1 = r1.json().get("pending")
        self.assertIsNotNone(p1)

        # Step 2: Scan worker again with pending within 10s
        r2 = self.client.post("/api/scan", json={
            "code": "EMP_NP_01",
            "mode": "Issue",
            "pending": p1
        })
        self.assertEqual(r2.status_code, 200)
        d2 = r2.json()
        self.assertTrue(d2.get("ok"))
        self.assertEqual(d2.get("state"), "pending")
        self.assertTrue(d2.get("title", "").startswith("Override Confirmed"))
        p2 = d2.get("pending")
        self.assertIsNotNone(p2)
        self.assertTrue(p2.get("override_confirmed"))

        # Step 3: Scan free device DEV_NP_002
        r3 = self.client.post("/api/scan", json={
            "code": "DEV_NP_002",
            "mode": "Issue",
            "pending": p2
        })
        self.assertEqual(r3.status_code, 200)
        d3 = r3.json()
        self.assertTrue(d3.get("ok"))
        self.assertEqual(d3.get("state"), "done")

        # Verify worker now holds 2 devices
        db = SessionLocal()
        try:
            held = db.query(Device).filter(Device.assigned_employee_id == "EMP_NP_01", Device.status == "Issued").count()
            self.assertEqual(held, 2)

            # Verify audit log exists
            audit_cnt = db.query(AuditLog).filter(
                AuditLog.action == "MAX_DEVICES_OVERRIDE",
                AuditLog.target_id == "EMP_NP_01"
            ).count()
            self.assertGreaterEqual(audit_cnt, 1)
        finally:
            db.close()

    # -------------------------------------------------------------
    # 3. Device-first order
    # -------------------------------------------------------------
    def test_03_device_first_order_override(self):
        """Device scan, then holding worker -> 'Max Devices Limit'; scan worker again -> done."""
        db = SessionLocal()
        try:
            self._set_worker_held(db, "EMP_NP_01", 1)
        finally:
            db.close()

        # Step 1: Scan available device DEV_NP_003
        r1 = self.client.post("/api/scan", json={
            "code": "DEV_NP_003",
            "mode": "Issue",
            "pending": None
        })
        self.assertEqual(r1.status_code, 200)
        p1 = r1.json().get("pending")
        self.assertIsNotNone(p1)
        self.assertEqual(p1.get("type"), "device")

        # Step 2: Scan holding worker -> Max Devices Limit warning
        r2 = self.client.post("/api/scan", json={
            "code": "EMP_NP_01",
            "mode": "Issue",
            "pending": p1
        })
        self.assertEqual(r2.status_code, 200)
        d2 = r2.json()
        self.assertFalse(d2.get("ok"))
        self.assertEqual(d2.get("title"), "Max Devices Limit")
        p2 = d2.get("pending")
        self.assertIsNotNone(p2)
        self.assertEqual(p2.get("warn_emp"), "EMP_NP_01")

        # Step 3: Scan worker again within 10s -> done
        r3 = self.client.post("/api/scan", json={
            "code": "EMP_NP_01",
            "mode": "Issue",
            "pending": p2
        })
        self.assertEqual(r3.status_code, 200)
        d3 = r3.json()
        self.assertTrue(d3.get("ok"))
        self.assertEqual(d3.get("state"), "done")

    # -------------------------------------------------------------
    # 4. Refuse confirmation when waiting 11s (>10s)
    # -------------------------------------------------------------
    def test_04_confirmation_refused_after_11_seconds(self):
        """Wait 11s (mock time.time) -> confirmation is refused and warns again."""
        db = SessionLocal()
        try:
            self._set_worker_held(db, "EMP_NP_01", 1)
        finally:
            db.close()

        t0 = 1000000.0
        with patch("app.scan_engine.time.time", return_value=t0):
            r1 = self.client.post("/api/scan", json={
                "code": "EMP_NP_01",
                "mode": "Issue",
                "pending": None
            })
            self.assertEqual(r1.status_code, 200)
            p1 = r1.json().get("pending")
            self.assertIsNotNone(p1)

        # 11 seconds later
        t1 = t0 + 11.0
        with patch("app.scan_engine.time.time", return_value=t1):
            r2 = self.client.post("/api/scan", json={
                "code": "EMP_NP_01",
                "mode": "Issue",
                "pending": p1
            })
            self.assertEqual(r2.status_code, 200)
            d2 = r2.json()
            # Refused! Must warn again
            self.assertFalse(d2.get("ok"))
            self.assertEqual(d2.get("title"), "Max Devices Limit")
            self.assertIn("already holds device", d2.get("message", ""))

    # -------------------------------------------------------------
    # 5. Catch-all matrix test: no 5xx under any combination
    # -------------------------------------------------------------
    def test_05_matrix_never_returns_5xx(self):
        """For each state of (limit 0, 1, 2) x (held 0, 1, 2) x (worker-first, device-first) x (Issue, Return), never 5xx."""
        db = SessionLocal()
        try:
            for limit in [0, 1, 2]:
                s_set = db.query(SystemSetting).filter(SystemSetting.key == "max_devices_per_worker").first()
                if s_set:
                    s_set.value = str(limit)
                else:
                    db.add(SystemSetting(key="max_devices_per_worker", value=str(limit), description="Max Devices"))
                db.commit()

                for held in [0, 1, 2]:
                    self._set_worker_held(db, "EMP_NP_01", held)

                    for mode in ["Issue", "Return"]:
                        # 1. Worker-first scan
                        resp1 = self.client.post("/api/scan", json={
                            "code": "EMP_NP_01",
                            "mode": mode,
                            "pending": None
                        })
                        self.assertLess(resp1.status_code, 500, f"Worker-first failed with {resp1.status_code} (limit={limit}, held={held}, mode={mode})")

                        # If pending resulted, test scanning device next
                        p1 = resp1.json().get("pending")
                        resp_dev = self.client.post("/api/scan", json={
                            "code": "DEV_NP_004",
                            "mode": mode,
                            "pending": p1
                        })
                        self.assertLess(resp_dev.status_code, 500, f"Device follow-up failed with {resp_dev.status_code} (limit={limit}, held={held}, mode={mode})")

                        # 2. Device-first scan
                        resp2 = self.client.post("/api/scan", json={
                            "code": "DEV_NP_004",
                            "mode": mode,
                            "pending": None
                        })
                        self.assertLess(resp2.status_code, 500, f"Device-first failed with {resp2.status_code} (limit={limit}, held={held}, mode={mode})")

                        p2 = resp2.json().get("pending")
                        resp_emp = self.client.post("/api/scan", json={
                            "code": "EMP_NP_01",
                            "mode": mode,
                            "pending": p2
                        })
                        self.assertLess(resp_emp.status_code, 500, f"Worker follow-up failed with {resp_emp.status_code} (limit={limit}, held={held}, mode={mode})")
        finally:
            db.close()

if __name__ == "__main__":
    unittest.main()
