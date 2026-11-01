import os
import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from main import app
from app.config import DB_PATH, BASE_DIR
from app.database import engine, Base, SessionLocal, verify_database_state
from app.models import User, AuditLog, SystemSetting
from app.auth import (
    sign_session, verify_password, hash_password, login_limiter,
    is_initial_setup_required, ROLE_ADMIN, ROLE_OPERATOR
)

class AdminSecuritySetupTestSuite(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        login_limiter.clear_all()
        self.client = TestClient(app)

    def tearDown(self):
        login_limiter.clear_all()
        tests.conftest.seed_test_users()

    @classmethod
    def tearDownClass(cls):
        login_limiter.clear_all()
        tests.conftest.seed_test_users()

    # -------------------------------------------------------------
    # SCENARIO 1: Fresh Installation - Setup Mode Triggered
    # -------------------------------------------------------------
    def test_01_fresh_installation_prompts_admin_setup(self):
        """Scenario 1: Fresh installation triggers setup mode, local users prompted to set up Admin."""
        db = SessionLocal()
        try:
            db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").delete()
            db.query(User).delete()
            db.commit()
            self.assertTrue(is_initial_setup_required(db))
        finally:
            db.close()

        # Local client on /setup loads wizard (200 OK)
        r = self.client.get("/setup")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Welcome to HHD Server Handover System", r.text)
        self.assertIn("Create your company's first Administrator account to get started.", r.text)

        # Root / and /login redirect local browser to /setup
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/setup")

        r = self.client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/setup")

    # -------------------------------------------------------------
    # SCENARIO 2: Valid Setup Form Submission Creates Active Admin
    # -------------------------------------------------------------
    def test_02_valid_setup_form_creates_active_admin(self):
        """Scenario 2: Valid details create active Admin account, hashes password, logs audit, and creates session."""
        db = SessionLocal()
        try:
            db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()

        r = self.client.post("/setup", data={
            "username": "company_admin",
            "full_name": "Rohan Mehra",
            "password": "SecurePassword#2026",
            "confirm_password": "SecurePassword#2026"
        }, follow_redirects=False)

        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin", r.headers["location"])
        self.assertIn("hhd_session", r.cookies)

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "company_admin").first()
            self.assertIsNotNone(user)
            self.assertEqual(user.role, ROLE_ADMIN)
            self.assertEqual(user.full_name, "Rohan Mehra")
            self.assertTrue(user.active)
            # Plaintext password never stored
            self.assertNotEqual(user.password_hash, "SecurePassword#2026")
            self.assertTrue(verify_password("SecurePassword#2026", user.password_hash))

            # Audit log created without fake actor
            audit = db.query(AuditLog).filter(
                AuditLog.action == "INITIAL_ADMIN_SETUP",
                AuditLog.username == "company_admin"
            ).first()
            self.assertIsNotNone(audit)
            self.assertIsNone(audit.user_id)
            self.assertEqual(audit.role, ROLE_ADMIN)

            # setup_completed setting recorded
            setting = db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").first()
            self.assertIsNotNone(setting)
            self.assertEqual(setting.value, "true")
        finally:
            db.close()

    # -------------------------------------------------------------
    # SCENARIO 3: Validation - Mismatched or Weak Passwords Rejected
    # -------------------------------------------------------------
    def test_03_validation_rejects_weak_and_mismatched_passwords(self):
        """Scenario 3: Validation rejects mismatched passwords, short passwords, common passwords, and username as password."""
        db = SessionLocal()
        try:
            db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()

        # 1. Short password (< 8 chars)
        r = self.client.post("/setup", data={
            "username": "adminuser",
            "full_name": "Admin User",
            "password": "short",
            "confirm_password": "short"
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least 8 characters", r.text)

        # 2. Mismatched confirmation password
        r = self.client.post("/setup", data={
            "username": "adminuser",
            "full_name": "Admin User",
            "password": "ValidPassword123",
            "confirm_password": "MismatchedPassword123"
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("Passwords do not match", r.text)

        # 3. Password same as username
        r = self.client.post("/setup", data={
            "username": "adminuser",
            "full_name": "Admin User",
            "password": "adminuser",
            "confirm_password": "adminuser"
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot be identical to the username", r.text)

        # 4. Common weak password
        r = self.client.post("/setup", data={
            "username": "adminuser",
            "full_name": "Admin User",
            "password": "password",
            "confirm_password": "password"
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("too common or insecure", r.text)

    # -------------------------------------------------------------
    # SCENARIO 4: Duplicate Usernames Prevented
    # -------------------------------------------------------------
    def test_04_duplicate_usernames_prevented(self):
        """Scenario 4: Registration fails if username already exists in database."""
        db = SessionLocal()
        try:
            db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").delete()
            db.query(User).delete()
            # Insert existing user (e.g. operator)
            existing = User(
                username="existing_worker",
                password_hash=hash_password("WorkerPassword123"),
                full_name="Existing Worker",
                role=ROLE_OPERATOR,
                active=True
            )
            db.add(existing)
            db.commit()
            # Admin still does not exist, so setup is open
            self.assertTrue(is_initial_setup_required(db))
        finally:
            db.close()

        # Attempt to register admin using the duplicate username
        r = self.client.post("/setup", data={
            "username": "existing_worker",
            "full_name": "Second Worker",
            "password": "UniquePassword123",
            "confirm_password": "UniquePassword123"
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("already exists", r.text)

    # -------------------------------------------------------------
    # SCENARIO 5: Subsequent Access to /setup is Blocked After Setup
    # -------------------------------------------------------------
    def test_05_subsequent_setup_access_blocked_after_setup(self):
        """Scenario 5: Once initial Admin is created, subsequent GET /setup redirects to login and POST returns 403."""
        db = SessionLocal()
        try:
            self.assertFalse(is_initial_setup_required(db))
        finally:
            db.close()

        # GET /setup redirects to /login
        r = self.client.get("/setup", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["location"])

        # POST /setup returns 403 Forbidden
        r = self.client.post("/setup", data={
            "username": "attacker_admin",
            "full_name": "Attacker",
            "password": "AttackerPassword123",
            "confirm_password": "AttackerPassword123"
        })
        self.assertEqual(r.status_code, 403)
        self.assertIn("already been completed", r.text)

    # -------------------------------------------------------------
    # SCENARIO 6: Remote Access to /setup From Remote LAN Rejected
    # -------------------------------------------------------------
    def test_06_remote_lan_access_to_setup_rejected(self):
        """Scenario 6: Remote IP accessing /setup or /login during setup mode is rejected."""
        db = SessionLocal()
        try:
            db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()

        # Remote LAN IP accessing GET /setup receives 403 Forbidden
        r = self.client.get("/setup", headers={"x-forwarded-for": "192.168.1.100"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("restricted to local access", r.text)

        # Remote LAN IP attempting POST /setup receives 403 Forbidden
        r = self.client.post("/setup", headers={"x-forwarded-for": "192.168.1.100"}, data={
            "username": "remote_admin",
            "full_name": "Remote Admin",
            "password": "RemotePassword123",
            "confirm_password": "RemotePassword123"
        })
        self.assertEqual(r.status_code, 403)
        self.assertIn("Remote LAN setup registration is strictly blocked", r.text)

        # Remote LAN IP visiting /login or / during setup sees holding page (503 Service Unavailable)
        r = self.client.get("/login", headers={"x-forwarded-for": "192.168.1.100"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("Central Server Setup Required", r.text)

        r = self.client.get("/", headers={"x-forwarded-for": "192.168.1.100"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("Central Server Setup Required", r.text)

    # -------------------------------------------------------------
    # SCENARIO 7: Existing DB With Admin Bypasses Setup
    # -------------------------------------------------------------
    def test_07_existing_database_bypasses_setup(self):
        """Scenario 7: Existing database with Admin proceeds to normal operations without reopening setup."""
        tests.conftest.seed_test_users()
        db = SessionLocal()
        try:
            self.assertFalse(is_initial_setup_required(db))
            # Even if an existing admin is deactivated, setup MUST NOT reopen
            admin_user = db.query(User).filter(User.role == ROLE_ADMIN).first()
            if admin_user:
                admin_user.active = False
                db.commit()
            self.assertFalse(is_initial_setup_required(db))
            # Restore active status
            if admin_user:
                admin_user.active = True
                db.commit()
        finally:
            db.close()

        # Login page serves normal login form
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign In", r.text)

    # -------------------------------------------------------------
    # SCENARIO 8: Server Restarts Preserve Admin Without Requesting Setup
    # -------------------------------------------------------------
    def test_08_server_restart_preserves_admin_and_locks_setup(self):
        """Scenario 8: Database preserves admin state across app sessions without requesting setup again."""
        tests.conftest.seed_test_users()
        fresh_client = TestClient(app)

        # Fresh request checks DB directly
        db = SessionLocal()
        try:
            admin_user = db.query(User).filter(User.role == ROLE_ADMIN).first()
            self.assertIsNotNone(admin_user)
            self.assertFalse(is_initial_setup_required(db))
        finally:
            db.close()

        # Setup remains locked
        r = fresh_client.get("/setup", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["location"])

    # -------------------------------------------------------------
    # SCENARIO 9: Absolute Database Paths Handled Correctly
    # -------------------------------------------------------------
    def test_09_absolute_database_paths_handled_correctly(self):
        """Scenario 9: Absolute database paths are handled correctly and independent of execution directory."""
        self.assertTrue(DB_PATH.is_absolute())
        self.assertTrue(DB_PATH.parent.exists())

        # Test database state verification utility
        state = verify_database_state()
        self.assertIn(state["status"], ("OPERATIONAL", "INITIALIZING", "FRESH"))
        self.assertEqual(state["db_path"], str(DB_PATH))

        # Changing working directory does not disrupt DB_PATH resolution
        original_cwd = os.getcwd()
        try:
            os.chdir(str(BASE_DIR.parent))
            self.assertTrue(DB_PATH.is_absolute())
            self.assertTrue(Path(state["db_path"]).is_absolute())
        finally:
            os.chdir(original_cwd)

    # -------------------------------------------------------------
    # ADDITIONAL SECURITY TESTS: Operator Creation & RBAC & Rate Limiting
    # -------------------------------------------------------------
    def test_10_admin_creates_operator_with_strong_password(self):
        """Admin creates operator account with minimum 8-character password enforcement."""
        tests.conftest.seed_test_users()
        admin_token = sign_session({"username": "hhdadmin", "role": "admin"})
        client = TestClient(app, cookies={"hhd_session": admin_token})

        # Operator with weak password rejected
        r = client.post("/admin/users", data={
            "username": "operator_test",
            "full_name": "Test Operator",
            "password": "short",
            "role": "operator"
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("minimum+8+character", r.headers["location"])

        # Operator with valid password created
        r = client.post("/admin/users", data={
            "username": "operator_test",
            "full_name": "Test Operator",
            "password": "OperatorPass#2026",
            "role": "operator"
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("User+account+created+successfully", r.headers["location"])

    def test_11_login_rate_limiting_and_lockout(self):
        """5 failed login attempts trigger lockout (429 Too Many Requests)."""
        tests.conftest.seed_test_users()
        login_limiter.reset("hhdadmin")
        login_limiter.reset("127.0.0.1")

        for _ in range(4):
            r = self.client.post("/login", data={
                "username": "hhdadmin",
                "password": "WrongPassword123"
            })
            self.assertEqual(r.status_code, 400)
            self.assertIn("remaining before temporary lockout", r.text)

        # 5th attempt locks out
        r = self.client.post("/login", data={
            "username": "hhdadmin",
            "password": "WrongPassword123"
        })
        self.assertEqual(r.status_code, 429)
        self.assertIn("locked", r.text.lower())

        login_limiter.reset("hhdadmin")
        login_limiter.reset("127.0.0.1")
