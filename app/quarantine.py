from datetime import datetime, date
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import Device, DamageReport, AuditLog
from app.backup import create_safe_backup
from app.config import now_kolkata, today_kolkata_str

OPEN_DAMAGE_STATUSES = ["Open", "Pending Inspection"]
OPEN_REPAIR_STATUSES = ["Under Repair"]
ALL_ACTIVE_QUARANTINE_STATUSES = ["Open", "Pending Inspection", "Under Repair"]

def check_quarantine_discrepancies(db: Session) -> Dict[str, Any]:
    """
    Exhaustively checks device inventory and quarantine/damage records for discrepancies:
    1. Damaged devices lacking an open damage report (Pending Inspection or Open).
    2. Under Repair devices lacking an Under Repair ticket.
    3. Available devices incorrectly flagged with an active quarantine report.
    4. Issued devices incorrectly flagged with an active quarantine report.
    5. Devices having duplicate active quarantine reports.
    """
    # Active open damage tickets
    open_dmg_serials = set(
        r[0] for r in db.query(DamageReport.serial)
        .filter(DamageReport.review_status.in_(OPEN_DAMAGE_STATUSES))
        .all()
    )

    # Active repair tickets (or open damage transitioning to repair)
    open_rep_serials = set(
        r[0] for r in db.query(DamageReport.serial)
        .filter(DamageReport.review_status.in_(ALL_ACTIVE_QUARANTINE_STATUSES))
        .all()
    )

    # 1. Damaged devices with no open damage report
    damaged_without_records = db.query(Device).filter(
        Device.status == "Damaged",
        Device.serial.notin_(open_dmg_serials) if open_dmg_serials else True
    ).order_by(Device.serial.asc()).all()

    # 2. Under Repair devices with no repair ticket
    repair_without_records = db.query(Device).filter(
        Device.status == "Under Repair",
        Device.serial.notin_(open_rep_serials) if open_rep_serials else True
    ).order_by(Device.serial.asc()).all()

    # 3. Available devices with open damage report
    avail_serials = set(r[0] for r in db.query(Device.serial).filter(Device.status == "Available").all())
    open_dmg_available = db.query(DamageReport).filter(
        DamageReport.review_status.in_(ALL_ACTIVE_QUARANTINE_STATUSES),
        DamageReport.serial.in_(avail_serials) if avail_serials else False
    ).order_by(DamageReport.id.desc()).all()

    # 4. Issued devices with open damage report
    issued_serials = set(r[0] for r in db.query(Device.serial).filter(Device.status == "Issued").all())
    open_dmg_issued = db.query(DamageReport).filter(
        DamageReport.review_status.in_(ALL_ACTIVE_QUARANTINE_STATUSES),
        DamageReport.serial.in_(issued_serials) if issued_serials else False
    ).order_by(DamageReport.id.desc()).all()

    # 5. Duplicate open tickets for a single device
    dup_rows = db.query(DamageReport.serial, func.count(DamageReport.id)).filter(
        DamageReport.review_status.in_(ALL_ACTIVE_QUARANTINE_STATUSES)
    ).group_by(DamageReport.serial).having(func.count(DamageReport.id) > 1).all()
    duplicate_open_serials = [r[0] for r in dup_rows]

    has_issues = bool(
        damaged_without_records or
        repair_without_records or
        open_dmg_available or
        open_dmg_issued or
        duplicate_open_serials
    )

    return {
        "damaged_without_records": damaged_without_records,
        "repair_without_records": repair_without_records,
        "open_dmg_available": open_dmg_available,
        "open_dmg_issued": open_dmg_issued,
        "duplicate_open_serials": duplicate_open_serials,
        "has_issues": has_issues,
        "counts": {
            "damaged_missing": len(damaged_without_records),
            "repair_missing": len(repair_without_records),
            "available_with_open_ticket": len(open_dmg_available),
            "issued_with_open_ticket": len(open_dmg_issued),
            "duplicate_serials": len(duplicate_open_serials)
        }
    }

