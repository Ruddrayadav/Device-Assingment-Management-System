import os
import sys
import time
import unittest
import threading
from datetime import datetime, date, timedelta, time as dtime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import Base, SCAN_WRITE_LOCK
from app.models import (
    User, Employee, Device, DeviceAssignment, Transaction,
    DamageReport, ShiftLog
)
from app.auth import hash_password, verify_password, sign_session, decode_session
from app.scan_engine import (
    issue_device_atomic, return_device_atomic, process_scan_pipeline,
    ScanException, resolve_barcode
)
from app.shifts import (
    get_current_shift_and_op_date, get_operational_date_for_shift,
    calculate_shift_reconciliation
)
from app.reports import generate_excel_report
from app.backup import create_safe_backup, verify_backup_integrity, restore_backup
from app.config import BACKUP_DIR, DATA_DIR

# In-memory thread-safe SQLite engine for tests
TEST_ENGINE = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)

class HHDSystemTestSuite(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=TEST_ENGINE)
        db = TestSession()

        now = datetime.now()
        admin = User(
            username="admin_test",
            password_hash=hash_password("admin_pass_123"),
            full_name="Admin Test",
            role="admin",
            default_shift="Morning",
            active=True,
            created_at=now,
            updated_at=now
        )
        op1 = User(
            username="op1_test",
            password_hash=hash_password("op1_pass_123"),
            full_name="Operator One",
            role="operator",
            default_shift="Morning",
            active=True,
            created_at=now,
            updated_at=now
        )
        op2 = User(
            username="op2_test",
            password_hash=hash_password("op2_pass_123"),
            full_name="Operator Two",
            role="operator",
            default_shift="Evening",
            active=True,
            created_at=now,
            updated_at=now
        )
        db.add_all([admin, op1, op2])

        # Seed test employees
        emps = [
            ("E101", "Ajay Kumar", "IB Puter", True),
            ("E102", "Sonu", "IB Puter", True),
            ("E103", "Rajkumar", "IB Receiver", True),
            ("E104", "Sunny", "RTO Puter", True),
            ("E999", "Inactive Worker", "Dispatch", False),
        ]
        for eid, name, dept, act in emps:
            db.add(Employee(
                employee_id=eid, name=name, department=dept,
                active=act, created_at=now, updated_at=now
            ))

        # Seed test devices
        devs = [
            ("DEV001", "CHAINWAY", "HHD", "Available", "Working"),
            ("DEV002", "CHAINWAY", "HHD", "Available", "Working"),
            ("DEV003", "CHAINWAY", "HHD", "Available", "Working"),
            ("DEV004", "CHAINWAY", "HHD", "Damaged", "Broken screen"),
            ("DEV005", "CHAINWAY", "HHD", "Under Repair", "Under Repair"),
        ]
        for s, make, model, status, cond in devs:
            db.add(Device(
                serial=s, make=make, model=model, cover_qty=2,
                status=status, condition=cond, created_at=now, updated_at=now
            ))

        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=TEST_ENGINE)

    # 1. AUTHENTICATION & SECURITY
    def test_password_hashing(self):
        pwd = "secret_password_123"
        hashed = hash_password(pwd)
        self.assertTrue(verify_password(pwd, hashed))
        self.assertFalse(verify_password("wrong_password", hashed))

    def test_session_signing(self):
        data = {"username": "op1_test", "role": "operator"}
        token = sign_session(data, max_age_seconds=10)
        decoded = decode_session(token)
        self.assertEqual(decoded["username"], "op1_test")
        self.assertEqual(decoded["role"], "operator")

        tampered = token[:-4] + "abcd"
        self.assertIsNone(decode_session(tampered))

    # 2. ZERO-ENTER CONTINUOUS SCANNING PIPELINE
    def test_issue_flow_employee_first(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()

        # Step 1: Scan employee
        res1 = process_scan_pipeline(db, code="E101", mode="Issue", pending=None, operator=op1)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["state"], "pending")
        self.assertEqual(res1["pending"]["type"], "employee")
        self.assertEqual(res1["pending"]["code"], "E101")

        # Step 2: Scan device
        res2 = process_scan_pipeline(db, code="DEV001", mode="Issue", pending=res1["pending"], operator=op1)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["state"], "done")
        self.assertEqual(res2["action"], "Issue")
        self.assertEqual(res2["device"]["serial"], "DEV001")
        self.assertEqual(res2["employee"]["id"], "E101")

        dev = db.query(Device).filter(Device.serial == "DEV001").first()
        self.assertEqual(dev.status, "Issued")
        self.assertEqual(dev.assigned_employee_id, "E101")
        db.close()

    def test_issue_flow_device_first(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()

        # Step 1: Scan device
        res1 = process_scan_pipeline(db, code="DEV002", mode="Issue", pending=None, operator=op1)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["state"], "pending")
        self.assertEqual(res1["pending"]["type"], "device")
        self.assertEqual(res1["pending"]["code"], "DEV002")

        # Step 2: Scan employee
        res2 = process_scan_pipeline(db, code="E102", mode="Issue", pending=res1["pending"], operator=op1)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["state"], "done")
        self.assertEqual(res2["action"], "Issue")
        self.assertEqual(res2["device"]["serial"], "DEV002")
        self.assertEqual(res2["employee"]["id"], "E102")
        db.close()

    # 3. RETURN WORKFLOW (Automatic, One-Scan)
    def test_return_device_alone(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()

        issue_device_atomic(db, "DEV001", "E101", op1)

        res = process_scan_pipeline(db, code="DEV001", mode="Return", pending=None, operator=op1)
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"], "done")
        self.assertEqual(res["action"], "Return")
        self.assertEqual(res["employee"]["id"], "E101")
        self.assertEqual(res["employee"]["name"], "Ajay Kumar")

        dev = db.query(Device).filter(Device.serial == "DEV001").first()
        self.assertEqual(dev.status, "Available")
        self.assertIsNone(dev.assigned_employee_id)
        db.close()

    def test_return_already_available_device_rejected(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()

        res = process_scan_pipeline(db, code="DEV003", mode="Return", pending=None, operator=op1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "error")
        self.assertIn("NOT ISSUED", res["message"])
        db.close()

    # 4. DAMAGE WORKFLOW
    def test_cannot_issue_damaged_or_repair_device(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()

        # Damaged
        res = process_scan_pipeline(db, code="DEV004", mode="Issue", pending={"type": "employee", "code": "E101"}, operator=op1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "error")
        self.assertIn("cannot be issued", res["message"])

        # Under Repair
        res2 = process_scan_pipeline(db, code="DEV005", mode="Issue", pending={"type": "employee", "code": "E101"}, operator=op1)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["state"], "error")
        self.assertIn("cannot be issued", res2["message"])
        db.close()

    def test_damaged_return_quarantines_device(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()

        issue_device_atomic(db, "DEV001", "E101", op1)

        res = return_device_atomic(
            db, "DEV001", op1, condition="Damaged",
            damage_type="Broken screen", remarks="Dropped on conveyor belt"
        )
        self.assertEqual(res["action"], "Return")
        self.assertEqual(res["condition"], "Broken screen")

        dev = db.query(Device).filter(Device.serial == "DEV001").first()
        self.assertEqual(dev.status, "Damaged")
        self.assertEqual(dev.condition, "Broken screen")

        dmg = db.query(DamageReport).filter(DamageReport.serial == "DEV001").first()
        self.assertIsNotNone(dmg)
        self.assertEqual(dmg.fault_type, "Broken screen")
        self.assertEqual(dmg.review_status, "Open")
        db.close()

    # 5. EDGE CASES
    def test_unknown_worker_barcode(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()
        res = process_scan_pipeline(db, code="UNKNOWN_BADGE_999", mode="Issue", pending=None, operator=op1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "unknown")
        db.close()

    def test_unknown_device_barcode(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()
        res = process_scan_pipeline(db, code="UNKNOWN_DEV_SERIAL", mode="Issue", pending=None, operator=op1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "unknown")
        db.close()

    def test_inactive_worker_rejected(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()
        res = process_scan_pipeline(db, code="E999", mode="Issue", pending=None, operator=op1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "error")
        self.assertIn("INACTIVE", res["message"])
        db.close()

    # 6. CONCURRENCY: SIMULTANEOUS OPERATORS (RACE CONDITION PREVENTION)
    def test_two_operators_simultaneous_same_device(self):
        db1 = TestSession()
        db2 = TestSession()
        op1 = db1.query(User).filter(User.username == "op1_test").first()
        op2 = db2.query(User).filter(User.username == "op2_test").first()

        results = []
        errors = []

        def op1_work():
            try:
                r = issue_device_atomic(db1, "DEV003", "E101", op1)
                results.append(("op1", r))
            except Exception as e:
                errors.append(("op1", str(e)))

        def op2_work():
            try:
                r = issue_device_atomic(db2, "DEV003", "E102", op2)
                results.append(("op2", r))
            except Exception as e:
                errors.append(("op2", str(e)))

        t1 = threading.Thread(target=op1_work)
        t2 = threading.Thread(target=op2_work)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("ALREADY ISSUED", errors[0][1])

        dev = db1.query(Device).filter(Device.serial == "DEV003").first()
        self.assertEqual(dev.status, "Issued")
        self.assertIn(dev.assigned_employee_id, ["E101", "E102"])

        db1.close()
        db2.close()

    def test_two_operators_different_devices_concurrent(self):
        db1 = TestSession()
        db2 = TestSession()
        op1 = db1.query(User).filter(User.username == "op1_test").first()
        op2 = db2.query(User).filter(User.username == "op2_test").first()

        results = []
        errors = []

        def op1_work():
            try:
                r = issue_device_atomic(db1, "DEV001", "E101", op1)
                results.append(("op1", r))
            except Exception as e:
                errors.append(("op1", str(e)))

        def op2_work():
            try:
                r = issue_device_atomic(db2, "DEV002", "E102", op2)
                results.append(("op2", r))
            except Exception as e:
                errors.append(("op2", str(e)))

        t1 = threading.Thread(target=op1_work)
        t2 = threading.Thread(target=op2_work)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(len(errors), 0)

        db1.close()
        db2.close()

    # 7. SHIFT RECONCILIATION & MIDNIGHT CROSSING
    def test_midnight_crossing_shift_date(self):
        dt1 = datetime(2026, 10, 30, 23, 30, 0)
        shift1, op1 = get_current_shift_and_op_date(dt1)
        self.assertEqual(shift1, "Night")
        self.assertEqual(op1, "2026-10-30")

        # Past midnight (02:15 on Oct 31) -> Must belong to yesterday's date (2026-10-30)!
        dt2 = datetime(2026, 10, 31, 2, 15, 0)
        shift2, op2 = get_current_shift_and_op_date(dt2)
        self.assertEqual(shift2, "Night")
        self.assertEqual(op2, "2026-10-30")

        # 08:30 on Oct 31 -> Morning shift on Oct 31
        dt3 = datetime(2026, 10, 31, 8, 30, 0)
        shift3, op3 = get_current_shift_and_op_date(dt3)
        self.assertEqual(shift3, "Morning")
        self.assertEqual(op3, "2026-10-31")

    def test_shift_reconciliation_math(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()
        target_date = "2026-10-30"
        target_shift = "Morning"

        issue_device_atomic(db, "DEV001", "E101", op1, active_shift=target_shift)
        issue_device_atomic(db, "DEV002", "E102", op1, active_shift=target_shift)
        issue_device_atomic(db, "DEV003", "E103", op1, active_shift=target_shift)

        return_device_atomic(db, "DEV001", op1, condition="Working", active_shift=target_shift)
        return_device_atomic(db, "DEV002", op1, condition="Damaged", damage_type="Blank screen", active_shift=target_shift)

        recon = calculate_shift_reconciliation(db, target_date, target_shift)
        self.assertEqual(recon["total_issued"], 3)
        self.assertEqual(recon["total_returned"], 2)
        self.assertEqual(recon["working_returned"], 1)
        self.assertEqual(recon["damaged_returned"], 1)
        self.assertEqual(recon["outstanding_count"], 1)
        self.assertEqual(recon["discrepancy"], 1)
        self.assertEqual(len(recon["outstanding_devices"]), 1)
        self.assertEqual(recon["outstanding_devices"][0]["serial"], "DEV003")
        self.assertEqual(recon["outstanding_devices"][0]["employee_id"], "E103")
        db.close()

    # 8. EXCEL REPORT GENERATION (5 SHEETS)
    def test_excel_5_sheets_export(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()
        issue_device_atomic(db, "DEV001", "E101", op1)

        excel_buffer = generate_excel_report(db)
        self.assertIsNotNone(excel_buffer)
        self.assertGreater(excel_buffer.getbuffer().nbytes, 1000)

        import openpyxl
        wb = openpyxl.load_workbook(excel_buffer)
        expected_sheets = ["Daily Summary", "Transaction Details", "Damage Reports", "Outstanding Devices", "Shift Summary"]
        self.assertEqual(wb.sheetnames, expected_sheets)
        db.close()

    # 9. PERFORMANCE: 100 CONSECUTIVE SCANS LATENCY
    def test_100_consecutive_scans_latency(self):
        db = TestSession()
        op1 = db.query(User).filter(User.username == "op1_test").first()

        start_time = time.time()
        for i in range(50):
            issue_device_atomic(db, "DEV001", "E101", op1)
            return_device_atomic(db, "DEV001", op1, condition="Working")

        total_time = time.time() - start_time
        avg_latency_ms = (total_time / 100) * 1000

        print(f"\n[BENCHMARK] 100 Consecutive Scans: {total_time:.3f}s total ({avg_latency_ms:.2f}ms/scan)")
        self.assertLess(avg_latency_ms, 100.0)
        db.close()

if __name__ == "__main__":
    unittest.main()
