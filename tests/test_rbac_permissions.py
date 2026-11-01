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
from app.models import User, Device, Employee, SystemSetting
from app.auth import sign_session, hash_password, ROLE_ADMIN, ROLE_INVENTORY_MANAGER, ROLE_OPERATOR

class RBACPermissionsTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_schema_migration()
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()

        cls.client = TestClient(app)
        db = SessionLocal()
        try:
            # Ensure users exist
            cls.admin_user = db.query(User).filter(User.username == "rbac_admin").first()
            if not cls.admin_user:
                cls.admin_user = User(
                    username="rbac_admin",
                    password_hash=hash_password("adminpass123"),
                    full_name="RBAC Admin",
                    role=ROLE_ADMIN,
                    active=True
                )
                db.add(cls.admin_user)

            cls.im_user = db.query(User).filter(User.username == "rbac_invmgr").first()
            if not cls.im_user:
                cls.im_user = User(
                    username="rbac_invmgr",
                    password_hash=hash_password("mgrpass123"),
                    full_name="RBAC Inventory Manager",
                    role=ROLE_INVENTORY_MANAGER,
                    active=True
                )
                db.add(cls.im_user)

            cls.op_user = db.query(User).filter(User.username == "rbac_op").first()
            if not cls.op_user:
                cls.op_user = User(
                    username="rbac_op",
                    password_hash=hash_password("oppass123"),
                    full_name="RBAC Operator",
                    role=ROLE_OPERATOR,
                    active=True
                )
                db.add(cls.op_user)

            db.commit()

            cls.admin_token = sign_session({"username": cls.admin_user.username, "role": ROLE_ADMIN})
            cls.im_token = sign_session({"username": cls.im_user.username, "role": ROLE_INVENTORY_MANAGER})
            cls.op_token = sign_session({"username": cls.op_user.username, "role": ROLE_OPERATOR})
        finally:
            db.close()

    def test_01_unauthenticated_requests_blocked(self):
        """Unauthenticated requests to admin or reports pages are redirected or rejected."""
        client = TestClient(app)
        r1 = client.get("/admin", follow_redirects=False)
        self.assertIn(r1.status_code, (302, 307))
        self.assertIn("/login", r1.headers.get("location", ""))

        r2 = client.get("/admin/reports", follow_redirects=False)
        self.assertIn(r2.status_code, (302, 307))

        r3 = client.post("/api/scan", json={"code": "123"}, follow_redirects=False)
        self.assertEqual(r3.status_code, 401)

    def test_02_operator_forbidden_from_admin_and_reports(self):
        """Operators must receive HTTP 403 Forbidden on all administrative endpoints."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.op_token)

        # Web pages
        endpoints = [
            "/admin",
            "/admin/devices",
            "/admin/employees",
            "/admin/reports",
            "/admin/users",
            "/admin/missing",
            "/admin/audit",
            "/admin/settings"
        ]
        for ep in endpoints:
            resp = client.get(ep, follow_redirects=False)
            self.assertEqual(resp.status_code, 403, f"Expected 403 for operator on {ep}")

        # API endpoints
        api_endpoints = [
            ("/api/reports/async/generate", "post"),
            ("/admin/users", "post"),
            ("/admin/missing/confirm", "post")
        ]
        for ep, method in api_endpoints:
            if method == "post":
                resp = client.post(ep, json={}, follow_redirects=False)
            else:
                resp = client.get(ep, follow_redirects=False)
            self.assertEqual(resp.status_code, 403, f"Expected 403 for operator on {ep}")

    def test_03_inventory_manager_permitted_routes(self):
        """Inventory Managers can access devices, employees, damage, missing, reports, audit."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.im_token)

        allowed_pages = [
            "/admin/devices",
            "/admin/employees",
            "/admin/damage",
            "/admin/missing",
            "/admin/reports",
            "/admin/audit",
            "/admin/transactions"
        ]
        for ep in allowed_pages:
            resp = client.get(ep)
            self.assertEqual(resp.status_code, 200, f"Expected 200 for inventory manager on {ep}")

    def test_04_inventory_manager_restricted_routes(self):
        """Inventory Managers CANNOT manage users, delete devices, or change daily cutoff."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.im_token)

        # User management is Admin only
        r_users = client.get("/admin/users", follow_redirects=False)
        self.assertEqual(r_users.status_code, 403)

        # Settings is Admin only
        r_settings = client.get("/admin/settings", follow_redirects=False)
        self.assertEqual(r_settings.status_code, 403)

        # Device deletion is Admin only
        r_del = client.post("/admin/device/delete/NONEXISTENT", follow_redirects=False)
        self.assertEqual(r_del.status_code, 403)

        # Confirm Missing is Admin only
        r_conf = client.post("/admin/missing/confirm", data={"serial": "TEST"}, follow_redirects=False)
        self.assertEqual(r_conf.status_code, 403)

    def test_05_admin_full_access(self):
        """Admins have full access to all areas including users, settings, and deletion."""
        client = TestClient(app)
        client.cookies.set("hhd_session", self.admin_token)

        admin_pages = [
            "/admin",
            "/admin/users",
            "/admin/settings",
            "/admin/missing",
            "/admin/devices",
            "/admin/audit"
        ]
        for ep in admin_pages:
            resp = client.get(ep)
            self.assertEqual(resp.status_code, 200, f"Expected 200 for admin on {ep}")

if __name__ == "__main__":
    unittest.main()