def reconcile_quarantine(
    db: Session,
    dry_run: bool = True,
    triggered_by: Optional[str] = None
) -> Dict[str, Any]:
    """
    Idempotently reconciles missing quarantine and repair tickets for all affected devices:
    - Creates 'Pending Inspection' tickets for Damaged devices.
    - Creates 'Under Repair' tickets for Under Repair devices.
    - Identifies source as 'System Reconciliation'.
    - Preserves exact device serial number as text.
    - Avoids duplicate tickets.
    - If dry_run=True, returns audit summary without modifying database.
    - If dry_run=False, takes verified backup before applying modifications.
    """
    discrepancies = check_quarantine_discrepancies(db)
    damaged_to_fix = discrepancies["damaged_without_records"]
    repair_to_fix = discrepancies["repair_without_records"]

    existing_open_cnt = db.query(DamageReport).filter(
        DamageReport.review_status.in_(ALL_ACTIVE_QUARANTINE_STATUSES)
    ).count()

    total_missing = len(damaged_to_fix) + len(repair_to_fix)
    plan: List[Dict[str, Any]] = []
    seen_serials = set()
    duplicates_avoided = 0
    manual_review = []

    # Plan tickets for Damaged devices
    for dev in damaged_to_fix:
        s_exact = str(dev.serial).strip()
        if s_exact in seen_serials:
            duplicates_avoided += 1
            continue
        seen_serials.add(s_exact)

        # Check existing active ticket
        existing_ticket = db.query(DamageReport).filter(
            DamageReport.serial == s_exact,
            DamageReport.review_status.in_(ALL_ACTIVE_QUARANTINE_STATUSES)
        ).first()
        if existing_ticket:
            duplicates_avoided += 1
            continue

        plan.append({
            "serial": s_exact,
            "device_status": "Damaged",
            "make": dev.make,
            "model": dev.model,
            "department": dev.department or "GENERAL",
            "ticket_status": "Pending Inspection",
            "fault_type": dev.condition if dev.condition and dev.condition != "Working" else "Not Working",
            "source": "System Reconciliation",
            "notes": dev.notes or "Identified during quarantine reconciliation"
        })

    # Plan tickets for Under Repair devices
    for dev in repair_to_fix:
        s_exact = str(dev.serial).strip()
        if s_exact in seen_serials:
            duplicates_avoided += 1
            continue
        seen_serials.add(s_exact)

        existing_ticket = db.query(DamageReport).filter(
            DamageReport.serial == s_exact,
            DamageReport.review_status.in_(ALL_ACTIVE_QUARANTINE_STATUSES)
        ).first()
        if existing_ticket:
            duplicates_avoided += 1
            continue

        plan.append({
            "serial": s_exact,
            "device_status": "Under Repair",
            "make": dev.make,
            "model": dev.model,
            "department": dev.department or "GENERAL",
            "ticket_status": "Under Repair",
            "fault_type": "Under Repair",
            "source": "System Reconciliation",
            "notes": dev.notes or "Identified during quarantine reconciliation"
        })

    # Check for anomalies requiring manual review (e.g. available/issued with open ticket)
    for r in discrepancies["open_dmg_available"]:
        manual_review.append({
            "serial": r.serial,
            "issue": f"Device is Available in inventory, but active ticket #{r.id} ({r.review_status}) is open."
        })
    for r in discrepancies["open_dmg_issued"]:
        manual_review.append({
            "serial": r.serial,
            "issue": f"Device is currently Issued, but active ticket #{r.id} ({r.review_status}) is open."
        })
    for s in discrepancies["duplicate_open_serials"]:
        manual_review.append({
            "serial": s,
            "issue": f"Device has multiple duplicate open tickets requiring review."
        })

    if dry_run:
        return {
            "dry_run": True,
            "damaged_missing_count": len(damaged_to_fix),
            "repair_missing_count": len(repair_to_fix),
            "existing_open_tickets": existing_open_cnt,
            "new_tickets_to_create": len(plan),
            "created_tickets_count": 0,
            "duplicates_avoided": duplicates_avoided,
            "records_requiring_manual_review": manual_review,
            "planned_tickets": plan,
            "affected_serials": [p["serial"] for p in plan],
            "backup_filename": None,
            "executed": False
        }

    # Execute Reconciliation
    # 1. Create verified safety backup
    backup_meta = create_safe_backup("pre_quarantine_reconciliation")
    now = now_kolkata()
    today_str = today_kolkata_str()
    created_reports = []

    for item in plan:
        # Fetch device record to get inventory date if recorded
        dev_rec = db.query(Device).filter(Device.serial == item["serial"]).first()
        rec_date = today_str
        if dev_rec and dev_rec.inventory_date and len(dev_rec.inventory_date) >= 10:
            rec_date = dev_rec.inventory_date[:10]

        report = DamageReport(
            report_time=dev_rec.created_at if dev_rec and dev_rec.created_at else now,
            report_date=rec_date,
            serial=item["serial"],
            employee_id=None,
            employee_name="",
            department=item["department"],
            fault_type=item["fault_type"],
            details=item["notes"],
            source="System Reconciliation",
            reported_by_id=None,
            reported_by_name="System Reconciliation",
            review_status=item["ticket_status"]
        )
        db.add(report)
        created_reports.append(report)

    # Audit log
    audit = AuditLog(
        timestamp=now,
        user_id=None,
        username=triggered_by or "System Admin",
        action="QUARANTINE_RECONCILIATION",
        target_type="QUARANTINE",
        target_id=f"TICKETS_{len(created_reports)}",
        details=(
            f"Created {len(created_reports)} quarantine tickets (Damaged: {len(damaged_to_fix)}, "
            f"Under Repair: {len(repair_to_fix)}). Pre-reconciliation backup: {backup_meta['filename']}."
        )
    )
    db.add(audit)
    db.commit()

    # Re-verify post-reconciliation state
    post_check = check_quarantine_discrepancies(db)

    return {
        "dry_run": False,
        "damaged_missing_count": len(damaged_to_fix),
        "repair_missing_count": len(repair_to_fix),
        "existing_open_tickets": existing_open_cnt,
        "new_tickets_to_create": len(plan),
        "created_tickets_count": len(created_reports),
        "duplicates_avoided": duplicates_avoided,
        "records_requiring_manual_review": manual_review,
        "planned_tickets": plan,
        "affected_serials": [p["serial"] for p in plan],
        "backup_filename": backup_meta["filename"],
        "executed": True,
        "remaining_discrepancies": post_check["counts"],
        "has_remaining_issues": post_check["has_issues"]
    }

