import os
import sys
import unittest
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from main import app
from tests.conftest import seed_initial_data_if_needed
from app.database import engine, Base, SessionLocal
from app.models import User, Device, Employee
from app.auth import sign_session
from app.migration import run_schema_migration

class APIEndpointsTestSuite(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()
        self.client = TestClient(app)

    @classmethod
    def setUpClass(cls):
        run_schema_migration()
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()
        db = SessionLocal()
        try:
            if not db.query(Employee).filter(Employee.employee_id == "1222905").first():
                db.add(Employee(employee_id="1222905", name="Ajay Kumar", department="LOGISTICS", active=True))
            if not db.query(Device).filter(Device.serial == "HC660DE260502008").first():
                db.add(Device(serial="HC660DE260502008", make="CHAINWAY", model="MC95", department="INV MTS", status="Available"))
            db.commit()
        finally:
            db.close()

    def test_login_flow(self):
        # 1. Successful Admin Login
        resp = self.client.post("/login", data={
            "username": "hhdadmin",
            "password": "admin12345",
            "next_url": "/admin"
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("hhd_session", resp.cookies)

        # 2. Invalid Credentials
        resp_bad = self.client.post("/login", data={
            "username": "hhdadmin",
            "password": "wrongpassword123"
        }, follow_redirects=False)
        self.assertEqual(resp_bad.status_code, 400)
        self.assertIn("Invalid username or password", resp_bad.text)

    def test_operator_blocked_from_admin_routes(self):
        # Create operator session token
        op_token = sign_session({"username": "operator1", "role": "operator"})
        self.client.cookies.set("hhd_session", op_token)

        # Try to access admin dashboard
        resp = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(resp.status_code, 403)

        # Try to access admin users
        resp_users = self.client.get("/admin/users", follow_redirects=False)
        self.assertEqual(resp_users.status_code, 403)

        # Try to access admin backups
        resp_backups = self.client.get("/admin/backups", follow_redirects=False)
        self.assertEqual(resp_backups.status_code, 403)

    def test_admin_allowed_on_admin_routes(self):
        # Create admin session token
        admin_token = sign_session({"username": "hhdadmin", "role": "admin"})
        self.client.cookies.set("hhd_session", admin_token)

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Operations Center", resp.text)

        resp_devs = self.client.get("/admin/devices")
        self.assertEqual(resp_devs.status_code, 200)
        self.assertIn("Device Inventory", resp_devs.text)

    def test_all_navigation_routes_and_same_tab_behavior(self):
        """
        Verifies every application navigation link and ensures all internal
        routes load successfully in the same tab without target='_blank' or window.open().
        """
        admin_token = sign_session({"username": "hhdadmin", "role": "admin"})
        self.client.cookies.set("hhd_session", admin_token)

        internal_pages = [
            ("/", "Scan Station"),
            ("/board", "Live Device Board"),
            ("/admin", "Operations Center"),
            ("/admin/devices", "Device Inventory"),
            ("/admin/employees", "Employee Master"),
            ("/admin/import", "Excel Master Import"),
            ("/admin/transactions", "Transaction Audit"),
            ("/admin/damage", "Damage & Repair"),
            ("/admin/reports", "Reports"),
            ("/admin/users", "User & Operator"),
            ("/admin/backups", "Database Files"),
            ("/admin/reset", "Controlled Initial Data Reset"),
        ]

        for path, expected_text in internal_pages:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, f"Page {path} failed to load with 200")
            self.assertIn(expected_text, resp.text, f"Page {path} missing expected content '{expected_text}'")
            
            # Assert no target="_blank" is used for any internal navigation link on any page
            self.assertNotIn('href="/board" target="_blank"', resp.text)
            self.assertNotIn('target="_blank"', resp.text)
            self.assertNotIn('window.open(', resp.text)

        # Test operator navigation links
        op_token = sign_session({"username": "operator1", "role": "operator"})
        self.client.cookies.set("hhd_session", op_token)
        for path in ["/", "/board"]:
            resp_op = self.client.get(path)
            self.assertEqual(resp_op.status_code, 200)
            self.assertNotIn('href="/board" target="_blank"', resp_op.text)
            self.assertNotIn('target="_blank"', resp_op.text)

    def test_api_scan_over_http(self):
        db = SessionLocal()
        try:
            dev = db.query(Device).filter(Device.serial == "HC660DE260502008").first()
            if dev:
                dev.status = "Available"
                dev.assigned_employee_id = None
                db.commit()
        finally:
            db.close()

        op_token = sign_session({"username": "operator1", "role": "operator"})
        self.client.cookies.set("hhd_session", op_token)

        # 1. Scan Worker
        r1 = self.client.post("/api/scan", json={"code": "1222905", "mode": "Issue"})
        self.assertEqual(r1.status_code, 200)
        d1 = r1.json()
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["state"], "pending")
        self.assertEqual(d1["pending"]["code"], "1222905")

        # 2. Scan Device
        r2 = self.client.post("/api/scan", json={
            "code": "HC660DE260502008",
            "mode": "Issue",
            "pending": d1["pending"]
        })
        self.assertEqual(r2.status_code, 200)
        d2 = r2.json()
        self.assertTrue(d2["ok"])
        self.assertEqual(d2["state"], "done")
        self.assertEqual(d2["action"], "Issue")
        self.assertIn("Ajay Kumar", d2["message"])

        # 3. Return Device Alone in Return Mode
        r3 = self.client.post("/api/scan", json={
            "code": "HC660DE260502008",
            "mode": "Return"
        })
        self.assertEqual(r3.status_code, 200)
        d3 = r3.json()
        self.assertTrue(d3["ok"])
        self.assertEqual(d3["state"], "done")
        self.assertEqual(d3["action"], "Return")
        self.assertIn("Returned from Ajay Kumar", d3["message"])

    def test_excel_and_csv_reports_download(self):
        admin_token = sign_session({"username": "hhdadmin", "role": "admin"})
        self.client.cookies.set("hhd_session", admin_token)

        # Excel
        resp_excel = self.client.get("/reports/excel")
        self.assertEqual(resp_excel.status_code, 200)
        self.assertEqual(
            resp_excel.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.assertGreater(len(resp_excel.content), 1000)

        # CSV Devices
        resp_csv = self.client.get("/reports/csv/devices")
        self.assertEqual(resp_csv.status_code, 200)
        self.assertIn("Serial,Make,Model", resp_csv.text)

if __name__ == "__main__":
    unittest.main()
