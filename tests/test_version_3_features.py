import os
import sys
import unittest
import io
import threading
from datetime import datetime, date
from pathlib import Path
import openpyxl

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from fastapi.testclient import TestClient
from main import app
from tests.conftest import seed_initial_data_if_needed
from app.config import BACKUP_DIR, DB_PATH, today_kolkata_str
from app.database import engine, Base, SessionLocal
from app.models import User, Device, Employee, Transaction, DeviceAssignment, DamageReport
from app.auth import sign_session, hash_password
from app.migration import run_schema_migration
from app.excel_templates import generate_device_inventory_template, generate_employee_master_template
from app.excel_importer import (
    validate_device_import, commit_device_import,
    validate_employee_import, commit_employee_import,
    cell_to_str
)
from app.reports import generate_excel_report
from app.scan_engine import issue_device_atomic, return_device_atomic, process_scan_pipeline

class Version3FeaturesTestSuite(unittest.TestCase):

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

    # -------------------------------------------------------------
    # 1. Text Conversion Edge Cases (Leading zeros, letters, scientific notation)
    # -------------------------------------------------------------
    def test_cell_to_str_preserves_text_and_leading_zeros(self):
        self.assertEqual(cell_to_str("001234"), "001234")
        self.assertEqual(cell_to_str("s25119524701038"), "s25119524701038")
        self.assertEqual(cell_to_str(2288), "2288")
        self.assertEqual(cell_to_str(2288.0), "2288")
        self.assertEqual(cell_to_str("  MC95  "), "MC95")
        self.assertEqual(cell_to_str(None), "")
        self.assertEqual(cell_to_str(""), "")

    # -------------------------------------------------------------
    # 2. Excel Template Generation & Verification
    # -------------------------------------------------------------
    def test_device_template_structure(self):
        buf = generate_device_inventory_template()
        self.assertGreater(len(buf.getvalue()), 1000)
        wb = openpyxl.load_workbook(buf)
        self.assertIn("Device Inventory", wb.sheetnames)
        self.assertIn("Instructions", wb.sheetnames)
        ws = wb["Device Inventory"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, 8)]
        expected = ["Sr.No.", "Make", "Model No", "Serial Number", "Department", "Remarks", "Date"]
        self.assertEqual(headers, expected)

    def test_employee_template_structure(self):
        buf = generate_employee_master_template()
        self.assertGreater(len(buf.getvalue()), 1000)
        wb = openpyxl.load_workbook(buf)
        self.assertIn("Employee Master", wb.sheetnames)
        self.assertIn("Instructions", wb.sheetnames)
        ws = wb["Employee Master"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, 6)]
        expected = ["Sr.No.", "Employee ID", "Employee Name", "Department", "Status"]
        self.assertEqual(headers, expected)

    def test_template_download_endpoints(self):
        self.client.cookies.set("hhd_session", self.admin_token)
        r_dev = self.client.get("/admin/import/template/device")
        self.assertEqual(r_dev.status_code, 200)
        self.assertIn("Device_Inventory_Template.xlsx", r_dev.headers.get("content-disposition", ""))

        r_emp = self.client.get("/admin/import/template/employee")
        self.assertEqual(r_emp.status_code, 200)
        self.assertIn("Employee_Master_Template.xlsx", r_emp.headers.get("content-disposition", ""))

    # -------------------------------------------------------------
    # 3. Device Excel Import: Preview, Rejections, & UPSERT
    # -------------------------------------------------------------
    def _create_sample_device_xlsx(self) -> bytes:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Devices"
        ws.append(["Sr.No.", "Make", "Model No", "Serial Number", "Department", "Remarks", "Date"])
        ws.append([1, "CHAINWAY", "MC95", "001234", "INV MTS", "Working", "28-Oct-26"])
        ws.append([2, "ZEBRA", "TC22", "s25119524701038", "INV MTS", "Working", "28-Oct-26"])
        ws.append([3, "CHAINWAY", "MC95", "001234", "LOGISTICS", "Working", "28-Oct-26"])  # duplicate serial
        ws.append([4, "ZEBRA", "TC22", "", "INV MTS", "Working", "28-Oct-26"])  # missing serial
        ws.append([5, "HONEYWELL", "EDA51", "HONEY_999", "OUTBOUND", "Damaged", "28-Oct-26"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_device_import_preview_and_validation(self):
        xlsx_bytes = self._create_sample_device_xlsx()
        db = SessionLocal()
        try:
            preview = validate_device_import(db, xlsx_bytes, "test_devices.xlsx")
            self.assertEqual(preview["total_rows"], 5)
            self.assertEqual(preview["valid_count"], 3)
            self.assertEqual(preview["rejected_count"], 2)

            # Check rejection reasons
            row4 = [r for r in preview["rejected_rows"] if r["row_number"] == 4][0]
            self.assertIn("Duplicate", row4["reason"])

            row5 = [r for r in preview["rejected_rows"] if r["row_number"] == 5][0]
            self.assertIn("cannot be empty", row5["reason"])

            # Commit valid rows via UPSERT
            res = commit_device_import(db, preview["valid_rows"], mode="UPSERT")
            self.assertEqual(res["inserted"], 3)
            self.assertEqual(res["updated"], 0)

            # Verify saved devices preserve leading zero and lowercase letters
            d1 = db.query(Device).filter(Device.serial == "001234").first()
            self.assertIsNotNone(d1)
            self.assertEqual(d1.make, "CHAINWAY")
            self.assertEqual(d1.status, "Available")

            d2 = db.query(Device).filter(Device.serial == "s25119524701038").first()
            self.assertIsNotNone(d2)
            self.assertEqual(d2.serial, "s25119524701038")
            self.assertEqual(d2.department, "INV MTS")

            d3 = db.query(Device).filter(Device.serial == "HONEY_999").first()
            self.assertIsNotNone(d3)
            self.assertEqual(d3.status, "Damaged")
        finally:
            db.close()

    # -------------------------------------------------------------
    # 4. UPSERT Mode: Never Resets Issued Devices to Available
    # -------------------------------------------------------------
    def test_upsert_preserves_issued_status(self):
        db = SessionLocal()
        try:
            admin_user = db.query(User).filter(User.username == "hhdadmin").first()
            # Create employee & device, issue it
            emp = db.merge(Employee(employee_id="EMP_UPSERT", name="Upsert Worker", department="INV", active=True))
            dev = db.merge(Device(serial="DEV_UPSERT_01", make="TEST", model="T1", department="INV", status="Available"))
            db.commit()

            # Issue device
            issue_device_atomic(db, "DEV_UPSERT_01", "EMP_UPSERT", admin_user)

            # Verify device is Issued
            db.refresh(dev)
            self.assertEqual(dev.status, "Issued")

            # Now perform an UPSERT spreadsheet import containing this device with Remarks='Working'
            rows = [{
                "row_number": 1,
                "serial": "DEV_UPSERT_01",
                "make": "TEST_UPDATED",
                "model": "T1_UPDATED",
                "department": "NEW_DEPT",
                "status": "Available",
                "condition": "Working",
                "remarks": "Working",
                "inventory_date": "30-Oct-26",
                "action": "UPDATE"
            }]
            commit_device_import(db, rows, mode="UPSERT")

            # Check that the device updated make and dept, BUT preserved status='Issued' and assigned employee!
            db.refresh(dev)
            self.assertEqual(dev.make, "TEST_UPDATED")
            self.assertEqual(dev.department, "NEW_DEPT")
            self.assertEqual(dev.status, "Issued")
            self.assertEqual(dev.assigned_employee_id, "EMP_UPSERT")

            # Cleanup
            return_device_atomic(db, "DEV_UPSERT_01", admin_user, "Working")
        finally:
            db.close()

    # -------------------------------------------------------------
    # 5. REPLACE Mode: Blocked when Devices are Issued
    # -------------------------------------------------------------
    def test_replace_mode_blocked_on_active_assignments(self):
        db = SessionLocal()
        try:
            admin_user = db.query(User).filter(User.username == "hhdadmin").first()
            emp = db.merge(Employee(employee_id="EMP_REPLACE", name="Replace Worker", department="INV", active=True))
            dev = db.merge(Device(serial="DEV_REPLACE_01", make="TEST", model="T1", department="INV", status="Available"))
            db.commit()

            issue_device_atomic(db, "DEV_REPLACE_01", "EMP_REPLACE", admin_user)

            # Attempt REPLACE mode -> Must raise ValueError
            rows = [{
                "row_number": 1,
                "serial": "DEV_REPLACE_02",
                "make": "TEST",
                "model": "T2",
                "department": "INV",
                "status": "Available",
                "condition": "Working",
                "remarks": "Working",
                "inventory_date": "30-Oct-26",
                "action": "NEW"
            }]
            with self.assertRaises(ValueError) as ctx:
                commit_device_import(db, rows, mode="REPLACE")
            self.assertIn("Cannot execute REPLACE mode", str(ctx.exception))

            # Cleanup
            return_device_atomic(db, "DEV_REPLACE_01", admin_user, "Working")
        finally:
            db.close()

    # -------------------------------------------------------------
    # 6. Employee Master Excel Import: Preview, Leading Zeros, & Status
    # -------------------------------------------------------------
    def _create_sample_employee_xlsx(self) -> bytes:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Employees"
        ws.append(["Sr.No.", "Employee ID", "Employee Name", "Department", "Status"])
        ws.append([1, "009988", "Ramesh Verma", "INVENTORY", "Active"])
        ws.append([2, "E-7766", "Sita Devi", "PACKING", "Inactive"])
        ws.append([3, "009988", "Ramesh Verma Duplicate", "INVENTORY", "Active"])  # duplicate ID
        ws.append([4, "", "No ID Worker", "SORTING", "Active"])  # missing ID
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_employee_import_preview_and_commit(self):
        xlsx_bytes = self._create_sample_employee_xlsx()
        db = SessionLocal()
        try:
            preview = validate_employee_import(db, xlsx_bytes, "test_employees.xlsx")
            self.assertEqual(preview["total_rows"], 4)
            self.assertEqual(preview["valid_count"], 2)
            self.assertEqual(preview["rejected_count"], 2)

            res = commit_employee_import(db, preview["valid_rows"], mode="UPSERT")
            self.assertEqual(res["inserted"], 2)

            e1 = db.query(Employee).filter(Employee.employee_id == "009988").first()
            self.assertIsNotNone(e1)
            self.assertEqual(e1.name, "Ramesh Verma")
            self.assertTrue(e1.active)

            e2 = db.query(Employee).filter(Employee.employee_id == "E-7766").first()
            self.assertIsNotNone(e2)
            self.assertFalse(e2.active)
        finally:
            db.close()

    # -------------------------------------------------------------
    # 7. Operator Accountability: Exact Identity Recorded on All Actions
    # -------------------------------------------------------------
    def test_operator_identity_in_transactions(self):
        # 1. Login Rahul (operator1)
        r_login = self.client.post("/login", data={"username": "operator1", "password": "operator123"}, follow_redirects=False)
        self.assertEqual(r_login.status_code, 302)

        # 2. Seed self-contained device and worker
        db = SessionLocal()
        emp = Employee(employee_id="OP_EMP_01", name="Ramesh Operator", department="INV", active=True)
        dev = Device(serial="OP_DEV_01", make="CHAINWAY", model="MC95", department="INV", status="Available")
        db.merge(emp)
        db.merge(dev)
        db.commit()
        db.close()

        r_issue = self.client.post("/api/scan", json={
            "code": "OP_DEV_01",
            "mode": "Issue",
            "pending": {"type": "employee", "code": "OP_EMP_01"}
        })
        self.assertTrue(r_issue.json().get("ok"))

        # 3. Check transaction recorded Rahul
        db = SessionLocal()
        tx_issue = db.query(Transaction).filter(Transaction.serial == "OP_DEV_01", Transaction.transaction_type == "Issue").order_by(Transaction.id.desc()).first()
        self.assertIsNotNone(tx_issue)
        self.assertEqual(tx_issue.operator_name, "Rahul (Operator 1)")
        self.assertEqual(tx_issue.transaction_date, today_kolkata_str())

        # 4. Now Operator 2 (Amit) returns device
        self.client.cookies.set("hhd_session", self.op2_token)
        r_ret = self.client.post("/api/return", json={"serial": "OP_DEV_01", "condition": "Working"})
        self.assertTrue(r_ret.json().get("ok"))

        # 5. Check transaction recorded Amit
        tx_ret = db.query(Transaction).filter(Transaction.serial == "OP_DEV_01", Transaction.transaction_type == "Return").order_by(Transaction.id.desc()).first()
        self.assertIsNotNone(tx_ret)
        self.assertEqual(tx_ret.operator_name, "Amit (Operator 2)")
        self.assertEqual(tx_ret.transaction_date, today_kolkata_str())
        db.close()

    # -------------------------------------------------------------
    # 8. Controlled Initial Data Reset
    # -------------------------------------------------------------
    def test_controlled_initial_data_reset(self):
        self.client.cookies.set("hhd_session", self.admin_token)

        # 1. Bad password -> fails redirect with error param
        r_bad_pw = self.client.post("/admin/reset", data={
            "admin_password": "wrongpassword",
            "confirmation_text": "CONFIRM RESET"
        }, follow_redirects=False)
        self.assertEqual(r_bad_pw.status_code, 302)
        self.assertIn("error", r_bad_pw.headers.get("location", ""))

        # 2. Bad confirmation text -> fails redirect with error param
        r_bad_conf = self.client.post("/admin/reset", data={
            "admin_password": "admin12345",
            "confirmation_text": "yes please reset"
        }, follow_redirects=False)
        self.assertEqual(r_bad_conf.status_code, 302)
        self.assertIn("error", r_bad_conf.headers.get("location", ""))

        # 3. Valid reset
        r_ok = self.client.post("/admin/reset", data={
            "admin_password": "admin12345",
            "confirmation_text": "CONFIRM RESET"
        }, follow_redirects=False)
        self.assertEqual(r_ok.status_code, 302)
        self.assertIn("/admin/import", r_ok.headers.get("location", ""))

        # Verify safety copy created
        safety_copies = list(BACKUP_DIR.glob("*pre_initial_data_reset*.db"))
        self.assertGreater(len(safety_copies), 0)

        # Verify all devices and employees are wiped
        db = SessionLocal()
        self.assertEqual(db.query(Device).count(), 0)
        self.assertEqual(db.query(Employee).count(), 0)
        self.assertEqual(db.query(Transaction).count(), 0)
        self.assertEqual(db.query(DeviceAssignment).count(), 0)

        # Verify admin and operator accounts are strictly preserved!
        admins = db.query(User).filter(User.role == "admin").all()
        self.assertGreater(len(admins), 0)
        operators = db.query(User).filter(User.role == "operator").all()
        self.assertGreater(len(operators), 0)
        db.close()

    # -------------------------------------------------------------
    # 9. 7-Sheet 24/7 Continuous Excel Report
    # -------------------------------------------------------------
    def test_7_sheet_excel_report_generation(self):
        db = SessionLocal()
        try:
            # Seed 1 emp and 1 dev and 1 tx for the report
            emp = Employee(employee_id="REP_E1", name="Report Employee", department="INVENTORY", active=True)
            dev = Device(serial="REP_D1", make="CHAINWAY", model="MC95", department="INVENTORY", status="Available")
            db.merge(emp)
            db.merge(dev)
            db.commit()

            tx = Transaction(
                transaction_time=datetime.now(),
                transaction_date=date.today().strftime("%Y-%m-%d"),
                transaction_type="Issue",
                serial="REP_D1",
                employee_id="REP_E1",
                employee_name="Report Employee",
                department="INVENTORY",
                operator_id=1,
                operator_name="Admin",
                condition="Working",
                cover_qty=2,
                remarks="Report Test"
            )
            db.add(tx)
            db.commit()

            # Generate workbook
            buf = generate_excel_report(db)
            self.assertGreater(len(buf.getvalue()), 2000)

            wb = openpyxl.load_workbook(buf)
            expected_sheets = [
                "Overall Summary",
                "Transaction Details",
                "Device Inventory",
                "Employee Master",
                "Damage and Repair Reports",
                "Outstanding Devices",
                "Operator Activity Summary"
            ]
            self.assertEqual(wb.sheetnames, expected_sheets)

            # Verify no shift headers in Transaction Details
            ws_tx = wb["Transaction Details"]
            headers_tx = [ws_tx.cell(row=1, column=c).value for c in range(1, 15) if ws_tx.cell(row=1, column=c).value]
            for h in headers_tx:
                self.assertNotIn("Shift", str(h))

            # Verify Operator Activity Summary sheet exists and has operator column
            ws_op = wb["Operator Activity Summary"]
            headers_op = [ws_op.cell(row=1, column=c).value for c in range(1, 10) if ws_op.cell(row=1, column=c).value]
            self.assertIn("Operator Full Name", headers_op)
            self.assertIn("Issues Done", headers_op)
            self.assertIn("Returns Handled", headers_op)
        finally:
            db.close()

    # -------------------------------------------------------------
    # 10. Concurrent Scanning Simulation
    # -------------------------------------------------------------
    def test_concurrent_operator_scans(self):
        # Create 20 test devices and 20 employees
        db = SessionLocal()
        for i in range(20):
            emp = Employee(employee_id=f"CONC_E_{i:02d}", name=f"Worker {i}", department="CONC", active=True)
            dev = Device(serial=f"CONC_D_{i:02d}", make="ZEBRA", model="TC22", department="CONC", status="Available")
            db.merge(emp)
            db.merge(dev)
        db.commit()
        db.close()

        errors = []

        def operator_worker(op_id: int, op_name: str, start_idx: int, end_idx: int):
            for i in range(start_idx, end_idx):
                db_thread = SessionLocal()
                try:
                    op_user = db_thread.query(User).filter(User.id == op_id).first()
                    res_issue = process_scan_pipeline(
                        db=db_thread,
                        code=f"CONC_D_{i:02d}",
                        mode="Issue",
                        pending={"type": "employee", "code": f"CONC_E_{i:02d}"},
                        operator=op_user
                    )
                    if not res_issue.get("ok"):
                        errors.append(f"Issue failed for {i}: {res_issue}")

                    res_ret = process_scan_pipeline(
                        db=db_thread,
                        code=f"CONC_D_{i:02d}",
                        mode="Return",
                        pending=None,
                        operator=op_user
                    )
                    if not res_ret.get("ok"):
                        errors.append(f"Return failed for {i}: {res_ret}")
                except Exception as ex:
                    errors.append(f"Exception for {i}: {ex}")
                finally:
                    db_thread.close()

        # Run 2 operators concurrently
        t1 = threading.Thread(target=operator_worker, args=(2, "Rahul (Operator 1)", 0, 10))
        t2 = threading.Thread(target=operator_worker, args=(3, "Amit (Operator 2)", 10, 20))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Concurrent scan errors: {errors}")

        # Verify all 20 devices returned and in Available status
        db = SessionLocal()
        for i in range(20):
            dev = db.query(Device).filter(Device.serial == f"CONC_D_{i:02d}").first()
            self.assertEqual(dev.status, "Available")
            self.assertIsNone(dev.assigned_employee_id)
        db.close()

if __name__ == "__main__":
    unittest.main()
