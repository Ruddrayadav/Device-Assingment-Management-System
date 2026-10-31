import os
import sys
import unittest
import io
import time
import threading
from datetime import datetime, date
from pathlib import Path
import openpyxl

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from fastapi.testclient import TestClient
from main import app, seed_initial_data_if_needed
from app.config import DB_PATH, now_kolkata, today_kolkata_str
from app.database import engine, Base, SessionLocal
from app.models import User, Device, Employee, Transaction, DeviceAssignment, DamageReport, AuditLog
from app.auth import sign_session, hash_password, login_limiter
from app.migration import run_schema_migration
from app.scan_engine import (
    issue_device_atomic, return_device_atomic, process_scan_pipeline,
    resolve_barcode, ScanException
)
from app.excel_importer import (
    validate_device_import, commit_device_import,
    validate_employee_import, commit_employee_import
)
from app.reports import generate_excel_report


class V3HardeningTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_schema_migration()
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()
        self.client = TestClient(app)
        self.admin_token = sign_session({"username": "hhdadmin", "role": "admin"})
        self.op1_token = sign_session({"username": "operator1", "role": "operator"})
        self.op2_token = sign_session({"username": "operator2", "role": "operator"})

    # -------------------------------------------------------------------------
    # 1. IDEMPOTENT SCAN RETRY & DOUBLE-SCAN WORKFLOW
    # -------------------------------------------------------------------------
    def test_idempotent_issue_scan_retry_no_red_error(self):
        """
        When an operator re-scans an already issued device for the same worker,
        the system must return a clean success replay instead of throwing a red
        'Already Issued' error or duplicating the database transaction.
        """
        db = SessionLocal()
        try:
            dev = Device(serial="IDEMP_DEV_01", make="CHAINWAY", model="MC95", department="LOG", status="Available")
            emp = Employee(employee_id="IDEMP_EMP_01", name="Idemp Worker", department="LOG", active=True)
            op = db.query(User).filter(User.username == "operator1").first()
            db.merge(dev)
            db.merge(emp)
            db.commit()

            # First scan issue via pipeline
            res1 = process_scan_pipeline(
                db=db,
                code="IDEMP_DEV_01",
                mode="Issue",
                pending={"type": "employee", "code": "IDEMP_EMP_01"},
                operator=op,
                idempotency_key="key_issue_001"
            )
            self.assertTrue(res1.get("ok"))
            self.assertEqual(res1.get("action"), "Issue")

            # Second scan issue (re-scan) via pipeline with same idempotency key
            res2 = process_scan_pipeline(
                db=db,
                code="IDEMP_DEV_01",
                mode="Issue",
                pending={"type": "employee", "code": "IDEMP_EMP_01"},
                operator=op,
                idempotency_key="key_issue_001"
            )
            self.assertTrue(res2.get("ok"))
            self.assertTrue(res2.get("idempotent_replay"))
            self.assertEqual(res2.get("action"), "Issue")

            # Re-scan without idempotency key but same device + employee (physical retry)
            res3 = process_scan_pipeline(
                db=db,
                code="IDEMP_DEV_01",
                mode="Issue",
                pending={"type": "employee", "code": "IDEMP_EMP_01"},
                operator=op
            )
            self.assertTrue(res3.get("ok"))
            self.assertTrue(res3.get("idempotent_replay"))

            # Verify no duplicate transaction was created
            tx_count = db.query(Transaction).filter(
                Transaction.serial == "IDEMP_DEV_01",
                Transaction.employee_id == "IDEMP_EMP_01",
                Transaction.transaction_type == "Issue"
            ).count()
            self.assertEqual(tx_count, 1)
        finally:
            db.close()

    def test_idempotent_return_scan_retry(self):
        """
        When returning a device, repeating the return scan returns clean replay
        success and does not create duplicate return transactions.
        """
        db = SessionLocal()
        try:
            dev = Device(serial="IDEMP_DEV_02", make="CHAINWAY", model="MC95", department="LOG", status="Available")
            emp = Employee(employee_id="IDEMP_EMP_02", name="Idemp Worker 2", department="LOG", active=True)
            op = db.query(User).filter(User.username == "operator1").first()
            db.merge(dev)
            db.merge(emp)
            db.commit()

            # Issue first
            issue_device_atomic(db, "IDEMP_DEV_02", "IDEMP_EMP_02", op)

            # First return via pipeline
            ret1 = process_scan_pipeline(
                db=db,
                code="IDEMP_DEV_02",
                mode="Return",
                pending=None,
                operator=op,
                idempotency_key="key_ret_001"
            )
            self.assertTrue(ret1.get("ok"))
            self.assertEqual(ret1.get("action"), "Return")

            # Second return (re-scan)
            ret2 = process_scan_pipeline(
                db=db,
                code="IDEMP_DEV_02",
                mode="Return",
                pending=None,
                operator=op,
                idempotency_key="key_ret_001"
            )
            self.assertTrue(ret2.get("ok"))
            self.assertTrue(ret2.get("idempotent_replay"))

            # Re-scan without key
            ret3 = process_scan_pipeline(
                db=db,
                code="IDEMP_DEV_02",
                mode="Return",
                pending=None,
                operator=op
            )
            self.assertTrue(ret3.get("ok"))
            self.assertTrue(ret3.get("idempotent_replay"))

            ret_tx_count = db.query(Transaction).filter(
                Transaction.serial == "IDEMP_DEV_02",
                Transaction.transaction_type == "Return"
            ).count()
            self.assertEqual(ret_tx_count, 1)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 2. BARCODE RESOLUTION & AMBIGUITY COLLISION PROTECTION
    # -------------------------------------------------------------------------
    def test_barcode_exact_match_prioritization_and_collision_error(self):
        """
        Resolve barcode must prioritize exact matches. If a barcode matches BOTH
        an Employee and a Device, it must raise ScanException('Ambiguous Barcode Collision').
        """
        db = SessionLocal()
        try:
            # 1. Suffix test: device serial 'DEV_LONG_12345' and employee ID '12345'
            dev = Device(serial="DEV_LONG_12345", make="CHAINWAY", model="MC95", department="LOG", status="Available")
            emp = Employee(employee_id="12345", name="Suffix Worker", department="LOG", active=True)
            db.merge(dev)
            db.merge(emp)
            db.commit()

            # Scanning '12345' must resolve strictly to the Employee (exact match), NEVER suffix match to DEV_LONG_12345
            dev_res, emp_res = resolve_barcode(db, "12345")
            self.assertIsNone(dev_res)
            self.assertIsNotNone(emp_res)
            self.assertEqual(emp_res.employee_id, "12345")

            # 2. True Ambiguity Collision: employee and device with the EXACT same code
            collision_code = "COLLISION_CODE_999"
            dev_col = Device(serial=collision_code, make="CHAINWAY", model="MC95", department="LOG", status="Available")
            emp_col = Employee(employee_id=collision_code, name="Collision Worker", department="LOG", active=True)
            db.merge(dev_col)
            db.merge(emp_col)
            db.commit()

            with self.assertRaises(ScanException) as ctx:
                resolve_barcode(db, collision_code)
            self.assertEqual(ctx.exception.title, "Ambiguous Barcode")
            self.assertEqual(ctx.exception.code, "AMBIGUOUS_BARCODE")
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 3. OPERATOR PERSISTENCE ACROSS SERVER RESTARTS
    # -------------------------------------------------------------------------
    def test_operator_daily_counters_restored_from_db(self):
        """
        Operator station page (/ or /station) must compute My Issued and My Returned
        from the database for the current calendar date and operator user ID.
        """
        db = SessionLocal()
        try:
            op = db.query(User).filter(User.username == "operator1").first()
            today_str = today_kolkata_str()
            now_dt = now_kolkata()

            # First create foreign key entities
            p_emp = Employee(employee_id="P_EMP", name="Persist Worker", department="LOG", active=True)
            db.merge(p_emp)
            for i in range(3):
                p_dev = Device(serial=f"PERSIST_D_{i}", make="CHAINWAY", model="MC95", department="LOG", status="Available")
                db.merge(p_dev)
            db.commit()

            # Record 3 issues and 2 returns today for operator1
            for i in range(3):
                tx = Transaction(
                    transaction_time=now_dt,
                    transaction_date=today_str,
                    transaction_type="Issue",
                    serial=f"PERSIST_D_{i}",
                    employee_id="P_EMP",
                    employee_name="Persist Worker",
                    department="LOG",
                    operator_id=op.id,
                    operator_name=op.full_name or op.username,
                    condition="Working",
                    cover_qty=0
                )
                db.add(tx)
            for i in range(2):
                tx = Transaction(
                    transaction_time=now_dt,
                    transaction_date=today_str,
                    transaction_type="Return",
                    serial=f"PERSIST_D_{i}",
                    employee_id="P_EMP",
                    employee_name="Persist Worker",
                    department="LOG",
                    operator_id=op.id,
                    operator_name=op.full_name or op.username,
                    condition="Working",
                    cover_qty=0
                )
                db.add(tx)
            db.commit()

            # Operator loads page in new session
            self.client.cookies.set("hhd_session", self.op1_token)
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 200)
            html = resp.text

            expected_issued = db.query(Transaction).filter(
                Transaction.operator_id == op.id,
                Transaction.transaction_date == today_str,
                Transaction.transaction_type == "Issue"
            ).count()
            expected_returned = db.query(Transaction).filter(
                Transaction.operator_id == op.id,
                Transaction.transaction_date == today_str,
                Transaction.transaction_type == "Return"
            ).count()

            # Check that op_issued and op_returned counters are present in the HTML
            self.assertIn('id="stat-op-issued"', html)
            self.assertIn('id="stat-op-returned"', html)
            self.assertIn(f'>{expected_issued}</div>', html)
            self.assertIn(f'>{expected_returned}</div>', html)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 4. EXCEL IMPORT REMARKS & EMPLOYEE STATUS MAPPING WITH DAMAGE AUTOMATION
    # -------------------------------------------------------------------------
    def test_device_import_remarks_and_damage_ticket_automation(self):
        """
        Verify:
        - 'Working' / 'Available' -> Available
        - 'Not Working' -> Damaged + DamageReport(Pending Inspection)
        - 'Under Repair' -> Under Repair + DamageReport(Pending Inspection)
        - Re-importing does not create duplicate open damage tickets.
        """
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Device Inventory"
        ws.append(["Sr.No.", "Make", "Model No", "Serial Number", "Department", "Remarks", "Date"])
        ws.append([1, "CHAINWAY", "MC95", "TEST_AUTO_AVAIL", "LOGISTICS", "Working", "2026-10-31"])
        ws.append([2, "CHAINWAY", "MC95", "TEST_AUTO_DMG", "LOGISTICS", "Not Working", "2026-10-31"])
        ws.append([3, "CHAINWAY", "MC95", "TEST_AUTO_REP", "LOGISTICS", "Under Repair", "2026-10-31"])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        file_bytes = buf.getvalue()

        db = SessionLocal()
        try:
            # 1. Validation
            val = validate_device_import(db, file_bytes, "devices.xlsx")
            self.assertTrue(val["ok"])
            self.assertEqual(len(val["valid_rows"]), 3)

            # 2. First Commit
            res1 = commit_device_import(db, val["valid_rows"], mode="upsert")
            self.assertEqual(res1["total_imported"], 3)

            dev_avail = db.query(Device).filter(Device.serial == "TEST_AUTO_AVAIL").first()
            dev_dmg = db.query(Device).filter(Device.serial == "TEST_AUTO_DMG").first()
            dev_rep = db.query(Device).filter(Device.serial == "TEST_AUTO_REP").first()

            self.assertEqual(dev_avail.status, "Available")
            self.assertEqual(dev_dmg.status, "Damaged")
            self.assertEqual(dev_rep.status, "Under Repair")

            # Check DamageReport created for Damaged & Under Repair
            dmg_rep_count = db.query(DamageReport).filter(DamageReport.serial == "TEST_AUTO_DMG").count()
            rep_rep_count = db.query(DamageReport).filter(DamageReport.serial == "TEST_AUTO_REP").count()
            avail_rep_count = db.query(DamageReport).filter(DamageReport.serial == "TEST_AUTO_AVAIL").count()

            self.assertEqual(dmg_rep_count, 1)
            self.assertEqual(rep_rep_count, 1)
            self.assertEqual(avail_rep_count, 0)

            # 3. Second Commit (re-import) -> Must NOT duplicate open damage reports!
            res2 = commit_device_import(db, val["valid_rows"], mode="upsert")
            dmg_rep_count_after = db.query(DamageReport).filter(DamageReport.serial == "TEST_AUTO_DMG").count()
            self.assertEqual(dmg_rep_count_after, 1)
        finally:
            db.close()

    def test_employee_import_status_mapping_and_rejection(self):
        """
        Verify:
        - 'Active' -> True
        - 'Resigned' / 'Left' / 'Terminated' / 'Inactive' -> False
        - Blank or invalid status -> validation error
        """
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Employee Master"
        ws.append(["Sr.No.", "Employee ID", "Employee Name", "Department", "Status"])
        ws.append([1, "EMP_MAP_01", "Worker Active", "LOG", "Active"])
        ws.append([2, "EMP_MAP_02", "Worker Resigned", "LOG", "Resigned"])
        ws.append([3, "EMP_MAP_03", "Worker Left", "LOG", "Left"])
        ws.append([4, "EMP_MAP_04", "Worker Terminated", "LOG", "Terminated"])
        ws.append([5, "EMP_MAP_05", "Worker Inactive", "LOG", "Inactive"])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        db = SessionLocal()
        try:
            val = validate_employee_import(db, buf.getvalue(), "employees.xlsx")
            self.assertTrue(val["ok"])
            self.assertEqual(len(val["valid_rows"]), 5)

            commit_employee_import(db, val["valid_rows"], mode="upsert")
            e1 = db.query(Employee).filter(Employee.employee_id == "EMP_MAP_01").first()
            e2 = db.query(Employee).filter(Employee.employee_id == "EMP_MAP_02").first()
            e3 = db.query(Employee).filter(Employee.employee_id == "EMP_MAP_03").first()
            e4 = db.query(Employee).filter(Employee.employee_id == "EMP_MAP_04").first()
            e5 = db.query(Employee).filter(Employee.employee_id == "EMP_MAP_05").first()

            self.assertTrue(e1.active)
            self.assertFalse(e2.active)
            self.assertFalse(e3.active)
            self.assertFalse(e4.active)
            self.assertFalse(e5.active)

            # Test invalid status rejection
            wb_invalid = openpyxl.Workbook()
            ws_inv = wb_invalid.active
            ws_inv.append(["Sr.No.", "Employee ID", "Employee Name", "Department", "Status"])
            ws_inv.append([1, "EMP_INV_01", "Worker Bad Status", "LOG", "UnknownStatusXYZ"])
            buf_inv = io.BytesIO()
            wb_invalid.save(buf_inv)
            buf_inv.seek(0)

            val_inv = validate_employee_import(db, buf_inv.getvalue(), "bad_employees.xlsx")
            self.assertGreater(len(val_inv["rejected_rows"]), 0)
            self.assertIn("Unknown employee status", val_inv["rejected_rows"][0]["reason"])
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 5. SECURITY HARDENING: BRUTE FORCE LOCKOUT & ADMIN UNLOCK
    # -------------------------------------------------------------------------
    def test_brute_force_rate_limiter_and_admin_unlock(self):
        """
        5 consecutive failed logins triggers temporary lockout.
        Admin can unlock account immediately via /admin/users/{id}/unlock.
        """
        test_uname = "lockout_victim"
        login_limiter.unlock_user(test_uname)

        # 1. Perform 5 failed attempts
        for i in range(5):
            login_limiter.record_failure(test_uname)

        # 2. Account must be locked out
        is_locked, rem = login_limiter.is_locked_out(test_uname)
        self.assertTrue(is_locked)
        self.assertGreater(rem, 0)

        # 3. Create victim user in DB and test admin unlock endpoint
        db = SessionLocal()
        try:
            victim = User(
                username=test_uname,
                password_hash=hash_password("password123"),
                full_name="Victim User",
                role="operator",
                active=True,
                created_at=datetime.now(),
                updated_at=datetime.now()
            )
            db.add(victim)
            db.commit()
            victim_id = victim.id

            # Admin unlocks via POST /admin/users/{user_id}/unlock
            self.client.cookies.set("hhd_session", self.admin_token)
            resp = self.client.post(f"/admin/users/{victim_id}/unlock", follow_redirects=False)
            self.assertEqual(resp.status_code, 302)

            # Must be unlocked now
            is_locked_after, _ = login_limiter.is_locked_out(test_uname)
            self.assertFalse(is_locked_after)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 6. SECURITY: RBAC ENFORCEMENT & SECURITY HEADERS
    # -------------------------------------------------------------------------
    def test_rbac_operator_forbidden_from_admin_and_reports(self):
        """
        Operators must receive HTTP 403 Forbidden when requesting admin pages
        or downloading privileged Excel/CSV reports.
        """
        self.client.cookies.set("hhd_session", self.op1_token)

        # Admin panel
        resp_admin = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(resp_admin.status_code, 403)

        # Admin users
        resp_users = self.client.get("/admin/users", follow_redirects=False)
        self.assertEqual(resp_users.status_code, 403)

        # Reports download
        resp_rep = self.client.get("/reports/excel", follow_redirects=False)
        self.assertEqual(resp_rep.status_code, 403)

        resp_csv = self.client.get("/reports/csv/devices", follow_redirects=False)
        self.assertEqual(resp_csv.status_code, 403)

    def test_security_headers_present(self):
        """
        All HTTP responses must carry Content-Security-Policy, nosniff, DENY, and strict-origin.
        """
        resp = self.client.get("/login")
        self.assertIn("Content-Security-Policy", resp.headers)
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")

    def test_unauthenticated_board_access_blocked(self):
        """
        Live board and /api/board/live must not be viewable by unauthenticated users.
        """
        self.client.cookies.clear()
        resp_page = self.client.get("/board", follow_redirects=False)
        self.assertEqual(resp_page.status_code, 302)
        self.assertIn("/login", resp_page.headers.get("Location", ""))

        resp_api = self.client.get("/api/board/live", follow_redirects=False)
        self.assertEqual(resp_api.status_code, 401)

    # -------------------------------------------------------------------------
    # 7. OUTSTANDING DEVICES & MARK MISSING WORKFLOW
    # -------------------------------------------------------------------------
    def test_outstanding_devices_and_mark_missing(self):
        """
        Test /admin/outstanding listing and /admin/devices/{serial}/mark-missing POST.
        """
        db = SessionLocal()
        try:
            dev = Device(serial="OUTST_DEV_01", make="CHAINWAY", model="MC95", department="LOG", status="Available")
            emp = Employee(employee_id="OUTST_EMP_01", name="Outst Worker", department="LOG", active=True)
            op = db.query(User).filter(User.username == "operator1").first()
            db.merge(dev)
            db.merge(emp)
            db.commit()

            # Issue device
            issue_device_atomic(db, "OUTST_DEV_01", "OUTST_EMP_01", op)

            # Admin checks outstanding page
            self.client.cookies.set("hhd_session", self.admin_token)
            resp_out = self.client.get("/admin/outstanding")
            self.assertEqual(resp_out.status_code, 200)
            self.assertIn("OUTST_DEV_01", resp_out.text)
            self.assertIn("Outst Worker", resp_out.text)

            # Mark Missing
            resp_miss = self.client.post(
                "/admin/devices/OUTST_DEV_01/mark-missing",
                data={"notes": "Device lost in zone B"},
                follow_redirects=False
            )
            self.assertEqual(resp_miss.status_code, 302)

            # Verify device status is 'Missing' and assignment is 'RETURNED'
            db.expire_all()
            dev_updated = db.query(Device).filter(Device.serial == "OUTST_DEV_01").first()
            self.assertEqual(dev_updated.status, "Missing")
            self.assertEqual(dev_updated.condition, "Missing")

            asgn = db.query(DeviceAssignment).filter(
                DeviceAssignment.device_serial == "OUTST_DEV_01"
            ).order_by(DeviceAssignment.id.desc()).first()
            self.assertEqual(asgn.status, "RETURNED")
            self.assertEqual(asgn.condition_on_return, "Missing")

            # Verify audit log created
            audit = db.query(AuditLog).filter(
                AuditLog.action == "MARK_DEVICE_MISSING",
                AuditLog.target_id == "OUTST_DEV_01"
            ).first()
            self.assertIsNotNone(audit)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 8. AUDIT PAGINATION
    # -------------------------------------------------------------------------
    def test_audit_pagination_and_query_preservation(self):
        """
        Verify server-side pagination returns page metadata and correct subsets.
        """
        self.client.cookies.set("hhd_session", self.admin_token)
        resp = self.client.get("/admin/transactions?page=1&page_size=25")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Showing", resp.text)
        self.assertIn("transactions (Page 1 of", resp.text)


if __name__ == "__main__":
    unittest.main()
