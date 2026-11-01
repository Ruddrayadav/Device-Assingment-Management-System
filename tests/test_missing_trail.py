import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest
from tests.conftest import seed_initial_data_if_needed

from fastapi.testclient import TestClient
from main import app
from app.database import engine, Base, SessionLocal
from app.migration import run_schema_migration
from app.models import User, Device, Employee, MissingEvent, DeviceAssignment
from app.auth import sign_session, hash_password
from app.scan_engine import issue_device_atomic, process_scan_pipeline

class MissingTrailTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_schema_migration()
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()
        cls.client = TestClient(app)

        db = SessionLocal()
        try:
            cls.admin = db.query(User).filter(User.username == "miss_admin").first()
            if not cls.admin:
                cls.admin = User(username="miss_admin", password_hash="hash", full_name="Missing Admin", role="admin", active=True)
                db.add(cls.admin)

            cls.im = db.query(User).filter(User.username == "miss_im").first()
            if not cls.im:
                cls.im = User(username="miss_im", password_hash="hash", full_name="Missing IM", role="inventory_manager", active=True)
                db.add(cls.im)

            dev = Device(serial="DEV_MISS_01", make="CHAINWAY", model="MC95", status="Available")
            emp = Employee(employee_id="EMP_MISS_01", name="Missing Worker", department="Packing", active=True)
            db.merge(dev)
            db.merge(emp)
            db.commit()

            cls.admin_token = sign_session({"username": cls.admin.username, "role": "admin"})
            cls.im_token = sign_session({"username": cls.im.username, "role": "inventory_manager"})
        finally:
            db.close()

    def test_01_confirm_missing_creates_event_and_updates_status(self):
        """Admin confirms missing device -> status becomes Missing and MissingEvent is created."""
        db = SessionLocal()
        try:
            # Issue first
            issue_device_atomic(db, "DEV_MISS_01", "EMP_MISS_01", self.admin)

            client = TestClient(app)
            client.cookies.set("hhd_session", self.admin_token)

            resp = client.post(
                "/admin/missing/confirm",
                data={"serial": "DEV_MISS_01", "reason": "Not returned after cutoff", "notes": "Searched packing area"},
                follow_redirects=False
            )
            self.assertEqual(resp.status_code, 302)

            db.expire_all()
            dev = db.query(Device).filter(Device.serial == "DEV_MISS_01").first()
            self.assertEqual(dev.status, "Missing")
            self.assertEqual(dev.condition, "Missing")

            # Check MissingEvent
            events = db.query(MissingEvent).filter(MissingEvent.serial == "DEV_MISS_01").all()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event.status, "CONFIRMED_MISSING")
            self.assertEqual(event.employee_id, "EMP_MISS_01")
            self.assertEqual(event.confirmed_by_name, "Missing Admin")
            self.assertEqual(event.reason, "Not returned after cutoff")

            # Verify device CANNOT be issued while Missing
            res_issue = process_scan_pipeline(
                db=db,
                code="DEV_MISS_01",
                mode="Issue",
                pending={"type": "employee", "code": "EMP_MISS_01"},
                operator=self.admin
            )
            self.assertFalse(res_issue["ok"])
            self.assertIn("Missing", res_issue["message"])
        finally:
            db.close()

    def test_02_recovery_workflow_requires_inspection(self):
        """Recovering a missing device defaults status to Unavailable (Pending Inspection)."""
        db = SessionLocal()
        try:
            client = TestClient(app)
            client.cookies.set("hhd_session", self.im_token)

            resp = client.post(
                "/admin/missing/recover",
                data={
                    "serial": "DEV_MISS_01",
                    "found_by": "Security Guard",
                    "recovery_condition": "Working",
                    "recovery_notes": "Found in cafeteria",
                    "post_recovery_status": "Unavailable"
                },
                follow_redirects=False
            )
            self.assertEqual(resp.status_code, 302)

            db.expire_all()
            dev = db.query(Device).filter(Device.serial == "DEV_MISS_01").first()
            self.assertEqual(dev.status, "Unavailable")
            self.assertEqual(dev.unavailable_reason, "Recovered - Pending Final Inspection")

            # Check MissingEvent is resolved
            event = db.query(MissingEvent).filter(MissingEvent.serial == "DEV_MISS_01").order_by(MissingEvent.id.desc()).first()
            self.assertEqual(event.status, "RECOVERED")
            self.assertEqual(event.found_by, "Security Guard")
            self.assertIsNotNone(event.recovered_at)
        finally:
            db.close()

    def test_03_repeated_missing_events_create_distinct_history(self):
        """If a device goes missing more than once, each event is preserved independently."""
        db = SessionLocal()
        try:
            dev = db.query(Device).filter(Device.serial == "DEV_MISS_01").first()
            dev.status = "Available"
            dev.unavailable_reason = None
            db.commit()

            # Issue again
            issue_device_atomic(db, "DEV_MISS_01", "EMP_MISS_01", self.admin)

            client = TestClient(app)
            client.cookies.set("hhd_session", self.admin_token)

            # Confirm missing a second time
            resp = client.post(
                "/admin/missing/confirm",
                data={"serial": "DEV_MISS_01", "reason": "Second incident: left at dock", "notes": "Shift 2 dock"},
                follow_redirects=False
            )
            self.assertEqual(resp.status_code, 302)

            db.expire_all()
            all_events = db.query(MissingEvent).filter(MissingEvent.serial == "DEV_MISS_01").order_by(MissingEvent.id.asc()).all()
            self.assertEqual(len(all_events), 2, "Must preserve both missing events in history")
            self.assertEqual(all_events[0].status, "RECOVERED")
            self.assertEqual(all_events[1].status, "CONFIRMED_MISSING")
            self.assertEqual(all_events[1].reason, "Second incident: left at dock")
        finally:
            db.close()

    def test_04_missing_trail_excel_export(self):
        """Missing trail export generates formatted .xlsx file."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.admin_token)
        resp = client.get("/admin/missing/export")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("missing_device_trail_", resp.headers["content-disposition"].lower())

if __name__ == "__main__":
    unittest.main()
