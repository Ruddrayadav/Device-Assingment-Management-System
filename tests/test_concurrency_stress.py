import os
import sys
import unittest
import io
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
import openpyxl

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from main import app, seed_initial_data_if_needed
from app.config import DB_PATH, now_kolkata, today_kolkata_str
from app.database import engine, Base, SessionLocal, SCAN_WRITE_LOCK
from app.models import User, Device, Employee, Transaction, DeviceAssignment, DamageReport, AuditLog
from app.migration import run_schema_migration
from app.scan_engine import (
    issue_device_atomic, return_device_atomic, process_scan_pipeline,
    ScanException
)
from app.excel_importer import (
    validate_device_import, commit_device_import
)
from app.reports import generate_excel_report


class ConcurrencyAndStressTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_schema_migration()
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()

    # -------------------------------------------------------------------------
    # 1. TWO OPERATORS ISSUING DIFFERENT DEVICES SIMULTANEOUSLY
    # -------------------------------------------------------------------------
    def test_two_operators_concurrent_scanning(self):
        """
        Two operators issuing distinct devices concurrently.
        Must complete cleanly without database contention or lost updates.
        """
        db = SessionLocal()
        try:
            for i in range(20):
                db.merge(Employee(employee_id=f"OP2_E_{i:02d}", name=f"Worker OP2 {i}", department="LOG", active=True))
                db.merge(Device(serial=f"OP2_D_{i:02d}", make="CHAINWAY", model="MC95", department="LOG", status="Available"))
            db.commit()
            op1 = db.query(User).filter(User.username == "operator1").first()
            op2 = db.query(User).filter(User.username == "operator2").first()
        finally:
            db.close()

        errors = []

        def worker_task(op_id, start_idx, end_idx):
            for i in range(start_idx, end_idx):
                session = SessionLocal()
                try:
                    op = session.query(User).filter(User.id == op_id).first()
                    res = process_scan_pipeline(
                        db=session,
                        code=f"OP2_D_{i:02d}",
                        mode="Issue",
                        pending={"type": "employee", "code": f"OP2_E_{i:02d}"},
                        operator=op
                    )
                    if not res.get("ok"):
                        errors.append(f"Op {op_id} failed for index {i}: {res}")
                except Exception as ex:
                    errors.append(f"Op {op_id} exception for index {i}: {ex}")
                finally:
                    session.close()

        t1 = threading.Thread(target=worker_task, args=(op1.id, 0, 10))
        t2 = threading.Thread(target=worker_task, args=(op2.id, 10, 20))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Concurrent scan errors: {errors}")

        db = SessionLocal()
        try:
            issued_count = db.query(Device).filter(Device.serial.like("OP2_D_%"), Device.status == "Issued").count()
            self.assertEqual(issued_count, 20)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 2. FOUR OPERATORS WORKING SIMULTANEOUSLY
    # -------------------------------------------------------------------------
    def test_four_operators_concurrent_scanning(self):
        """
        Four operators issuing 40 distinct devices simultaneously.
        """
        db = SessionLocal()
        try:
            # Create extra operators if needed
            for op_num in [3, 4]:
                if not db.query(User).filter(User.username == f"operator{op_num}").first():
                    db.add(User(
                        username=f"operator{op_num}",
                        password_hash="hash",
                        full_name=f"Operator {op_num}",
                        role="operator",
                        active=True,
                        created_at=datetime.now(),
                        updated_at=datetime.now()
                    ))
            for i in range(40):
                db.merge(Employee(employee_id=f"OP4_E_{i:02d}", name=f"Worker OP4 {i}", department="INV", active=True))
                db.merge(Device(serial=f"OP4_D_{i:02d}", make="ZEBRA", model="TC22", department="INV", status="Available"))
            db.commit()

            ops = [
                db.query(User).filter(User.username == "operator1").first(),
                db.query(User).filter(User.username == "operator2").first(),
                db.query(User).filter(User.username == "operator3").first(),
                db.query(User).filter(User.username == "operator4").first(),
            ]
        finally:
            db.close()

        errors = []

        def worker_op(op_id, start_i, end_i):
            for i in range(start_i, end_i):
                s = SessionLocal()
                try:
                    op = s.query(User).filter(User.id == op_id).first()
                    res = process_scan_pipeline(
                        db=s,
                        code=f"OP4_D_{i:02d}",
                        mode="Issue",
                        pending={"type": "employee", "code": f"OP4_E_{i:02d}"},
                        operator=op
                    )
                    if not res.get("ok"):
                        errors.append(f"Op {op_id} error: {res}")
                except Exception as ex:
                    errors.append(f"Op {op_id} ex: {ex}")
                finally:
                    s.close()

        threads = []
        for idx, op in enumerate(ops):
            t = threading.Thread(target=worker_op, args=(op.id, idx * 10, (idx + 1) * 10))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"4-operator concurrency errors: {errors}")

        db = SessionLocal()
        try:
            issued_cnt = db.query(Device).filter(Device.serial.like("OP4_D_%"), Device.status == "Issued").count()
            self.assertEqual(issued_cnt, 40)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 3. EIGHT SIMULATED OPERATORS (STRESS LOAD)
    # -------------------------------------------------------------------------
    def test_eight_simulated_operators(self):
        """
        Eight operators scanning concurrently (80 total transactions: issues and returns).
        """
        db = SessionLocal()
        try:
            # Seed devices and employees
            for i in range(80):
                db.merge(Employee(employee_id=f"OP8_E_{i:02d}", name=f"Worker OP8 {i}", department="STRESS", active=True))
                db.merge(Device(serial=f"OP8_D_{i:02d}", make="CHAINWAY", model="MC95", department="STRESS", status="Available"))
            db.commit()
            op = db.query(User).filter(User.username == "operator1").first()
            op_id = op.id
        finally:
            db.close()

        errors = []

        def op_worker(start_idx, end_idx):
            for i in range(start_idx, end_idx):
                s = SessionLocal()
                try:
                    u = s.query(User).filter(User.id == op_id).first()
                    # 1. Issue
                    res_issue = process_scan_pipeline(
                        db=s,
                        code=f"OP8_D_{i:02d}",
                        mode="Issue",
                        pending={"type": "employee", "code": f"OP8_E_{i:02d}"},
                        operator=u
                    )
                    if not res_issue.get("ok"):
                        errors.append(f"Issue {i} failed: {res_issue}")

                    # 2. Return
                    res_ret = process_scan_pipeline(
                        db=s,
                        code=f"OP8_D_{i:02d}",
                        mode="Return",
                        pending=None,
                        operator=u
                    )
                    if not res_ret.get("ok"):
                        errors.append(f"Return {i} failed: {res_ret}")
                except Exception as ex:
                    errors.append(f"Thread ex {i}: {ex}")
                finally:
                    s.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(op_worker, k * 10, (k + 1) * 10)
                for k in range(8)
            ]
            for f in futures:
                f.result()

        self.assertEqual(len(errors), 0, f"8-operator stress errors: {errors}")

        db = SessionLocal()
        try:
            # All 80 devices must be safely back in Available status
            avail_cnt = db.query(Device).filter(Device.serial.like("OP8_D_%"), Device.status == "Available").count()
            self.assertEqual(avail_cnt, 80)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 4. RACE CONDITION: TWO OPERATORS ISSUING THE SAME DEVICE AT THE SAME INSTANT
    # -------------------------------------------------------------------------
    def test_race_condition_two_operators_issue_same_device(self):
        """
        Two operators try to issue the EXACT SAME device to two different workers
        at the exact same microsecond using a synchronization Barrier.
        Guarantee:
        - Exactly 1 operator succeeds.
        - Exactly 1 operator is rejected.
        - The device is never assigned to two workers simultaneously.
        """
        db = SessionLocal()
        try:
            target_serial = "RACE_DEV_SAME_01"
            db.merge(Device(serial=target_serial, make="CHAINWAY", model="MC95", department="LOG", status="Available"))
            db.merge(Employee(employee_id="WORKER_RACE_A", name="Worker A", department="LOG", active=True))
            db.merge(Employee(employee_id="WORKER_RACE_B", name="Worker B", department="LOG", active=True))
            db.commit()
            op1 = db.query(User).filter(User.username == "operator1").first()
            op2 = db.query(User).filter(User.username == "operator2").first()
            op1_id = op1.id
            op2_id = op2.id
        finally:
            db.close()

        barrier = threading.Barrier(2)
        results = []

        def try_issue(op_id, emp_id):
            s = SessionLocal()
            try:
                op = s.query(User).filter(User.id == op_id).first()
                # Wait for both threads to hit the barrier at the exact same moment
                barrier.wait()
                res = process_scan_pipeline(
                    db=s,
                    code=target_serial,
                    mode="Issue",
                    pending={"type": "employee", "code": emp_id},
                    operator=op
                )
                results.append((op_id, emp_id, res))
            finally:
                s.close()

        t1 = threading.Thread(target=try_issue, args=(op1_id, "WORKER_RACE_A"))
        t2 = threading.Thread(target=try_issue, args=(op2_id, "WORKER_RACE_B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Analyze results
        successes = [r for r in results if r[2].get("ok") and r[2].get("state") == "done" and not r[2].get("idempotent_replay")]
        failures = [r for r in results if not r[2].get("ok") or r[2].get("state") == "error"]

        # Exactly 1 operator must succeed, and 1 must be rejected
        self.assertEqual(len(successes), 1, f"Expected exactly 1 success, got {len(successes)}. Results: {results}")
        self.assertEqual(len(failures), 1, f"Expected exactly 1 failure, got {len(failures)}. Results: {results}")

        # Verify device state in permanent DB
        db = SessionLocal()
        try:
            dev = db.query(Device).filter(Device.serial == target_serial).first()
            self.assertEqual(dev.status, "Issued")
            self.assertIn(dev.assigned_employee_id, ["WORKER_RACE_A", "WORKER_RACE_B"])

            # Verify active assignments in DB: exactly 1
            asgn_cnt = db.query(DeviceAssignment).filter(
                DeviceAssignment.device_serial == target_serial,
                DeviceAssignment.status == "ACTIVE"
            ).count()
            self.assertEqual(asgn_cnt, 1)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 5. RACE CONDITION: TWO OPERATORS RETURNING THE SAME DEVICE AT THE SAME INSTANT
    # -------------------------------------------------------------------------
    def test_race_condition_two_operators_return_same_device(self):
        """
        Two operators try to return the EXACT SAME device simultaneously.
        Guarantee:
        - Exactly 1 return transaction is processed.
        - The other either receives idempotent replay or clean rejection.
        - Zero database corruption or duplicate returns.
        """
        target_serial = "RACE_DEV_RETURN_01"
        db = SessionLocal()
        try:
            db.merge(Device(serial=target_serial, make="CHAINWAY", model="MC95", department="LOG", status="Available"))
            db.merge(Employee(employee_id="WORKER_RET_RACE", name="Worker Ret Race", department="LOG", active=True))
            db.commit()
            op = db.query(User).filter(User.username == "operator1").first()
            # Issue first
            issue_device_atomic(db, target_serial, "WORKER_RET_RACE", op)
            op1_id = op.id
            op2_id = db.query(User).filter(User.username == "operator2").first().id
        finally:
            db.close()

        barrier = threading.Barrier(2)
        results = []

        def try_return(op_id):
            s = SessionLocal()
            try:
                u = s.query(User).filter(User.id == op_id).first()
                barrier.wait()
                res = process_scan_pipeline(
                    db=s,
                    code=target_serial,
                    mode="Return",
                    pending=None,
                    operator=u
                )
                results.append((op_id, res))
            finally:
                s.close()

        t1 = threading.Thread(target=try_return, args=(op1_id,))
        t2 = threading.Thread(target=try_return, args=(op2_id,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Verify exactly 1 Return transaction in database
        db = SessionLocal()
        try:
            ret_tx_count = db.query(Transaction).filter(
                Transaction.serial == target_serial,
                Transaction.transaction_type == "Return"
            ).count()
            self.assertEqual(ret_tx_count, 1)

            dev = db.query(Device).filter(Device.serial == target_serial).first()
            self.assertEqual(dev.status, "Available")
            self.assertIsNone(dev.assigned_employee_id)
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 6. CONCURRENT EXCEL MASTER IMPORT AND ACTIVE SCANNING
    # -------------------------------------------------------------------------
    def test_concurrent_import_and_scanning(self):
        """
        An Excel import of 30 devices runs concurrently with live scanning operations.
        Neither operation must fail or produce SQLite busy timeout locks.
        """
        db = SessionLocal()
        try:
            # Seed 10 devices for scanning
            for i in range(10):
                db.merge(Employee(employee_id=f"CONC_IMP_E_{i}", name=f"Worker {i}", department="LOG", active=True))
                db.merge(Device(serial=f"CONC_IMP_D_{i}", make="CHAINWAY", model="MC95", department="LOG", status="Available"))
            db.commit()
            op = db.query(User).filter(User.username == "operator1").first()
            op_id = op.id
        finally:
            db.close()

        # Build 30 device rows for import
        import_rows = []
        for i in range(30):
            import_rows.append({
                "row_number": i + 1,
                "sr_no": i + 1,
                "serial": f"IMPORT_DEV_{i:03d}",
                "make": "CHAINWAY",
                "model": "MC95",
                "department": "IMPORT_DEPT",
                "department_warning": None,
                "remarks": "Working",
                "status": "Available",
                "condition": "Working",
                "inventory_date": today_kolkata_str(),
                "action": "NEW",
                "current_status": "Available"
            })

        errors = []

        def import_worker():
            s = SessionLocal()
            try:
                res = commit_device_import(s, import_rows, mode="UPSERT")
                if not res.get("ok"):
                    errors.append(f"Import failed: {res}")
            except Exception as ex:
                errors.append(f"Import exception: {ex}")
            finally:
                s.close()

        def scan_worker():
            for i in range(10):
                s = SessionLocal()
                try:
                    u = s.query(User).filter(User.id == op_id).first()
                    res = process_scan_pipeline(
                        db=s,
                        code=f"CONC_IMP_D_{i}",
                        mode="Issue",
                        pending={"type": "employee", "code": f"CONC_IMP_E_{i}"},
                        operator=u
                    )
                    if not res.get("ok"):
                        errors.append(f"Scan issue failed: {res}")
                except Exception as ex:
                    errors.append(f"Scan exception: {ex}")
                finally:
                    s.close()

        t_import = threading.Thread(target=import_worker)
        t_scan = threading.Thread(target=scan_worker)

        t_import.start()
        t_scan.start()
        t_import.join()
        t_scan.join()

        self.assertEqual(len(errors), 0, f"Concurrent import/scan errors: {errors}")

    # -------------------------------------------------------------------------
    # 7. CONCURRENT REPORT GENERATION AND ACTIVE SCANNING
    # -------------------------------------------------------------------------
    def test_concurrent_report_generation_and_scanning(self):
        """
        Generating the full 7-sheet Excel report must not block or slow down live scans.
        """
        db = SessionLocal()
        try:
            for i in range(10):
                db.merge(Employee(employee_id=f"CONC_REP_E_{i}", name=f"Rep Worker {i}", department="LOG", active=True))
                db.merge(Device(serial=f"CONC_REP_D_{i}", make="CHAINWAY", model="MC95", department="LOG", status="Available"))
            db.commit()
            op = db.query(User).filter(User.username == "operator1").first()
            op_id = op.id
        finally:
            db.close()

        errors = []
        scan_latencies = []

        def report_worker():
            s = SessionLocal()
            try:
                buf = generate_excel_report(s, all_history=True)
                if len(buf.getvalue()) < 1000:
                    errors.append("Generated report buffer too small")
            except Exception as ex:
                errors.append(f"Report generation failed: {ex}")
            finally:
                s.close()

        def scan_worker():
            for i in range(10):
                s = SessionLocal()
                t0 = time.perf_counter()
                try:
                    u = s.query(User).filter(User.id == op_id).first()
                    res = process_scan_pipeline(
                        db=s,
                        code=f"CONC_REP_D_{i}",
                        mode="Issue",
                        pending={"type": "employee", "code": f"CONC_REP_E_{i}"},
                        operator=u
                    )
                    elapsed = time.perf_counter() - t0
                    scan_latencies.append(elapsed)
                    if not res.get("ok"):
                        errors.append(f"Scan during report failed: {res}")
                except Exception as ex:
                    errors.append(f"Scan during report exception: {ex}")
                finally:
                    s.close()

        t_rep = threading.Thread(target=report_worker)
        t_scan = threading.Thread(target=scan_worker)

        t_rep.start()
        t_scan.start()
        t_rep.join()
        t_scan.join()

        self.assertEqual(len(errors), 0, f"Report concurrency errors: {errors}")
        avg_scan_ms = (sum(scan_latencies) / len(scan_latencies)) * 1000
        # Average scan time during report generation must remain sub-50ms
        self.assertLess(avg_scan_ms, 50.0, f"Scan latency spiked during report: {avg_scan_ms:.2f}ms")

    # -------------------------------------------------------------------------
    # 8. 600-DEVICE INVENTORY REGRESSION TEST DATASET
    # -------------------------------------------------------------------------
    def test_600_device_inventory_mapping_regression(self):
        """
        Regression expectation for the standard 600-device warehouse inventory:
        - 493 Available (Working)
        - 33 Damaged (Not Working)
        - 74 Under Repair (Under Repair)
        Total = 600 devices.
        Damaged + Under Repair (107 devices) must generate automatic DamageReports.
        """
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Device Inventory"
        ws.append(["Sr.No.", "Make", "Model No", "Serial Number", "Department", "Remarks", "Date"])

        sr = 1
        # 1. 493 Available
        for i in range(493):
            ws.append([sr, "CHAINWAY", "MC95", f"REG_600_AVAIL_{i:03d}", "INV MTS", "Working", "28-Oct-26"])
            sr += 1
        # 2. 33 Damaged
        for i in range(33):
            ws.append([sr, "CHAINWAY", "MC95", f"REG_600_DMG_{i:03d}", "INV MTS", "Not Working", "28-Oct-26"])
            sr += 1
        # 3. 74 Under Repair
        for i in range(74):
            ws.append([sr, "ZEBRA", "TC22", f"REG_600_REP_{i:03d}", "INV MTS", "Under Repair", "28-Oct-26"])
            sr += 1

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        file_bytes = buf.getvalue()

        db = SessionLocal()
        try:
            # Validate
            val = validate_device_import(db, file_bytes, "regression_600_devices.xlsx")
            self.assertTrue(val["ok"])
            self.assertEqual(val["total_rows"], 600)
            self.assertEqual(len(val["valid_rows"]), 600)
            self.assertEqual(len(val["rejected_rows"]), 0)

            # Commit
            res = commit_device_import(db, val["valid_rows"], mode="UPSERT")
            self.assertEqual(res["total_imported"], 600)

            # Query database counts for REG_600 series
            avail_cnt = db.query(Device).filter(Device.serial.like("REG_600_AVAIL_%"), Device.status == "Available").count()
            dmg_cnt = db.query(Device).filter(Device.serial.like("REG_600_DMG_%"), Device.status == "Damaged").count()
            rep_cnt = db.query(Device).filter(Device.serial.like("REG_600_REP_%"), Device.status == "Under Repair").count()

            self.assertEqual(avail_cnt, 493)
            self.assertEqual(dmg_cnt, 33)
            self.assertEqual(rep_cnt, 74)

            # Check linked damage tickets for Damaged and Under Repair (107 total)
            dmg_tickets = db.query(DamageReport).filter(
                DamageReport.serial.like("REG_600_%"),
                DamageReport.reported_by_name == "Excel Inventory Import",
                DamageReport.review_status == "Pending Inspection"
            ).count()
            self.assertEqual(dmg_tickets, 107)

            # Ensure Damaged & Under Repair devices CANNOT be issued
            op = db.query(User).filter(User.username == "operator1").first()
            emp = Employee(employee_id="REG_TEST_EMP", name="Reg Worker", department="INV", active=True)
            db.merge(emp)
            db.commit()

            with self.assertRaises(ScanException):
                issue_device_atomic(db, "REG_600_DMG_000", "REG_TEST_EMP", op)

            with self.assertRaises(ScanException):
                issue_device_atomic(db, "REG_600_REP_000", "REG_TEST_EMP", op)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

