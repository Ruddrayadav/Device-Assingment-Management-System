import os
import sys
import unittest
import sqlite3
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from app.config import DATA_DIR, BACKUP_DIR, DB_PATH
from app.database import engine, Base, SessionLocal
from app.models import User, Device, Employee
from app.backup import create_safe_backup, list_backups, verify_backup_integrity, restore_backup
from app.auth import hash_password
from tests.conftest import seed_initial_data_if_needed

class BackupRestoreTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        seed_initial_data_if_needed()
        db = SessionLocal()
        # Seed test device
        dev = db.query(Device).filter(Device.serial == "BACKUP_TEST_001").first()
        if not dev:
            db.add(Device(
                serial="BACKUP_TEST_001",
                make="TEST_MAKE",
                model="TEST_MODEL",
                status="Available",
                condition="Working"
            ))
            db.commit()
        db.close()

    def test_backup_creation_and_integrity(self):
        res = create_safe_backup(custom_tag="unit_test")
        self.assertTrue(os.path.exists(res["path"]))
        self.assertGreater(res["size_bytes"], 0)
        self.assertIn("unit_test", res["filename"])

        # Integrity verification
        is_valid = verify_backup_integrity(res["path"])
        self.assertTrue(is_valid)

        # Check list backups
        backups = list_backups()
        filenames = [b["filename"] for b in backups]
        self.assertIn(res["filename"], filenames)

    def test_backup_restore_roundtrip(self):
        db = SessionLocal()
        # 1. Modify data before backup
        dev = db.query(Device).filter(Device.serial == "BACKUP_TEST_001").first()
        dev.notes = "Original Note Before Snapshot"
        db.commit()
        db.close()

        # 2. Take backup
        b_res = create_safe_backup(custom_tag="snapshot_state")
        backup_filename = b_res["filename"]

        # 3. Mutate data after backup
        db = SessionLocal()
        dev = db.query(Device).filter(Device.serial == "BACKUP_TEST_001").first()
        dev.notes = "Corrupted or Changed After Snapshot"
        db.commit()
        db.close()

        # 4. Perform Restore
        success = restore_backup(backup_filename)
        self.assertTrue(success)

        # 5. Verify restored state
        db = SessionLocal()
        dev_restored = db.query(Device).filter(Device.serial == "BACKUP_TEST_001").first()
        self.assertEqual(dev_restored.notes, "Original Note Before Snapshot")
        db.close()

if __name__ == "__main__":
    unittest.main()

