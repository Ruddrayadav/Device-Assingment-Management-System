import os
import sys
import unittest
import time
import json
from pathlib import Path
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from main import app
from app.config import BACKUP_DIR, DB_PATH, now_kolkata, today_kolkata_str
from app.database import engine, Base, SessionLocal
from app.models import User, Device, Employee, Transaction, DeviceAssignment, SystemSetting, AuditLog
from app.auth import (
    sign_session, hash_password, login_limiter,
    ROLE_ADMIN, ROLE_OPERATOR, ROLE_DISPLAY
)
from app.backup import (
    apply_backup_retention, get_last_auto_backup_info, get_backup_type
)
from app.migration import is_schema_current, run_schema_migration
from app.scan_engine import process_scan_pipeline, get_max_devices_per_worker
from app.excel_importer import validate_employee_import

class V3Phase6FeaturesTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        run_schema_migration()
        tests.conftest.seed_initial_data_if_needed()
        tests.conftest.seed_test_users()

    def setUp(self):
        login_limiter.clear_all()
        self.client = TestClient(app)

    def tearDown(self):
        login_limiter.clear_all()
        tests.conftest.seed_test_users()

    # -------------------------------------------------------------
    # 1. Automatic Backup Retention & Stale Banner
    # -------------------------------------------------------------
    def test_backup_retention_rules(self):
        """Verifies retention keeps 14 daily auto + 4 weekly, newest 5 pre_*, and never deletes uploaded_*."""
        test_backup_dir = BACKUP_DIR / "test_retention"
        test_backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Create 20 dummy auto backups with timestamps spread across days
            now = datetime.now()
            for i in range(20):
                ts = (now - timedelta(days=i, hours=2)).strftime("%Y%m%d_%H%M%S")
                f = test_backup_dir / f"hhd_backup_{ts}_auto.db"
                f.write_text("dummy")

            # Create 8 pre_migration safety backups
            for i in range(8):
                ts = (now - timedelta(hours=i * 3)).strftime("%Y%m%d_%H%M%S")
                f = test_backup_dir / f"hhd_backup_{ts}_pre_migration_upgrade.db"
                f.write_text("dummy")

            # Create 3 uploaded snapshots
            f_up1 = test_backup_dir / "uploaded_20261101_120000_manual.db"
            f_up2 = test_backup_dir / "uploaded_20261101_130000_clone.db"
            f_up1.write_text("dummy")
            f_up2.write_text("dummy")

            # Apply retention
            summary = apply_backup_retention(test_backup_dir)
            self.assertGreater(summary["deleted_auto"], 0)
            self.assertEqual(summary["deleted_pre"], 3)  # 8 - 5 = 3 deleted

            # Verify uploaded files still exist
            self.assertTrue(f_up1.exists())
            self.assertTrue(f_up2.exists())

            # Verify remaining auto backups <= 18 (14 daily + up to 4 weekly)
            remaining_auto = list(test_backup_dir.glob("*_auto.db"))
            self.assertLessEqual(len(remaining_auto), 18)

            # Verify remaining pre_* backups == 5
            remaining_pre = list(test_backup_dir.glob("*_pre_*.db"))
            self.assertEqual(len(remaining_pre), 5)
        finally:
            import shutil
            shutil.rmtree(test_backup_dir, ignore_errors=True)

    def test_last_auto_backup_info(self):
        """Verifies calculation of last auto backup time and 26h stale banner detection."""
        test_backup_dir = BACKUP_DIR / "test_stale_info"
        test_backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            # 1. No backup
            info_none = get_last_auto_backup_info(test_backup_dir)
            self.assertIsNone(info_none["last_backup_dt"])
            self.assertTrue(info_none["is_stale_26h"])

            # 2. Fresh backup (1 hour ago)
            fresh_ts = (datetime.now() - timedelta(hours=1)).strftime("%Y%m%d_%H%M%S")
            f_fresh = test_backup_dir / f"hhd_backup_{fresh_ts}_auto.db"
            f_fresh.write_text("dummy")

            info_fresh = get_last_auto_backup_info(test_backup_dir)
            self.assertIsNotNone(info_fresh["last_backup_dt"])
            self.assertFalse(info_fresh["is_stale_26h"])

            # 3. Stale backup (30 hours ago)
            f_fresh.unlink()
            stale_dt = datetime.now() - timedelta(hours=30)
            stale_ts = stale_dt.strftime("%Y%m%d_%H%M%S")
            f_stale = test_backup_dir / f"hhd_backup_{stale_ts}_auto.db"
            f_stale.write_text("dummy")
            os.utime(f_stale, (stale_dt.timestamp(), stale_dt.timestamp()))

            info_stale = get_last_auto_backup_info(test_backup_dir)
            self.assertTrue(info_stale["is_stale_26h"])
        finally:
            import shutil
            shutil.rmtree(test_backup_dir, ignore_errors=True)

    def test_schema_migration_optimization(self):
        """Verifies run_schema_migration does not create redundant backups when schema is current."""
        self.assertTrue(is_schema_current(DB_PATH))
        # Count backups before
        pre_count_before = len(list(BACKUP_DIR.glob("*_pre_migration_upgrade.db")))
        run_schema_migration()
        pre_count_after = len(list(BACKUP_DIR.glob("*_pre_migration_upgrade.db")))
        self.assertEqual(pre_count_before, pre_count_after)

    # -------------------------------------------------------------
    # 2. Wall Board "display" Role & Session Extension
    # -------------------------------------------------------------
    def test_role_display_access_and_sliding_session(self):
        """Display role can access /board and /api/board/live, gets 30-day session, and 403 on other endpoints."""
        db = SessionLocal()
        try:
            disp_user = db.query(User).filter(User.username == "board_display").first()
            if not disp_user:
                disp_user = User(
                    username="board_display",
                    full_name="Warehouse Wall Board",
                    password_hash=hash_password("display123"),
                    role=ROLE_DISPLAY,
                    active=True
                )
                db.add(disp_user)
                db.commit()
                db.refresh(disp_user)
        finally:
            db.close()

        # Login as display user
        login_res = self.client.post(
            "/login",
            data={"username": "board_display", "password": "display123"},
            follow_redirects=False
        )
        self.assertEqual(login_res.status_code, 302)
        self.assertEqual(login_res.headers.get("location"), "/board")

        # Session cookie has ~30 days max-age
        cookie_header = login_res.headers.get("set-cookie", "")
        self.assertIn("Max-Age=2592000", cookie_header)

        # Access /board -> 200 OK
        board_res = self.client.get("/board")
        self.assertEqual(board_res.status_code, 200)
        self.assertIn("Wall Board", board_res.text)

        # Poll /api/board/live -> 200 OK & slides session
        poll_res = self.client.get("/api/board/live")
        self.assertEqual(poll_res.status_code, 200)
        poll_cookie = poll_res.headers.get("set-cookie", "")
        self.assertIn("Max-Age=2592000", poll_cookie)

        # Forbidden on operator station /
        op_res = self.client.get("/")
        self.assertEqual(op_res.status_code, 403)

        # Forbidden on admin routes
        admin_res = self.client.get("/admin")
        self.assertEqual(admin_res.status_code, 403)

    # -------------------------------------------------------------
    # 3. Reset Button Lock After Go-Live
    # -------------------------------------------------------------
    def test_go_live_reset_lock_behavior(self):
        """Go live locks demo reset endpoint with 403; CLI can unlock; blocks reset if any device is issued."""
        token_admin = sign_session({"username": "hhdadmin", "role": ROLE_ADMIN})
        self.client.cookies.set("hhd_session", token_admin)

        db = SessionLocal()
        try:
            # Ensure go_live is false initially
            db.query(SystemSetting).filter(SystemSetting.key == "go_live").delete()
            db.commit()

            # Ensure an employee and device exist to test issued block
            emp = db.query(Employee).first()
            if not emp:
                emp = Employee(employee_id="EMP_GOLIVE_TEST", name="Go Live Worker", department="Warehouse", active=True)
                db.add(emp)
                db.commit()

            dev = db.query(Device).filter(Device.status == "Issued").first()
            if not dev:
                dev = db.query(Device).first()
                if not dev:
                    dev = Device(serial="DEV_GOLIVE_TEST", make="Zebra", model="TC21", status="Available")
                    db.add(dev)
                    db.commit()
            dev.status = "Issued"
            dev.assigned_employee_id = emp.employee_id
            db.commit()

            # Attempt reset while device is Issued -> Redirects with error message
            res_issued_block = self.client.post("/admin/reset", data={"admin_password": "admin12345", "confirmation_text": "CONFIRM RESET"}, follow_redirects=False)
            self.assertEqual(res_issued_block.status_code, 302)
            self.assertIn("error=", res_issued_block.headers.get("location", ""))

            # Now mark device Available
            dev.status = "Available"
            dev.assigned_employee_id = None
            # Also clear all other issued devices for testing go_live lock
            db.query(Device).filter(Device.status == "Issued").update({"status": "Available", "assigned_employee_id": None})
            db.commit()

            # Trigger Go-Live
            res_golive = self.client.post("/admin/reset/go-live", follow_redirects=False)
            self.assertEqual(res_golive.status_code, 302)

            # Setting should now be "true"
            gl_setting = db.query(SystemSetting).filter(SystemSetting.key == "go_live").first()
            self.assertIsNotNone(gl_setting)
            self.assertEqual(gl_setting.value, "true")

            # Attempt reset after go-live -> 403 Forbidden!
            res_locked = self.client.post("/admin/reset", data={"admin_password": "admin12345", "confirmation_text": "CONFIRM RESET"}, follow_redirects=False)
            self.assertEqual(res_locked.status_code, 403)
            self.assertIn("System is LIVE", res_locked.text)

            # Unlock via setting (simulating CLI --unlock-reset)
            gl_setting.value = "false"
            db.commit()

            # Verification that reset page is available again
            res_page = self.client.get("/admin/reset")
            self.assertEqual(res_page.status_code, 200)

        finally:
            # Clean up go_live
            db.query(SystemSetting).filter(SystemSetting.key == "go_live").delete()
            db.commit()
            db.close()

    # -------------------------------------------------------------
    # 4. Max Devices Per Worker (10s Override Scan)
    # -------------------------------------------------------------
    def test_max_devices_per_worker_override(self):
        """When worker holds max devices, first scan warns; second scan within 10s confirms override and logs audit."""
        db = SessionLocal()
        try:
            # Setup worker and 2 test devices
            emp = db.query(Employee).filter(Employee.employee_id == "EMP_OVERRIDE_TEST").first()
            if not emp:
                emp = Employee(employee_id="EMP_OVERRIDE_TEST", name="Override Worker", department="Warehouse", active=True)
                db.add(emp)

            dev1 = db.query(Device).filter(Device.serial == "DEV_OVR_001").first()
            if not dev1:
                dev1 = Device(serial="DEV_OVR_001", make="HONEYWELL", model="EDA50", status="Available")
                db.add(dev1)

            dev2 = db.query(Device).filter(Device.serial == "DEV_OVR_002").first()
            if not dev2:
                dev2 = Device(serial="DEV_OVR_002", make="HONEYWELL", model="EDA50", status="Available")
                db.add(dev2)

            # Set max devices to 1
            m_set = db.query(SystemSetting).filter(SystemSetting.key == "max_devices_per_worker").first()
            if m_set:
                m_set.value = "1"
            else:
                db.add(SystemSetting(key="max_devices_per_worker", value="1", description="Max Devices"))

            # Return any previously issued devices to this worker
            db.query(Device).filter(Device.assigned_employee_id == "EMP_OVERRIDE_TEST").update({"status": "Available", "assigned_employee_id": None})
            db.commit()

            admin_user = db.query(User).filter(User.username == "hhdadmin").first()

            # 1. Issue dev1 to worker (Order A: dev1 first, then worker)
            r1 = process_scan_pipeline(
                db=db, code=dev1.serial, mode="Issue", pending=None, operator=admin_user
            )
            self.assertTrue(r1["ok"])
            self.assertEqual(r1["state"], "pending")

            r2 = process_scan_pipeline(
                db=db, code=emp.employee_id, mode="Issue", pending=r1["pending"], operator=admin_user
            )
            self.assertTrue(r2["ok"])
            self.assertEqual(r2["state"], "done")
            self.assertEqual(r2["action"], "Issue")

            # Worker now holds 1 device. dev2 scan starts:
            r3 = process_scan_pipeline(
                db=db, code=dev2.serial, mode="Issue", pending=None, operator=admin_user
            )
            self.assertTrue(r3["ok"])
            self.assertEqual(r3["state"], "pending")

            # Scan worker for dev2: should trigger warning!
            r4 = process_scan_pipeline(
                db=db, code=emp.employee_id, mode="Issue", pending=r3["pending"], operator=admin_user
            )
            self.assertFalse(r4["ok"])
            self.assertEqual(r4["title"], "Max Devices Limit")
            self.assertIn("already holds device", r4["message"])
            self.assertIsNotNone(r4.get("pending"))
            self.assertEqual(r4["pending"].get("warn_emp"), emp.employee_id)

            # Second scan of worker within 10s: should override and succeed!
            r5 = process_scan_pipeline(
                db=db, code=emp.employee_id, mode="Issue", pending=r4["pending"], operator=admin_user
            )
            self.assertTrue(r5["ok"])
            self.assertEqual(r5["state"], "done")
            self.assertEqual(r5["action"], "Issue")

            # Check audit log for MAX_DEVICES_OVERRIDE
            log = db.query(AuditLog).filter(
                AuditLog.action == "MAX_DEVICES_OVERRIDE",
                AuditLog.target_id == emp.employee_id
            ).first()
            self.assertIsNotNone(log)
            self.assertIn("overrode max devices", log.details)

        finally:
            # Clean up
            db.query(Device).filter(Device.serial.in_(["DEV_OVR_001", "DEV_OVR_002"])).update({"status": "Available", "assigned_employee_id": None})
            db.commit()
            db.close()

    # -------------------------------------------------------------
    # 5. Login Lockout Pair (Username, IP) & IP-Only Limits
    # -------------------------------------------------------------
    def test_login_lockout_pair_isolation(self):
        """5 failed attempts on (userA, ip1) locks that pair, but leaves (userB, ip1) and (userA, ip2) free."""
        login_limiter.clear_all()

        # 5 failures for user1 from IP 192.168.1.10
        for _ in range(5):
            login_limiter.record_failure_pair("user1", "192.168.1.10")

        # user1 from 192.168.1.10 must be locked out
        locked, rem, reason = login_limiter.is_locked_out_pair("user1", "192.168.1.10")
        self.assertTrue(locked)
        self.assertEqual(reason, "pair")

        # user2 from same IP 192.168.1.10 must NOT be locked out
        locked2, _, _ = login_limiter.is_locked_out_pair("user2", "192.168.1.10")
        self.assertFalse(locked2)

        # user1 from different IP 192.168.1.20 must NOT be locked out
        locked3, _, _ = login_limiter.is_locked_out_pair("user1", "192.168.1.20")
        self.assertFalse(locked3)

    # -------------------------------------------------------------
    # 6. Excel Employee Import Blank Department Row Warning
    # -------------------------------------------------------------
    def test_employee_import_blank_department_warning(self):
        """Blank Department is defaulted to Warehouse, counted in summary, and flagged with row warning."""
        csv_content = (
            "Employee ID,Name,Department,Status\n"
            "EMP_WARN_01,Alice Smith,,Active\n"
            "EMP_WARN_02,Bob Jones,Logistics,Active\n"
        ).encode("utf-8")

        db = SessionLocal()
        try:
            preview = validate_employee_import(db, csv_content, "test_workers.csv")
            self.assertTrue(preview["ok"])
            self.assertEqual(preview["valid_count"], 2)
            self.assertEqual(preview["warning_count"], 1)

            row1 = preview["valid_rows"][0]
            self.assertEqual(row1["employee_id"], "EMP_WARN_01")
            self.assertEqual(row1["department"], "Warehouse")
            self.assertEqual(row1["warning"], "Department blank - will be set to Warehouse")

            row2 = preview["valid_rows"][1]
            self.assertEqual(row2["employee_id"], "EMP_WARN_02")
            self.assertEqual(row2["department"], "Logistics")
            self.assertIsNone(row2.get("warning"))
        finally:
            db.close()

if __name__ == "__main__":
    unittest.main()
