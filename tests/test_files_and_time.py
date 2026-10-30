import os
import sys
import unittest
import sqlite3
import io
from pathlib import Path
from datetime import datetime, date, time, timedelta

from fastapi.testclient import TestClient

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from main import app, seed_initial_data_if_needed
from app.config import BACKUP_DIR, DB_PATH
from app.database import engine, Base, SessionLocal
from app.models import User, Device, Employee, Transaction, DeviceAssignment
from app.auth import sign_session, decode_session
from app.shifts import get_current_shift_and_op_date
from app.timefmt import fmt_time, fmt_datetime
from app.backup import (
    safe_backup_path, create_safe_backup, restore_backup,
    list_backups, inspect_db_file
)

class FilesAndTimeTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()

    def setUp(self):
        self.client = TestClient(app)
        self.admin_token = sign_session({"username": "hhdadmin", "role": "admin"})
        self.op_token = sign_session({"username": "operator1", "role": "operator"})

    # -------------------------------------------------------------
    # 1. 12-Hour Time Formatting & Edge Cases
    # -------------------------------------------------------------
    def test_12_hour_edge_cases(self):
        # 00:05 -> 12:05 AM
        self.assertEqual(fmt_time(time(0, 5), seconds=False), "12:05 AM")
        self.assertEqual(fmt_time(datetime(2026, 10, 30, 0, 5, 0), seconds=False), "12:05 AM")
        self.assertEqual(fmt_time("00:05", seconds=False), "12:05 AM")
        self.assertEqual(fmt_time(time(0, 5), seconds=True), "12:05:00 AM")

        # 12:00 -> 12:00 PM
        self.assertEqual(fmt_time(time(12, 0), seconds=False), "12:00 PM")
        self.assertEqual(fmt_time(datetime(2026, 10, 30, 12, 0, 0), seconds=False), "12:00 PM")
        self.assertEqual(fmt_time("12:00", seconds=False), "12:00 PM")
        self.assertEqual(fmt_time(time(12, 0), seconds=True), "12:00:00 PM")

        # 23:59 -> 11:59 PM
        self.assertEqual(fmt_time(time(23, 59), seconds=False), "11:59 PM")
        self.assertEqual(fmt_time(datetime(2026, 10, 30, 23, 59, 0), seconds=False), "11:59 PM")
        self.assertEqual(fmt_time("23:59", seconds=False), "11:59 PM")
        self.assertEqual(fmt_time(time(23, 59), seconds=True), "11:59:00 PM")

        # 00:00 midnight -> 12:00 AM
        self.assertEqual(fmt_time(time(0, 0), seconds=False), "12:00 AM")

        # Full datetime 12-hour format: 2026-10-30 03:45:12 PM
        dt = datetime(2026, 10, 30, 15, 45, 12)
        self.assertEqual(fmt_datetime(dt, seconds=True), "2026-10-30 03:45:12 PM")
        self.assertEqual(fmt_datetime(dt, seconds=False), "2026-10-30 03:45 PM")

    # -------------------------------------------------------------
    # 2. Login Without Shift & Automatic Shift from Scan Clock
    # -------------------------------------------------------------
    def test_login_works_without_shift_and_shift_is_automatic(self):
        # 1. Login with only username and password (no shift_name sent)
        resp = self.client.post("/login", data={
            "username": "operator1",
            "password": "operator123"
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        cookie = resp.cookies.get("hhd_session")
        self.assertIsNotNone(cookie)

        # 2. Verify shift_name is NOT in session cookie
        session_data = decode_session(cookie)
        self.assertNotIn("shift_name", session_data)
        self.assertEqual(session_data.get("username"), "operator1")

        # 3. Simulate legacy client sending shift_name in cookie or body
        legacy_token = sign_session({"username": "operator1", "role": "operator", "shift_name": "BogusShift"})
        self.client.cookies.set("hhd_session", legacy_token)

        # Get test device & employee
        db = SessionLocal()
        dev = db.query(Device).filter(Device.status == "Available").first()
        emp = db.query(Employee).filter(Employee.active == True).first()
        self.assertIsNotNone(dev)
        self.assertIsNotNone(emp)
        dev_serial = dev.serial
        emp_id = emp.employee_id
        db.close()

        scan_resp = self.client.post("/api/scan", json={
            "code": dev_serial,
            "mode": "Issue",
            "pending": {"type": "employee", "code": emp_id},
            "shift_name": "BogusShift"  # Legacy field in payload
        })
        self.assertEqual(scan_resp.status_code, 200)
        self.assertTrue(scan_resp.json().get("ok"))

        # Verify transaction recorded shift from server clock, ignoring BogusShift
        clock_shift, clock_opdate = get_current_shift_and_op_date()
        db = SessionLocal()
        tx = db.query(Transaction).filter(Transaction.serial == dev_serial).order_by(Transaction.id.desc()).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.shift_name, clock_shift)
        self.assertEqual(tx.operational_date, clock_opdate)
        self.assertNotEqual(tx.shift_name, "BogusShift")

        # Cleanup: Return the device
        ret_resp = self.client.post("/api/return", json={
            "serial": dev_serial,
            "condition": "Working"
        })
        self.assertEqual(ret_resp.status_code, 200)
        db.close()

    # -------------------------------------------------------------
    # 3. File Upload, Schema Verification, & Security
    # -------------------------------------------------------------
    def _create_valid_sqlite_db_bytes(self) -> bytes:
        """Creates a byte stream of a SQLite database matching Base.metadata core tables."""
        temp_file = BACKUP_DIR / f"_temp_valid_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        con = sqlite3.connect(str(temp_file))
        cur = con.cursor()
        
        # Create core tables with all expected columns
        cur.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(64) NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(100) NOT NULL,
                role VARCHAR(20) NOT NULL,
                default_shift VARCHAR(20) NOT NULL,
                active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE employees (
                employee_id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                department VARCHAR(100) NOT NULL,
                active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE devices (
                serial VARCHAR(64) PRIMARY KEY,
                make VARCHAR(50) NOT NULL,
                model VARCHAR(50) NOT NULL,
                cover_qty INTEGER NOT NULL,
                status VARCHAR(30) NOT NULL,
                assigned_employee_id VARCHAR(64),
                condition VARCHAR(30) NOT NULL,
                notes TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE device_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_serial VARCHAR(64) NOT NULL,
                employee_id VARCHAR(64) NOT NULL,
                issued_at DATETIME NOT NULL,
                issued_by_operator_id INTEGER,
                issued_by_operator_name VARCHAR(100) NOT NULL,
                issued_shift VARCHAR(20) NOT NULL,
                issued_operational_date VARCHAR(10) NOT NULL,
                returned_at DATETIME,
                returned_by_operator_id INTEGER,
                returned_by_operator_name VARCHAR(100),
                returned_shift VARCHAR(20),
                returned_operational_date VARCHAR(10),
                status VARCHAR(20) NOT NULL,
                condition_on_return VARCHAR(30),
                remarks TEXT NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_time DATETIME NOT NULL,
                operational_date VARCHAR(10) NOT NULL,
                shift_name VARCHAR(20) NOT NULL,
                transaction_type VARCHAR(20) NOT NULL,
                serial VARCHAR(64) NOT NULL,
                employee_id VARCHAR(64) NOT NULL,
                employee_name VARCHAR(120) NOT NULL,
                department VARCHAR(100) NOT NULL,
                operator_id INTEGER,
                operator_name VARCHAR(100) NOT NULL,
                condition VARCHAR(30) NOT NULL,
                cover_qty INTEGER NOT NULL,
                remarks TEXT NOT NULL,
                idempotency_key VARCHAR(100)
            );
        """)
        # Insert sample row
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(f"INSERT INTO users VALUES (1, 'u1', 'p1', 'User One', 'admin', 'Morning', 1, '{now_str}', '{now_str}', NULL);".replace(", NULL);", ");"))
        con.commit()
        con.close()

        data = temp_file.read_bytes()
        temp_file.unlink()
        return data

    def test_file_upload_valid(self):
        self.client.cookies.set("hhd_session", self.admin_token)
        db_bytes = self._create_valid_sqlite_db_bytes()

        resp = self.client.post("/admin/backups/upload", files={
            "file": ("warehouse_clone.db", io.BytesIO(db_bytes), "application/octet-stream")
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("uploaded_", resp.headers.get("location", ""))

        # Check list_backups sees it as Uploaded badge
        backups = list_backups()
        uploaded = [b for b in backups if b["type"] == "Uploaded"]
        self.assertGreater(len(uploaded), 0)
        self.assertTrue(uploaded[0]["valid"])
        self.assertGreaterEqual(uploaded[0]["counts"]["users"], 1)

    def test_file_upload_rejections_and_temp_cleanup(self):
        self.client.cookies.set("hhd_session", self.admin_token)

        # 1. Empty file
        resp = self.client.post("/admin/backups/upload", files={
            "file": ("empty.db", io.BytesIO(b""), "application/octet-stream")
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error", resp.headers.get("location", ""))
        # Verify no .part file remains
        part_files = list(BACKUP_DIR.glob(".upload_*.part"))
        self.assertEqual(len(part_files), 0)

        # 2. Text file named x.db
        resp = self.client.post("/admin/backups/upload", files={
            "file": ("x.db", io.BytesIO(b"Just plain text pretending to be sqlite"), "application/octet-stream")
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error", resp.headers.get("location", ""))
        self.assertEqual(len(list(BACKUP_DIR.glob(".upload_*.part"))), 0)

        # 3. Non-sqlite extension .txt
        resp = self.client.post("/admin/backups/upload", files={
            "file": ("test.txt", io.BytesIO(b"hello world"), "text/plain")
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error", resp.headers.get("location", ""))
        self.assertEqual(len(list(BACKUP_DIR.glob(".upload_*.part"))), 0)

        # 4. Foreign SQLite database (missing required warehouse tables)
        foreign_file = BACKUP_DIR / "temp_foreign.db"
        con = sqlite3.connect(str(foreign_file))
        con.execute("CREATE TABLE blog_posts (id INT, title TEXT);")
        con.commit()
        con.close()
        foreign_bytes = foreign_file.read_bytes()
        foreign_file.unlink()

        resp = self.client.post("/admin/backups/upload", files={
            "file": ("foreign.db", io.BytesIO(foreign_bytes), "application/octet-stream")
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error", resp.headers.get("location", ""))
        self.assertEqual(len(list(BACKUP_DIR.glob(".upload_*.part"))), 0)

        # 5. Corrupt file with SQLite header followed by bad data
        corrupt_bytes = b"SQLite format 3\x00" + b"\xff" * 200
        resp = self.client.post("/admin/backups/upload", files={
            "file": ("corrupt.db", io.BytesIO(corrupt_bytes), "application/octet-stream")
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error", resp.headers.get("location", ""))
        self.assertEqual(len(list(BACKUP_DIR.glob(".upload_*.part"))), 0)

    def test_safe_backup_path_directory_traversal(self):
        # Directory traversal attempts must raise ValueError
        traversal_attempts = [
            "../etc/passwd.db",
            "..\\windows\\system32.db",
            "/absolute/root.db",
            ".hidden.db",
            "normal_file.txt",
            "nested/dir/file.db",
            "",
            "   "
        ]
        for path in traversal_attempts:
            with self.assertRaises(ValueError):
                safe_backup_path(path)

        # Download route rejects invalid filename extension
        self.client.cookies.set("hhd_session", self.admin_token)
        resp = self.client.get("/admin/backups/download/invalid_not_db.txt")
        self.assertEqual(resp.status_code, 400)

    def test_operator_forbidden_on_backup_management(self):
        self.client.cookies.set("hhd_session", self.op_token)

        # 1. Upload
        resp_upload = self.client.post("/admin/backups/upload", files={
            "file": ("test.db", io.BytesIO(b"dummy"), "application/octet-stream")
        })
        self.assertEqual(resp_upload.status_code, 403)

        # 2. Delete
        resp_del = self.client.post("/admin/backups/delete", data={"filename": "test.db"})
        self.assertEqual(resp_del.status_code, 403)

        # 3. Download
        resp_dl = self.client.get("/admin/backups/download/test.db")
        self.assertEqual(resp_dl.status_code, 403)

        # 4. Restore
        resp_res = self.client.post("/admin/backups/restore", data={"filename": "test.db"})
        self.assertEqual(resp_res.status_code, 403)

    # -------------------------------------------------------------
    # 4. Pre-Restore Safety Copy via SQLite Backup API
    # -------------------------------------------------------------
    def test_restore_creates_safety_copy_via_sqlite_backup_api(self):
        db = SessionLocal()
        dev = db.query(Device).first()
        dev_serial = dev.serial
        original_notes = dev.notes
        db.close()

        # Create a snapshot before any edits
        snapshot = create_safe_backup("pre_restore_verification")
        snapshot_filename = snapshot["filename"]

        # Mutate the live database
        db = SessionLocal()
        dev = db.query(Device).filter(Device.serial == dev_serial).first()
        dev.notes = "Mutated Note Just Before Restore"
        db.commit()
        db.close()

        # Perform restore
        success = restore_backup(snapshot_filename)
        self.assertTrue(success)

        # Verify a pre_restore_*.db safety copy was created
        pre_restore_files = list(BACKUP_DIR.glob("pre_restore_*.db"))
        self.assertGreater(len(pre_restore_files), 0)

        # Verify the safety copy captured the pre-restore mutated state
        latest_safety_copy = max(pre_restore_files, key=lambda f: f.stat().st_mtime)
        con = sqlite3.connect(f"{latest_safety_copy.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        cur = con.cursor()
        saved_note = cur.execute("SELECT notes FROM devices WHERE serial = ?", (dev_serial,)).fetchone()[0]
        con.close()
        self.assertEqual(saved_note, "Mutated Note Just Before Restore")

        # Verify the live DB was restored back to original_notes
        db = SessionLocal()
        restored_dev = db.query(Device).filter(Device.serial == dev_serial).first()
        self.assertEqual(restored_dev.notes, original_notes)
        db.close()

    # -------------------------------------------------------------
    # 5. Live Board Versioning & Conditional Polling
    # -------------------------------------------------------------
    def test_board_versioning_and_conditional_return(self):
        # 1. Fetch initial live board data
        resp1 = self.client.get("/api/board/live")
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertTrue(data1.get("changed"))
        board = data1.get("board")
        self.assertIsNotNone(board)
        version = board.get("version")
        self.assertIsNotNone(version)

        # 2. Poll with same version string -> must return {"changed": False}
        resp2 = self.client.get(f"/api/board/live?v={version}")
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2, {"changed": False})

        # 3. Modify a device to bump updated_at
        db = SessionLocal()
        dev = db.query(Device).first()
        dev.updated_at = datetime.now() + timedelta(seconds=2)
        db.commit()
        db.close()

        # 4. Poll again with previous version string -> must return {"changed": True}
        resp3 = self.client.get(f"/api/board/live?v={version}")
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json()
        self.assertTrue(data3.get("changed"))
        new_version = data3.get("board", {}).get("version")
        self.assertNotEqual(new_version, version)

if __name__ == "__main__":
    unittest.main()
