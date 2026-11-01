import os
import sys
import unittest
from datetime import datetime
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest

from main import app
from app.database import engine, Base, SessionLocal
from app.models import Device, DamageReport, User, Employee
from app.auth import sign_session
from app.quarantine import check_quarantine_discrepancies, reconcile_quarantine
from app.scan_engine import return_device_atomic

class QuarantineReconciliationTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        tests.conftest.seed_test_users()

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.client = TestClient(app)
        self.admin_token = sign_session({"username": "hhdadmin", "role": "admin"})
        self.op_token = sign_session({"username": "operator1", "role": "operator"})

    def test_01_damaged_device_without_ticket(self):
        """A device with status Damaged but no DamageReport is flagged and reconciled."""
        db = SessionLocal()
        try:
            # Create isolated test device
            serial = "TEST_DMG_NO_TICKET_01"
            db.query(DamageReport).filter(DamageReport.serial == serial).delete()
            dev = db.query(Device).filter(Device.serial == serial).first()
            if not dev:
                dev = Device(
                    serial=serial,
                    make="ZEBRA",
                    model="TC22",
                    department="LOGISTICS",
                    status="Damaged",
                    condition="Damaged",
                    notes="Screen cracked"
                )
                db.add(dev)
            else:
                dev.status = "Damaged"
                dev.condition = "Damaged"
            db.commit()

            # 1. Check detects discrepancy
            disc = check_quarantine_discrepancies(db)
            self.assertTrue(disc["has_issues"])
            serials = [d.serial for d in disc["damaged_without_records"]]
            self.assertIn(serial, serials)

            # 2. Dry run reports 1 ticket to create
            dry = reconcile_quarantine(db, dry_run=True)
            self.assertTrue(dry["dry_run"])
            self.assertIn(serial, dry["affected_serials"])

            # 3. Apply reconciliation
            res = reconcile_quarantine(db, dry_run=False, triggered_by="Test Admin")
            self.assertFalse(res["dry_run"])
            self.assertTrue(res["executed"])
            self.assertIn(serial, res["affected_serials"])

            # 4. Verify created ticket
            ticket = db.query(DamageReport).filter(
                DamageReport.serial == serial,
                DamageReport.review_status == "Pending Inspection"
            ).first()
            self.assertIsNotNone(ticket)
            self.assertEqual(ticket.source, "System Reconciliation")
            self.assertEqual(ticket.department, "LOGISTICS")

            # 5. Check reports zero issues for this device
            post_disc = check_quarantine_discrepancies(db)
            post_serials = [d.serial for d in post_disc["damaged_without_records"]]
            self.assertNotIn(serial, post_serials)
        finally:
            db.close()

    def test_02_under_repair_device_without_ticket(self):
        """A device with status Under Repair but no ticket is flagged and reconciled to Under Repair."""
        db = SessionLocal()
        try:
            serial = "TEST_REP_NO_TICKET_02"
            db.query(DamageReport).filter(DamageReport.serial == serial).delete()
            dev = db.query(Device).filter(Device.serial == serial).first()
            if not dev:
                dev = Device(
                    serial=serial,
                    make="CHAINWAY",
                    model="MC95",
                    department="OUTBOUND",
                    status="Under Repair",
                    condition="Under Repair",
                    notes="Sent to service center"
                )
                db.add(dev)
            else:
                dev.status = "Under Repair"
                dev.condition = "Under Repair"
            db.commit()

            # Check detects discrepancy
            disc = check_quarantine_discrepancies(db)
            rep_serials = [d.serial for d in disc["repair_without_records"]]
            self.assertIn(serial, rep_serials)

            # Reconcile
            res = reconcile_quarantine(db, dry_run=False, triggered_by="Test Admin")
            self.assertIn(serial, res["affected_serials"])

            ticket = db.query(DamageReport).filter(
                DamageReport.serial == serial,
                DamageReport.review_status == "Under Repair"
            ).first()
            self.assertIsNotNone(ticket)
            self.assertEqual(ticket.source, "System Reconciliation")
        finally:
            db.close()

    def test_03_device_with_existing_open_ticket_prevents_duplicate(self):
        """A device that already has an active ticket is preserved; no duplicate ticket is created."""
        db = SessionLocal()
        try:
            serial = "TEST_DEV_OPEN_EXISTS_03"
            dev = db.query(Device).filter(Device.serial == serial).first()
            if not dev:
                dev = Device(serial=serial, make="HONEYWELL", model="EDA51", status="Damaged", condition="Damaged")
                db.add(dev)
            else:
                dev.status = "Damaged"
            db.flush()

            # Ensure single open ticket
            db.query(DamageReport).filter(DamageReport.serial == serial).delete()
            t = DamageReport(
                serial=serial,
                fault_type="Blank screen",
                details="Original operator note",
                source="Operator Report",
                reported_by_name="Rahul",
                review_status="Open"
            )
            db.add(t)
            db.commit()

            # Check detects zero discrepancy for this device
            disc = check_quarantine_discrepancies(db)
            self.assertNotIn(serial, [d.serial for d in disc["damaged_without_records"]])

            # Reconcile does not touch this device
            res = reconcile_quarantine(db, dry_run=False)
            self.assertNotIn(serial, res["affected_serials"])

            # Verify only 1 ticket remains
            cnt = db.query(DamageReport).filter(DamageReport.serial == serial).count()
            self.assertEqual(cnt, 1)
        finally:
            db.close()

    def test_04_device_with_only_closed_ticket_requires_new_ticket(self):
        """If a device is currently Damaged but its only existing ticket is Closed, reconciliation creates a new ticket."""
        db = SessionLocal()
        try:
            serial = "TEST_DEV_CLOSED_ONLY_04"
            dev = db.query(Device).filter(Device.serial == serial).first()
            if not dev:
                dev = Device(serial=serial, make="ZEBRA", model="TC22", status="Damaged", condition="Damaged")
                db.add(dev)
            else:
                dev.status = "Damaged"
            db.flush()

            # Only a Closed ticket exists
            db.query(DamageReport).filter(DamageReport.serial == serial).delete()
            old_t = DamageReport(
                serial=serial,
                fault_type="Previous fault",
                review_status="Closed",
                source="Operator Report",
                reported_by_name="Amit"
            )
            db.add(old_t)
            db.commit()

            # Discrepancy checker flags it because no OPEN ticket exists
            disc = check_quarantine_discrepancies(db)
            self.assertIn(serial, [d.serial for d in disc["damaged_without_records"]])

            # Reconcile creates new active ticket
            res = reconcile_quarantine(db, dry_run=False)
            self.assertIn(serial, res["affected_serials"])

            # Verify 2 tickets now exist: 1 Closed (preserved historical), 1 Pending Inspection
            all_t = db.query(DamageReport).filter(DamageReport.serial == serial).all()
            self.assertEqual(len(all_t), 2)
            statuses = [t.review_status for t in all_t]
            self.assertIn("Closed", statuses)
            self.assertIn("Pending Inspection", statuses)
        finally:
            db.close()

    def test_05_repeated_reconciliation_is_idempotent(self):
        """Calling reconcile_quarantine multiple times results in 0 additional tickets."""
        db = SessionLocal()
        try:
            res1 = reconcile_quarantine(db, dry_run=False)
            res2 = reconcile_quarantine(db, dry_run=False)
            self.assertEqual(res2["new_tickets_to_create"], 0)
            self.assertEqual(res2["created_tickets_count"], 0)
        finally:
            db.close()

    def test_06_operator_reported_damage_synchronization(self):
        """Operator returning device as damaged sets dev.status='Damaged' and creates DamageReport with source='Operator Report'."""
        db = SessionLocal()
        try:
            serial = "TEST_OP_RETURN_DMG_06"
            emp_id = "EMP_RECON_06"
            dev = db.query(Device).filter(Device.serial == serial).first()
            if not dev:
                dev = Device(serial=serial, status="Issued", assigned_employee_id=emp_id)
                db.add(dev)
            else:
                dev.status = "Issued"
                dev.assigned_employee_id = emp_id

            emp = db.query(Employee).filter(Employee.employee_id == emp_id).first()
            if not emp:
                db.add(Employee(employee_id=emp_id, name="Operator Test Worker", department="INVENTORY", active=True))
            db.commit()

            op = db.query(User).filter(User.username == "operator1").first()

            # Return with damage
            res = return_device_atomic(
                db=db,
                device_serial=serial,
                operator=op,
                condition="Damaged",
                damage_type="Blank screen",
                remarks="Dropped on warehouse floor"
            )
            self.assertEqual(res["action"], "Return")

            # Verify device status is Damaged
            dev = db.query(Device).filter(Device.serial == serial).first()
            self.assertEqual(dev.status, "Damaged")

            # Verify DamageReport exists with Operator Report source
            dmg = db.query(DamageReport).filter(DamageReport.serial == serial, DamageReport.review_status == "Open").first()
            self.assertIsNotNone(dmg)
            self.assertEqual(dmg.source, "Operator Report")
            self.assertEqual(dmg.fault_type, "Blank screen")

            # Discrepancy checker confirms consistent state
            disc = check_quarantine_discrepancies(db)
            self.assertNotIn(serial, [d.serial for d in disc["damaged_without_records"]])
        finally:
            db.close()

    def test_07_device_repaired_and_verified(self):
        """Updating damage ticket to 'Repaired/Available' automatically transitions device to Available in inventory."""
        db = SessionLocal()
        try:
            serial = "TEST_DEV_REPAIRED_07"
            dev = db.query(Device).filter(Device.serial == serial).first()
            if not dev:
                dev = Device(serial=serial, status="Under Repair", condition="Under Repair")
                db.add(dev)
            else:
                dev.status = "Under Repair"
            db.flush()

            db.query(DamageReport).filter(DamageReport.serial == serial).delete()
            t = DamageReport(
                serial=serial,
                fault_type="Under Repair",
                review_status="Under Repair",
                source="System Reconciliation",
                reported_by_name="System Reconciliation"
            )
            db.add(t)
            db.commit()

            # Admin updates ticket via UI endpoint
            resp = self.client.post(
                f"/admin/damage/{t.id}/update",
                data={"review_status": "Repaired/Available", "resolution_notes": "Main board replaced by vendor"},
                cookies={"hhd_session": self.admin_token},
                follow_redirects=False
            )
            self.assertEqual(resp.status_code, 302)

            db.expire_all()
            dev = db.query(Device).filter(Device.serial == serial).first()
            self.assertEqual(dev.status, "Available")
            self.assertEqual(dev.condition, "Working")

            # Discrepancy checker confirms consistent state
            disc = check_quarantine_discrepancies(db)
            self.assertNotIn(serial, [d.serial for d in disc["damaged_without_records"]])
            self.assertNotIn(serial, [d.serial for d in disc["repair_without_records"]])
        finally:
            db.close()

    def test_08_status_mismatch_detection(self):
        """Available or Issued device with an unresolved damage ticket is detected by discrepancy checker."""
        db = SessionLocal()
        try:
            serial = "TEST_DEV_MISMATCH_08"
            dev = db.query(Device).filter(Device.serial == serial).first()
            if not dev:
                dev = Device(serial=serial, status="Available", condition="Working")
                db.add(dev)
            else:
                dev.status = "Available"
            db.flush()

            # Create orphan open damage report
            db.query(DamageReport).filter(DamageReport.serial == serial).delete()
            t = DamageReport(
                serial=serial,
                fault_type="Camera glass shattered",
                review_status="Open",
                source="Operator Report",
                reported_by_name="Rahul"
            )
            db.add(t)
            db.commit()

            # Discrepancy checker flags available device with open damage report
            disc = check_quarantine_discrepancies(db)
            self.assertTrue(disc["has_issues"])
            mismatch_serials = [r.serial for r in disc["open_dmg_available"]]
            self.assertIn(serial, mismatch_serials)

            # Cleanup
            db.delete(t)
            db.commit()
        finally:
            db.close()

    def test_09_large_batch_preserves_exact_text_serials(self):
        """Batch of devices with alphanumeric, lowercase, uppercase, and leading zeros are preserved as exact text."""
        db = SessionLocal()
        try:
            batch_serials = [
                "00078912",          # Leading zeros
                "001234",            # Leading zeros
                "s065490300165260",   # Lowercase 's'
                "HONEY_999",         # Uppercase underscore
                "DEV-inv.456#A"      # Mixed symbols
            ]

            for s in batch_serials:
                db.query(DamageReport).filter(DamageReport.serial == s).delete()
                dev = db.query(Device).filter(Device.serial == s).first()
                if not dev:
                    dev = Device(serial=s, status="Damaged", condition="Damaged", department="TEST_DEPT")
                    db.add(dev)
                else:
                    dev.status = "Damaged"
            db.commit()

            # Check flags all
            disc = check_quarantine_discrepancies(db)
            dmg_serials = set(d.serial for d in disc["damaged_without_records"])
            for s in batch_serials:
                self.assertIn(s, dmg_serials)

            # Reconcile
            res = reconcile_quarantine(db, dry_run=False, triggered_by="Batch Test")
            for s in batch_serials:
                self.assertIn(s, res["affected_serials"])

                # Check exact string match in DamageReport
                t = db.query(DamageReport).filter(
                    DamageReport.serial == s,
                    DamageReport.review_status == "Pending Inspection"
                ).first()
                self.assertIsNotNone(t)
                self.assertEqual(t.serial, s)
        finally:
            db.close()

    def test_10_admin_ui_damage_and_reconcile_pages(self):
        """Test GET /admin/damage, GET /admin/damage/reconcile and POST /admin/damage/reconcile."""
        # 1. GET /admin/damage renders correctly
        r = self.client.get("/admin/damage", cookies={"hhd_session": self.admin_token})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Damage & Repair Management", r.text)
        self.assertIn("Record Source", r.text)

        # 2. GET /admin/damage/reconcile renders dry run preview
        r_rec = self.client.get("/admin/damage/reconcile", cookies={"hhd_session": self.admin_token})
        self.assertEqual(r_rec.status_code, 200)
        self.assertIn("Quarantine Reconciliation Preview", r_rec.text)

        # 3. POST /admin/damage/reconcile applies reconciliation and redirects
        r_post = self.client.post("/admin/damage/reconcile", cookies={"hhd_session": self.admin_token}, follow_redirects=False)
        self.assertEqual(r_post.status_code, 302)
        self.assertIn("/admin/damage", r_post.headers["location"])
