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
from app.models import User, Device, Employee, Transaction, DeviceAssignment
from app.auth import sign_session
from app.scan_engine import issue_device_atomic, process_scan_pipeline

class TwoScanReturnTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_schema_migration()
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()
        cls.client = TestClient(app)

        db = SessionLocal()
        try:
            # Create two test operators
            cls.op_issuer = db.query(User).filter(User.username == "op_issuer").first()
            if not cls.op_issuer:
                cls.op_issuer = User(username="op_issuer", password_hash="hash", full_name="Issuer Operator", role="operator", active=True)
                db.add(cls.op_issuer)

            cls.op_returner = db.query(User).filter(User.username == "op_returner").first()
            if not cls.op_returner:
                cls.op_returner = User(username="op_returner", password_hash="hash", full_name="Returner Operator", role="operator", active=True)
                db.add(cls.op_returner)

            # Create test devices and employees
            dev1 = Device(serial="DEV_TWO_01", make="CHAINWAY", model="MC95", status="Available")
            dev2 = Device(serial="DEV_TWO_02", make="CHAINWAY", model="MC95", status="Available")
            emp1 = Employee(employee_id="EMP_TWO_01", name="Worker Alpha", department="Outbound", active=True)
            emp2 = Employee(employee_id="EMP_TWO_02", name="Worker Beta", department="Inbound", active=True)

            db.merge(dev1)
            db.merge(dev2)
            db.merge(emp1)
            db.merge(emp2)
            db.commit()

            cls.issuer_token = sign_session({"username": cls.op_issuer.username, "role": "operator"})
            cls.returner_token = sign_session({"username": cls.op_returner.username, "role": "operator"})
        finally:
            db.close()

    def test_01_return_employee_first_then_device(self):
        """Worker ID scanned first in Return mode, then Device scanned second -> Successful Return."""
        db = SessionLocal()
        try:
            # 1. Issue DEV_TWO_01 to EMP_TWO_01 by op_issuer
            issue_device_atomic(db, "DEV_TWO_01", "EMP_TWO_01", self.op_issuer)

            client = TestClient(app)
            client.cookies.set("hhd_session", self.returner_token)

            # Scan 1: Employee ID in Return mode
            r1 = client.post("/api/scan", json={"code": "EMP_TWO_01", "mode": "Return", "strict_two_scan": True})
            self.assertEqual(r1.status_code, 200)
            d1 = r1.json()
            self.assertTrue(d1["ok"])
            self.assertEqual(d1["state"], "pending")
            self.assertEqual(d1["pending"]["type"], "employee")
            self.assertEqual(d1["pending"]["code"], "EMP_TWO_01")

            # Scan 2: Device serial
            r2 = client.post("/api/scan", json={
                "code": "DEV_TWO_01",
                "mode": "Return",
                "pending": d1["pending"],
                "strict_two_scan": True
            })
            self.assertEqual(r2.status_code, 200)
            d2 = r2.json()
            self.assertTrue(d2["ok"])
            self.assertEqual(d2["state"], "done")
            self.assertEqual(d2["action"], "Return")
            self.assertIn("Worker Alpha", d2["message"])

            # Verify database state
            db.expire_all()
            dev = db.query(Device).filter(Device.serial == "DEV_TWO_01").first()
            self.assertEqual(dev.status, "Available")
            self.assertIsNone(dev.assigned_employee_id)

            # Verify transaction accountability: separate issuing vs returning operator
            tx = db.query(Transaction).filter(
                Transaction.serial == "DEV_TWO_01",
                Transaction.transaction_type == "Return"
            ).order_by(Transaction.id.desc()).first()
            self.assertIsNotNone(tx)
            self.assertEqual(tx.issued_by_operator_name, "Issuer Operator")
            self.assertEqual(tx.returned_by_operator_name, "Returner Operator")
        finally:
            db.close()

    def test_02_return_device_first_then_employee(self):
        """Device scanned first in Return mode, then Worker ID scanned second -> Successful Return."""
        db = SessionLocal()
        try:
            # 1. Issue DEV_TWO_02 to EMP_TWO_02 by op_issuer
            issue_device_atomic(db, "DEV_TWO_02", "EMP_TWO_02", self.op_issuer)

            client = TestClient(app)
            client.cookies.set("hhd_session", self.returner_token)

            # Scan 1: Device serial in Return mode
            r1 = client.post("/api/scan", json={"code": "DEV_TWO_02", "mode": "Return", "strict_two_scan": True})
            self.assertEqual(r1.status_code, 200)
            d1 = r1.json()
            self.assertTrue(d1["ok"])
            self.assertEqual(d1["state"], "pending")
            self.assertEqual(d1["pending"]["type"], "device")
            self.assertEqual(d1["pending"]["code"], "DEV_TWO_02")

            # Scan 2: Employee ID
            r2 = client.post("/api/scan", json={
                "code": "EMP_TWO_02",
                "mode": "Return",
                "pending": d1["pending"],
                "strict_two_scan": True
            })
            self.assertEqual(r2.status_code, 200)
            d2 = r2.json()
            self.assertTrue(d2["ok"])
            self.assertEqual(d2["state"], "done")
            self.assertEqual(d2["action"], "Return")
            self.assertIn("Worker Beta", d2["message"])

            db.expire_all()
            dev = db.query(Device).filter(Device.serial == "DEV_TWO_02").first()
            self.assertEqual(dev.status, "Available")
            self.assertIsNone(dev.assigned_employee_id)
        finally:
            db.close()

    def test_03_assignment_mismatch_rejected(self):
        """If returning employee != currently assigned employee, return is strictly rejected."""
        db = SessionLocal()
        try:
            # Issue DEV_TWO_01 to EMP_TWO_01
            issue_device_atomic(db, "DEV_TWO_01", "EMP_TWO_01", self.op_issuer)

            client = TestClient(app)
            client.cookies.set("hhd_session", self.returner_token)

            # Scan Worker Beta (EMP_TWO_02) first
            r1 = client.post("/api/scan", json={"code": "EMP_TWO_02", "mode": "Return", "strict_two_scan": True})
            self.assertEqual(r1.status_code, 200)
            d1 = r1.json()
            self.assertEqual(d1["state"], "pending")

            # Scan DEV_TWO_01 (which belongs to EMP_TWO_01!)
            r2 = client.post("/api/scan", json={
                "code": "DEV_TWO_01",
                "mode": "Return",
                "pending": d1["pending"],
                "strict_two_scan": True
            })
            self.assertEqual(r2.status_code, 200)
            d2 = r2.json()
            self.assertFalse(d2["ok"])
            self.assertEqual(d2["state"], "error")
            self.assertIn("Assignment Mismatch", d2["title"])
            self.assertIn("Worker Alpha", d2["message"])

            # Device must remain Issued to Worker Alpha
            db.expire_all()
            dev = db.query(Device).filter(Device.serial == "DEV_TWO_01").first()
            self.assertEqual(dev.status, "Issued")
            self.assertEqual(dev.assigned_employee_id, "EMP_TWO_01")
        finally:
            db.close()

if __name__ == "__main__":
    unittest.main()

