import csv
import io
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException, status, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, case, desc
import openpyxl

from app.database import get_db
from app.models import (
    User, Device, Employee, Transaction, DamageReport,
    DeviceAssignment, AuditLog, MissingEvent, ServiceRecord, SystemSetting
)
from app.auth import (
    require_admin, require_inventory_manager, hash_password, verify_password, login_limiter,
    ROLE_ADMIN, ROLE_INVENTORY_MANAGER, ROLE_OPERATOR, VALID_ROLES
)
from app.audit import record_audit
from app.backup import create_safe_backup
from app.quarantine import check_quarantine_discrepancies, reconcile_quarantine
from app.config import (
    DAMAGE_CATEGORIES, DEVICE_STATUSES, DEFAULT_DAILY_CUTOFF_TIME,
    now_kolkata, today_kolkata_str
)
from app.timefmt import fmt_datetime, fmt_time

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(prefix="/admin")

def get_configured_cutoff_time(db: Session) -> str:
    setting = db.query(SystemSetting).filter(SystemSetting.key == "daily_cutoff_time").first()
    return setting.value if setting else DEFAULT_DAILY_CUTOFF_TIME

# -------------------------------------------------------------
# 1. ADMIN DASHBOARD - 24/7 OPERATIONAL CENTER
# -------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    operator_filter: Optional[str] = "",
    dept_filter: Optional[str] = "",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    today_str = today_kolkata_str()

    # Core Device KPIs
    total_devices = db.query(Device).count()
    available_devices = db.query(Device).filter(Device.status == "Available").count()
    issued_devices = db.query(Device).filter(Device.status == "Issued").count()
    damaged_devices = db.query(Device).filter(Device.status == "Damaged").count()
    missing_devices = db.query(Device).filter(Device.status == "Missing").count()
    repair_devices = db.query(Device).filter(Device.status.in_(["Under Repair", "Sent to Service Centre"])).count()
    unavailable_devices = db.query(Device).filter(Device.status == "Unavailable").count()

    # Core Employee & Operator KPIs
    total_employees = db.query(Employee).count()
    active_employees = db.query(Employee).filter(Employee.active == True).count()
    total_operators = db.query(User).filter(User.role == "operator").count()
    total_inventory_managers = db.query(User).filter(User.role == "inventory_manager").count()

    # Today's Activity (Calendar date)
    today_issues = db.query(Transaction).filter(
        Transaction.transaction_date == today_str,
        Transaction.transaction_type == "Issue"
    ).count()
    today_returns = db.query(Transaction).filter(
        Transaction.transaction_date == today_str,
        Transaction.transaction_type == "Return"
    ).count()

    # Outstanding & Open Damage
    outstanding_count = db.query(DeviceAssignment).filter(DeviceAssignment.status == "ACTIVE").count()
    open_damage_count = db.query(DamageReport).filter(DamageReport.review_status == "Open").count()
    open_missing_count = db.query(MissingEvent).filter(MissingEvent.status == "CONFIRMED_MISSING").count()

    # Filtered Transactions Query for Activity Table
    tx_query = db.query(Transaction)
    if start_date:
        tx_query = tx_query.filter(Transaction.transaction_date >= start_date)
    if end_date:
        tx_query = tx_query.filter(Transaction.transaction_date <= end_date)
    if operator_filter and operator_filter != "All":
        tx_query = tx_query.filter(Transaction.operator_name == operator_filter)
    if dept_filter and dept_filter != "All":
        tx_query = tx_query.filter(Transaction.department == dept_filter)

    recent_txs = tx_query.order_by(Transaction.id.desc()).limit(15).all()

    # Open damage reports
    open_damage = db.query(DamageReport).filter(DamageReport.review_status == "Open").order_by(DamageReport.id.desc()).limit(5).all()

    # Filter Dropdown Options
    all_operators = [u[0] for u in db.query(User.full_name).filter(User.full_name != "").distinct().all() if u[0]]
    all_depts = [e[0] for e in db.query(Employee.department).distinct().all() if e[0]]

    # Detect legacy default accounts for security advisory
    legacy_default_usernames = {"hhdadmin", "operator1", "operator2"}
    legacy_accounts = [u.username for u in db.query(User).filter(User.username.in_(legacy_default_usernames)).all()]

    return templates.TemplateResponse(request=request, name="admin/dashboard.html", context={
        "title": "Admin Dashboard - HHD Management",
        "user": admin,
        "metrics": {
            "total_devices": total_devices,
            "available_devices": available_devices,
            "issued_devices": issued_devices,
            "damaged_devices": damaged_devices,
            "missing_devices": missing_devices,
            "repair_devices": repair_devices,
            "unavailable_devices": unavailable_devices,
            "total_employees": total_employees,
            "active_employees": active_employees,
            "total_operators": total_operators,
            "total_inventory_managers": total_inventory_managers,
            "today_issues": today_issues,
            "today_returns": today_returns,
            "outstanding_count": outstanding_count,
            "open_damage_count": open_damage_count,
            "open_missing_count": open_missing_count
        },
        "today_str": today_str,
        "recent_txs": recent_txs,
        "open_damage": open_damage,
        "operators": all_operators,
        "departments": all_depts,
        "start_date": start_date,
        "end_date": end_date,
        "operator_filter": operator_filter,
        "dept_filter": dept_filter,
        "legacy_accounts": legacy_accounts
    })

# -------------------------------------------------------------
# 2. DEVICE INVENTORY MANAGEMENT
# -------------------------------------------------------------
@router.get("/devices", response_class=HTMLResponse)
async def admin_devices(
    request: Request,
    q: Optional[str] = "",
    status_filter: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    query = db.query(Device)
    if q:
        q_clean = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Device.serial.ilike(q_clean),
                Device.make.ilike(q_clean),
                Device.model.ilike(q_clean),
                Device.notes.ilike(q_clean)
            )
        )
    if status_filter and status_filter != "All":
        query = query.filter(Device.status == status_filter)

    devices = query.order_by(Device.updated_at.desc()).limit(2000).all()

    # Pre-fetch employees to display assigned worker and department accurately
    emp_map = {e.employee_id: e for e in db.query(Employee).all()}

    dev_rows = []
    for d in devices:
        emp = emp_map.get(d.assigned_employee_id) if d.assigned_employee_id else None
        dev_rows.append({
            "serial": d.serial,
            "make": d.make,
            "model": d.model,
            "cover_qty": d.cover_qty,
            "status": d.status,
            "condition": d.condition,
            "unavailable_reason": d.unavailable_reason,
            "service_centre_name": d.service_centre_name,
            "service_reference": d.service_reference,
            "assigned_eid": emp.employee_id if emp else "",
            "assigned_ename": emp.name if emp else "",
            "assigned_dept": emp.department if emp else "",
            "inventory_date": d.inventory_date or "",
            "notes": d.notes or "",
            "updated_at": fmt_datetime(d.updated_at)
        })

    all_statuses = DEVICE_STATUSES

    return templates.TemplateResponse(request=request, name="admin/devices.html", context={
        "title": "All Devices - HHD Management",
        "user": user,
        "devices": dev_rows,
        "q": q,
        "status_filter": status_filter,
        "statuses": all_statuses,
        "damage_categories": DAMAGE_CATEGORIES,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.post("/devices")
async def admin_save_device(
    request: Request,
    serial: str = Form(...),
    make: str = Form("CHAINWAY"),
    model: str = Form("HHD"),
    department: str = Form(""),
    cover_qty: int = Form(0),
    status: str = Form("Unavailable"),  # Safe default for newly added devices
    condition: str = Form("Working"),
    unavailable_reason: str = Form("Pending Inspection"),
    inventory_date: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    serial_clean = serial.strip()
    if not serial_clean:
        return RedirectResponse("/admin/devices?error=Serial+number+is+required", status_code=302)

    dev = db.query(Device).filter(Device.serial == serial_clean).first()
    now = now_kolkata()

    if dev:
        # Inventory Manager/Admin updating permitted inventory fields
        prev_status = dev.status
        dev.make = make.strip()
        dev.model = model.strip()
        if inventory_date:
            dev.inventory_date = inventory_date.strip()
        dev.cover_qty = cover_qty
        
        # State transition validation: An Issued device cannot be made Available directly without return!
        if dev.status == "Issued" and status != "Issued":
            return RedirectResponse(
                f"/admin/devices?error=Device+{serial_clean}+is+currently+Issued+to+a+worker.+It+must+complete+a+valid+return+workflow+before+changing+status.",
                status_code=302
            )
        
        # Damaged or Under Repair cannot become Available without repair/inspection approval
        if dev.status in ("Damaged", "Under Repair", "Sent to Service Centre") and status == "Available":
            if condition != "Working":
                return RedirectResponse(
                    f"/admin/devices?error=Cannot+make+device+Available+while+condition+is+{condition}.+Inspection+and+repair+must+be+approved.",
                    status_code=302
                )

        dev.status = status
        dev.condition = condition
        if status == "Unavailable":
            dev.unavailable_reason = unavailable_reason.strip() or "Pending Inspection"
        elif status == "Available":
            dev.unavailable_reason = None

        dev.notes = notes.strip() if notes else ""
        dev.updated_at = now

        record_audit(
            db=db, user=user, action="DEVICE_UPDATED", target_type="DEVICE", target_id=serial_clean,
            details=f"Device {serial_clean} updated. Status: {prev_status} -> {status}, Condition: {condition}",
            previous_value=prev_status, new_value=status, status="SUCCESS"
        )
    else:
        # Create new device (Default status: Unavailable / Pending Inspection)
        initial_status = status if status in ("Available", "Unavailable") else "Unavailable"
        dev = Device(
            serial=serial_clean,
            make=make.strip(),
            model=model.strip(),
            department=None,  # Devices do not have departments
            inventory_date=inventory_date.strip() if inventory_date else date.today().isoformat(),
            cover_qty=cover_qty,
            status=initial_status,
            unavailable_reason=unavailable_reason.strip() if initial_status == "Unavailable" else None,
            condition=condition,
            notes=notes.strip() if notes else "Added via Inventory Management",
            created_at=now,
            updated_at=now
        )
        db.add(dev)
        record_audit(
            db=db, user=user, action="DEVICE_CREATED", target_type="DEVICE", target_id=serial_clean,
            details=f"Device {serial_clean} added to inventory. Status: {initial_status}, Model: {make.strip()} {model.strip()}",
            new_value=initial_status, status="SUCCESS"
        )

    # Maintain atomic quarantine ticket consistency on device status change
    if status == "Damaged":
        existing_dmg = db.query(DamageReport).filter(
            DamageReport.serial == serial_clean,
            DamageReport.review_status.in_(["Open", "Pending Inspection", "Under Repair"])
        ).first()
        if not existing_dmg:
            dmg = DamageReport(
                report_time=now,
                report_date=today_kolkata_str(),
                serial=serial_clean,
                employee_id=None,
                employee_name="",
                department="",
                fault_type=condition if condition and condition != "Working" else "Not Working",
                details=dev.notes or f"Marked as Damaged by {user.username}",
                source="Manual Entry",
                reported_by_id=user.id,
                reported_by_name=user.full_name or user.username,
                review_status="Pending Inspection"
            )
            db.add(dmg)
    elif status == "Under Repair":
        existing_dmg = db.query(DamageReport).filter(
            DamageReport.serial == serial_clean,
            DamageReport.review_status.in_(["Open", "Pending Inspection", "Under Repair"])
        ).first()
        if not existing_dmg:
            dmg = DamageReport(
                report_time=now,
                report_date=today_kolkata_str(),
                serial=serial_clean,
                employee_id=None,
                employee_name="",
                department="",
                fault_type="Under Repair",
                details=dev.notes or f"Marked as Under Repair by {user.username}",
                source="Manual Entry",
                reported_by_id=user.id,
                reported_by_name=user.full_name or user.username,
                review_status="Under Repair"
            )
            db.add(dmg)
    elif status == "Available":
        # Resolve any open damage tickets
        open_dmgs = db.query(DamageReport).filter(
            DamageReport.serial == serial_clean,
            DamageReport.review_status.in_(["Open", "Pending Inspection", "Under Repair"])
        ).all()
        for d in open_dmgs:
            d.review_status = "Repaired/Available"
            d.resolution_notes = f"Returned to Available by {user.username}"
            d.resolved_by = user.full_name or user.username
            d.resolved_at = now

    db.commit()
    return RedirectResponse(f"/admin/devices?msg=Device+{serial_clean}+saved+successfully", status_code=302)

# Safe Device Deletion (Admin Only, Historical Integrity Guarded)
@router.post("/device/delete/{serial}")
async def admin_delete_device(
    serial: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    dev = db.query(Device).filter(Device.serial == serial).first()
    if not dev:
        return RedirectResponse("/admin/devices?error=Device+not+found", status_code=302)

    if dev.status == "Issued":
        return RedirectResponse(f"/admin/devices?error=Cannot+delete+currently+Issued+device+{serial}.+Return+it+first.", status_code=302)

    # Check for historical transactions, assignments, damage reports, or missing events
    has_tx = db.query(Transaction).filter(Transaction.serial == serial).count() > 0
    has_dmg = db.query(DamageReport).filter(DamageReport.serial == serial).count() > 0
    has_asgn = db.query(DeviceAssignment).filter(DeviceAssignment.device_serial == serial).count() > 0
    has_missing = db.query(MissingEvent).filter(MissingEvent.serial == serial).count() > 0

    if has_tx or has_dmg or has_asgn or has_missing:
        # Historical Integrity Protection: Archive rather than delete to preserve audit history
        dev.status = "Archived"
        dev.condition = "Decommissioned"
        dev.unavailable_reason = f"Archived by Admin {admin.username} (Historical audit trails preserved)"
        dev.assigned_employee_id = None
        dev.updated_at = now_kolkata()
        record_audit(
            db=db, user=admin, action="DEVICE_ARCHIVED", target_type="DEVICE", target_id=serial,
            details=f"Device {serial} archived (Deletion restricted: preserved historical transactions)",
            status="SUCCESS"
        )
        db.commit()
        return RedirectResponse(f"/admin/devices?msg=Device+{serial}+has+historical+records+and+was+safely+archived+(audit+integrity+preserved)", status_code=302)
    else:
        db.delete(dev)
        record_audit(
            db=db, user=admin, action="DEVICE_DELETED", target_type="DEVICE", target_id=serial,
            details=f"Device {serial} deleted permanently (Zero historical records existed)",
            status="SUCCESS"
        )
        db.commit()
        return RedirectResponse(f"/admin/devices?msg=Device+{serial}+deleted+successfully", status_code=302)

# -------------------------------------------------------------
# 3. EMPLOYEE MASTER MANAGEMENT
# -------------------------------------------------------------
@router.get("/employees", response_class=HTMLResponse)
async def admin_employees(
    request: Request,
    q: Optional[str] = "",
    status_filter: Optional[str] = "",
    dept_filter: Optional[str] = "",
    add_prefill: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    query = db.query(Employee)
    if q:
        q_clean = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Employee.employee_id.ilike(q_clean),
                Employee.name.ilike(q_clean),
                Employee.department.ilike(q_clean)
            )
        )
    if status_filter == "Active":
        query = query.filter(Employee.active == True)
    elif status_filter == "Inactive":
        query = query.filter(Employee.active == False)
    if dept_filter and dept_filter != "All":
        query = query.filter(Employee.department == dept_filter)

    employees = query.order_by(Employee.name.asc()).limit(2000).all()

    emp_rows = []
    for e in employees:
        held = db.query(Device).filter(Device.assigned_employee_id == e.employee_id, Device.status == "Issued").all()
        emp_rows.append({
            "employee_id": e.employee_id,
            "name": e.name,
            "department": e.department,
            "active": e.active,
            "held_count": len(held),
            "held_serials": ", ".join(d.serial for d in held),
            "updated_at": fmt_datetime(e.updated_at)
        })

    all_depts = [d[0] for d in db.query(Employee.department).distinct().all() if d[0]]

    return templates.TemplateResponse(request=request, name="admin/employees.html", context={
        "title": "Employee Master - HHD Management",
        "user": user,
        "employees": emp_rows,
        "q": q,
        "status_filter": status_filter,
        "dept_filter": dept_filter,
        "departments": all_depts,
        "prefill": add_prefill,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.post("/employees")
async def admin_save_employee(
    request: Request,
    employee_id: str = Form(...),
    name: str = Form(...),
    department: str = Form(...),
    active: bool = Form(True),
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    eid = employee_id.strip()
    ename = name.strip()
    dept = department.strip()
    if not eid or not ename or not dept:
        return RedirectResponse("/admin/employees?error=Employee+ID,+Name,+and+Department+are+all+required", status_code=302)

    emp = db.query(Employee).filter(Employee.employee_id == eid).first()
    now = now_kolkata()

    if emp:
        emp.name = ename
        emp.department = dept
        emp.active = active
        emp.updated_at = now
        record_audit(db=db, user=user, action="EMPLOYEE_UPDATED", target_type="EMPLOYEE", target_id=eid, details=f"Updated employee {ename} ({dept})", status="SUCCESS")
    else:
        emp = Employee(
            employee_id=eid,
            name=ename,
            department=dept,
            active=active,
            created_at=now,
            updated_at=now
        )
        db.add(emp)
        record_audit(db=db, user=user, action="EMPLOYEE_CREATED", target_type="EMPLOYEE", target_id=eid, details=f"Created employee {ename} ({dept})", status="SUCCESS")

    db.commit()
    return RedirectResponse(f"/admin/employees?msg=Employee+{eid}+saved+successfully", status_code=302)

# -------------------------------------------------------------
# 4. DAMAGE, REPAIR & SERVICE CENTRE WORKFLOW
# -------------------------------------------------------------
@router.get("/damage", response_class=HTMLResponse)
async def admin_damage(
    request: Request,
    status_filter: Optional[str] = "",
    fault_filter: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    # Check for quarantine discrepancies between inventory and damage tickets
    discrepancies = check_quarantine_discrepancies(db)

    query = db.query(DamageReport)
    if status_filter and status_filter != "All":
        query = query.filter(DamageReport.review_status == status_filter)
    if fault_filter and fault_filter != "All":
        query = query.filter(DamageReport.fault_type == fault_filter)

    reports = query.order_by(DamageReport.id.desc()).limit(1000).all()

    all_statuses = ["Open", "Pending Inspection", "Under Repair", "Sent to Service Centre", "Repaired/Available", "Written Off", "Closed"]
    all_categories = DAMAGE_CATEGORIES

    return templates.TemplateResponse(request=request, name="admin/damage.html", context={
        "title": "Damaged Devices - HHD Management",
        "user": user,
        "reports": reports,
        "reconciliation": discrepancies,
        "discrepancies": discrepancies,
        "status_filter": status_filter,
        "fault_filter": fault_filter,
        "statuses": all_statuses,
        "categories": all_categories,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.post("/damage/{report_id}/update")
async def update_damage_status(
    report_id: int,
    review_status: str = Form(...),
    resolution_notes: str = Form(""),
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    report = db.query(DamageReport).filter(DamageReport.id == report_id).first()
    if not report:
        return RedirectResponse("/admin/damage?error=Report+not+found", status_code=302)

    now = now_kolkata()
    report.review_status = review_status
    report.resolution_notes = resolution_notes.strip()
    if review_status in ("Repaired/Available", "Closed"):
        report.resolved_by = user.full_name or user.username
        report.resolved_at = now

        other_open = db.query(DamageReport).filter(
            DamageReport.serial == report.serial,
            DamageReport.id != report.id,
            DamageReport.review_status.in_(["Open", "Pending Inspection", "Under Repair"])
        ).first()

        if not other_open:
            dev = db.query(Device).filter(Device.serial == report.serial).first()
            if dev and dev.status in ("Damaged", "Under Repair"):
                dev.status = "Available"
                dev.condition = "Working"
                dev.unavailable_reason = None
                dev.updated_at = now
    elif review_status == "Under Repair":
        dev = db.query(Device).filter(Device.serial == report.serial).first()
        if dev and dev.status != "Issued":
            dev.status = "Under Repair"
            dev.condition = "Under Repair"
            dev.updated_at = now

    record_audit(
        db=db, user=user, action="DAMAGE_REPORT_UPDATED", target_type="DAMAGE_REPORT", target_id=str(report_id),
        details=f"Damage report #{report_id} ({report.serial}) updated to {review_status}",
        new_value=review_status, status="SUCCESS"
    )
    db.commit()
    return RedirectResponse("/admin/damage?msg=Damage+report+updated+successfully", status_code=302)

@router.get("/damage/reconcile", response_class=HTMLResponse)
async def reconcile_preview_page(
    request: Request,
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    preview = reconcile_quarantine(db, dry_run=True)
    return templates.TemplateResponse(request=request, name="admin/damage_reconcile.html", context={
        "title": "Quarantine Reconciliation Preview",
        "user": user,
        "summary": preview
    })

@router.post("/damage/reconcile")
async def execute_reconcile_quarantine(
    request: Request,
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    reconcile_quarantine(db, dry_run=False, triggered_by=user.username)
    return RedirectResponse("/admin/damage?msg=Quarantine+reconciled+successfully", status_code=302)

# Service Centre Workflow Actions
@router.post("/service/send")
async def send_to_service_centre(
    serial: str = Form(...),
    service_centre_name: str = Form(...),
    external_reference: str = Form(""),
    expected_return_date: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    dev = db.query(Device).filter(Device.serial == serial).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found.")

    if dev.status == "Issued":
        return RedirectResponse("/admin/devices?error=Cannot+send+an+Issued+device+to+service+centre.+Return+it+first.", status_code=302)

    now = now_kolkata()
    today_str = today_kolkata_str()

    record = ServiceRecord(
        serial=dev.serial,
        service_centre_name=service_centre_name.strip(),
        sent_date=today_str,
        external_reference=external_reference.strip(),
        expected_return_date=expected_return_date.strip(),
        sent_by_id=user.id,
        sent_by_name=user.full_name or user.username,
        status="SENT"
    )
    db.add(record)

    prev_status = dev.status
    dev.status = "Sent to Service Centre"
    dev.condition = "Under Repair"
    dev.service_centre_name = service_centre_name.strip()
    dev.service_reference = external_reference.strip()
    dev.service_sent_date = today_str
    dev.service_expected_return = expected_return_date.strip()
    dev.unavailable_reason = f"Sent to {service_centre_name.strip()} on {today_str}"
    dev.notes = (dev.notes + f" | Sent to {service_centre_name.strip()}: {notes}").strip(" |")
    dev.updated_at = now

    record_audit(
        db=db, user=user, action="SENT_TO_SERVICE_CENTRE", target_type="DEVICE", target_id=serial,
        details=f"Device {serial} sent to {service_centre_name.strip()} (Ref: {external_reference}). Operator: {user.username}",
        previous_value=prev_status, new_value="Sent to Service Centre", status="SUCCESS"
    )
    db.commit()

    return RedirectResponse(f"/admin/devices?msg=Device+{serial}+marked+as+Sent+to+Service+Centre", status_code=302)

@router.post("/service/return")
async def return_from_service_centre(
    serial: str = Form(...),
    service_result: str = Form(...),
    inspection_notes: str = Form(""),
    approved_status: str = Form("Unavailable"),  # Default: Unavailable (Pending Inspection)
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    dev = db.query(Device).filter(Device.serial == serial).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found.")

    now = now_kolkata()
    today_str = today_kolkata_str()

    service_rec = db.query(ServiceRecord).filter(
        ServiceRecord.serial == serial,
        ServiceRecord.status == "SENT"
    ).order_by(ServiceRecord.id.desc()).first()

    if service_rec:
        service_rec.actual_return_date = today_str
        service_rec.service_result = service_result
        service_rec.inspection_notes = inspection_notes
        service_rec.inspected_by_id = user.id
        service_rec.inspected_by_name = user.full_name or user.username
        service_rec.status = "RETURNED"

    prev_status = dev.status
    dev.status = approved_status
    dev.condition = "Working" if approved_status == "Available" else ("Under Repair" if approved_status == "Under Repair" else "Damaged")
    if approved_status == "Unavailable":
        dev.unavailable_reason = "Returned from Service Centre - Pending Final Approval"
    elif approved_status == "Available":
        dev.unavailable_reason = None
    dev.updated_at = now

    record_audit(
        db=db, user=user, action="SERVICE_CENTRE_RETURN", target_type="DEVICE", target_id=serial,
        details=f"Returned from service centre: {service_result}. Inspected status: {approved_status} by {user.username}",
        previous_value=prev_status, new_value=approved_status, status="SUCCESS"
    )
    db.commit()

    return RedirectResponse(f"/admin/devices?msg=Device+{serial}+service+centre+return+and+inspection+recorded", status_code=302)

# -------------------------------------------------------------
# 5. OUTSTANDING DEVICES & DAILY CUTOFF REPORTING
# -------------------------------------------------------------
@router.get("/outstanding", response_class=HTMLResponse)
async def admin_outstanding(
    request: Request,
    date_filter: Optional[str] = "",
    dept_filter: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    cutoff_time = get_configured_cutoff_time(db)
    now = now_kolkata()
    today_str = today_kolkata_str()
    cutoff_hour, cutoff_min = map(int, cutoff_time.split(":"))

    query = db.query(DeviceAssignment).filter(DeviceAssignment.status == "ACTIVE")
    if date_filter:
        query = query.filter(DeviceAssignment.issued_date == date_filter)

    active_assignments = query.order_by(DeviceAssignment.issued_at.asc()).all()

    emp_map = {e.employee_id: e for e in db.query(Employee).all()}
    dev_map = {d.serial: d for d in db.query(Device).all()}

    outstanding = []
    for a in active_assignments:
        emp = emp_map.get(a.employee_id)
        if dept_filter and dept_filter != "All":
            if not emp or emp.department != dept_filter:
                continue

        dev = dev_map.get(a.device_serial)
        issued_dt = a.issued_at

        # Calculate cutoff for the issue date
        cutoff_dt = issued_dt.replace(hour=cutoff_hour, minute=cutoff_min, second=0, microsecond=0)
        is_past_cutoff = now > cutoff_dt

        hours_held = round((now - issued_dt).total_seconds() / 3600, 1)

        outstanding.append({
            "serial": a.device_serial,
            "make": dev.make if dev else "",
            "model": dev.model if dev else "",
            "employee_id": a.employee_id,
            "employee_name": emp.name if emp else a.employee_id,
            "department": emp.department if emp else "Warehouse",
            "issued_at": a.issued_at,
            "issued_at_fmt": fmt_datetime(a.issued_at),
            "issued_date": a.issued_date,
            "issued_by": a.issued_by_operator_name,
            "operator_name": a.issued_by_operator_name,
            "hours_held": hours_held,
            "duration_hours": hours_held,
            "is_past_cutoff": is_past_cutoff,
            "status": dev.status if dev else "Issued"
        })

    all_depts = [d[0] for d in db.query(Employee.department).distinct().all() if d[0]]

    return templates.TemplateResponse(request=request, name="admin/outstanding.html", context={
        "title": "Outstanding Devices - HHD Management",
        "user": user,
        "outstanding": outstanding,
        "total_count": len(outstanding),
        "departments": all_depts,
        "dept_filter": dept_filter,
        "date_filter": date_filter,
        "cutoff_time": cutoff_time,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

# -------------------------------------------------------------
# 6. DEDICATED MISSING DEVICE TRAIL MODULE
# -------------------------------------------------------------
@router.get("/missing", response_class=HTMLResponse)
async def admin_missing_trail(
    request: Request,
    view: Optional[str] = "all",
    q: Optional[str] = "",
    dept_filter: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    devices = db.query(Device).all()
    emp_map = {e.employee_id: e for e in db.query(Employee).all()}
    
    # Pre-fetch all missing events grouped by serial
    all_missing_events = db.query(MissingEvent).order_by(MissingEvent.id.desc()).all()
    missing_by_serial: Dict[str, List[MissingEvent]] = {}
    for m in all_missing_events:
        missing_by_serial.setdefault(m.serial, []).append(m)

    counts = {
        "total": len(devices),
        "never_missing": 0,
        "outstanding": 0,
        "confirmed_missing": 0,
        "recovered": 0,
        "missing_multiple": 0
    }

    records = []
    for d in devices:
        events = missing_by_serial.get(d.serial, [])
        m_count = len(events)
        last_event = events[0] if events else None

        # Determine category
        is_currently_missing = d.status == "Missing" or (last_event and last_event.status == "CONFIRMED_MISSING")
        has_recovered = any(e.status == "RECOVERED" for e in events)

        if is_currently_missing:
            cat = "Confirmed Missing"
            counts["confirmed_missing"] += 1
        elif d.status == "Issued":
            cat = "Currently Outstanding"
            counts["outstanding"] += 1
        elif has_recovered:
            cat = "Recovered"
            counts["recovered"] += 1
        else:
            cat = "Never Missing"
            counts["never_missing"] += 1

        if m_count > 1:
            counts["missing_multiple"] += 1

        # Check view filtering
        match_view = True
        if view == "never_missing" and cat != "Never Missing":
            match_view = False
        elif view == "outstanding" and cat != "Currently Outstanding":
            match_view = False
        elif view == "confirmed_missing" and cat != "Confirmed Missing":
            match_view = False
        elif view == "recovered" and cat != "Recovered":
            match_view = False
        elif view == "missing_multiple" and m_count <= 1:
            match_view = False

        if not match_view:
            continue

        emp = emp_map.get(d.assigned_employee_id) if d.assigned_employee_id else None
        
        # Check free-text search
        if q:
            q_clean = q.strip().lower()
            text_haystack = f"{d.serial} {d.make} {d.model} {cat} {emp.name if emp else ''} {emp.employee_id if emp else ''}".lower()
            if q_clean not in text_haystack:
                continue

        # Check department filter
        if dept_filter and dept_filter != "All":
            emp_dept = emp.department if emp else (last_event.department if last_event else "")
            if emp_dept != dept_filter:
                continue

        records.append({
            "serial": d.serial,
            "make": d.make,
            "model": d.model,
            "status": d.status,
            "category": cat,
            "missing_count": m_count,
            "last_missing_date": fmt_datetime(last_event.confirmed_at) if last_event else None,
            "confirmed_by_admin": last_event.confirmed_by_name if last_event else None,
            "last_recovery_date": fmt_datetime(last_event.recovered_at) if (last_event and last_event.recovered_at) else None,
            "assigned_employee": f"{emp.name} ({emp.employee_id})" if emp else ""
        })

    view_labels = {
        "all": "All Devices",
        "never_missing": "Never Missing",
        "outstanding": "Currently Outstanding",
        "confirmed_missing": "Confirmed Missing",
        "recovered": "Recovered",
        "missing_multiple": "Missing More Than Once"
    }

    all_depts = [e[0] for e in db.query(Employee.department).distinct().all() if e[0]]

    return templates.TemplateResponse(request=request, name="admin/missing.html", context={
        "title": "Missing Device Trail - HHD Management",
        "user": user,
        "counts": counts,
        "records": records,
        "current_view": view,
        "current_view_label": view_labels.get(view, "All Devices"),
        "q": q,
        "dept_filter": dept_filter,
        "departments": all_depts,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

# Confirm Missing (Admin ONLY)
def _execute_confirm_device_missing(db: Session, admin: User, serial: str, reason: str, notes: str):
    dev = db.query(Device).filter(Device.serial == serial).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found.")

    now = now_kolkata()
    today_str = today_kolkata_str()

    asgn = db.query(DeviceAssignment).filter(
        DeviceAssignment.device_serial == serial,
        DeviceAssignment.status == "ACTIVE"
    ).first()

    emp_id = dev.assigned_employee_id or (asgn.employee_id if asgn else None)
    emp = db.query(Employee).filter(Employee.employee_id == emp_id).first() if emp_id else None
    emp_name = emp.name if emp else (asgn.employee_id if asgn else "Unknown")
    emp_dept = emp.department if emp else "Warehouse"

    missing_event = MissingEvent(
        serial=dev.serial,
        employee_id=emp_id,
        employee_name=emp_name,
        department=emp_dept,
        issued_at=asgn.issued_at if asgn else None,
        issued_by_operator_name=asgn.issued_by_operator_name if asgn else "",
        confirmed_at=now,
        confirmed_by_id=admin.id,
        confirmed_by_name=admin.full_name or admin.username,
        reason=reason.strip(),
        notes=notes.strip(),
        status="CONFIRMED_MISSING"
    )
    db.add(missing_event)

    prev_status = dev.status
    dev.status = "Missing"
    dev.condition = "Missing"
    dev.unavailable_reason = f"Confirmed Missing by {admin.username}: {reason.strip()}"
    dev.notes = (dev.notes + f" | Marked Missing by {admin.full_name or admin.username}: {notes.strip()}").strip(" |")
    dev.updated_at = now

    if asgn:
        asgn.status = "RETURNED"
        asgn.returned_at = now
        asgn.returned_date = today_str
        asgn.returned_by_operator_id = admin.id
        asgn.returned_by_operator_name = admin.full_name or admin.username
        asgn.condition_on_return = "Missing"
        asgn.remarks = f"Marked missing by admin: {notes.strip()}"

    record_audit(
        db=db, user=admin, action="MARK_DEVICE_MISSING", target_type="DEVICE", target_id=serial,
        details=f"Device {serial} confirmed Missing by {admin.username}. Reason: {reason.strip()}. Worker: {emp_name} ({emp_id})",
        previous_value=prev_status, new_value="Missing", status="SUCCESS", reason=reason.strip()
    )
    db.commit()

@router.post("/missing/confirm")
async def confirm_device_missing_form(
    serial: str = Form(...),
    reason: str = Form("Unreturned after reporting cutoff investigation"),
    notes: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    _execute_confirm_device_missing(db, admin, serial, reason, notes)
    return RedirectResponse("/admin/missing?msg=Device+officially+confirmed+Missing", status_code=302)

@router.post("/devices/{serial}/mark-missing")
async def mark_device_missing_path(
    serial: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    _execute_confirm_device_missing(db, admin, serial, "Marked missing via admin action", "")
    return RedirectResponse("/admin/missing?msg=Device+officially+confirmed+Missing", status_code=302)

# Record Recovery (Admin or Inventory Manager)
def _execute_device_recovery(db: Session, user: User, serial: str, found_by: str, recovery_condition: str, recovery_notes: str, post_recovery_status: str):
    dev = db.query(Device).filter(Device.serial == serial).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found.")

    now = now_kolkata()

    # Find open unrecovered missing event
    missing_event = db.query(MissingEvent).filter(
        MissingEvent.serial == serial,
        MissingEvent.status == "CONFIRMED_MISSING"
    ).order_by(MissingEvent.id.desc()).first()

    if missing_event:
        missing_event.recovered_at = now
        missing_event.recovered_by_id = user.id
        missing_event.recovered_by_name = user.full_name or user.username
        missing_event.found_by = found_by.strip() or "Warehouse"
        missing_event.recovery_condition = recovery_condition
        missing_event.recovery_notes = recovery_notes.strip()
        missing_event.post_recovery_status = post_recovery_status
        missing_event.status = "RECOVERED"

    prev_status = dev.status
    dev.status = post_recovery_status
    dev.condition = recovery_condition
    dev.assigned_employee_id = None
    if post_recovery_status == "Unavailable":
        dev.unavailable_reason = "Recovered - Pending Final Inspection"
    elif post_recovery_status == "Available":
        dev.unavailable_reason = None
    dev.updated_at = now

    record_audit(
        db=db, user=user, action="DEVICE_RECOVERED", target_type="DEVICE", target_id=serial,
        details=f"Device {serial} recovered by {user.username}. Found by: {found_by}. Condition: {recovery_condition}. Post-recovery status: {post_recovery_status}",
        previous_value=prev_status, new_value=post_recovery_status, status="SUCCESS"
    )
    db.commit()

@router.post("/missing/recover")
async def record_device_recovery_form(
    serial: str = Form(...),
    found_by: str = Form(""),
    recovery_condition: str = Form("Working"),
    recovery_notes: str = Form(""),
    post_recovery_status: str = Form("Unavailable"),  # Default: Unavailable (Pending Inspection)
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    _execute_device_recovery(db, user, serial, found_by, recovery_condition, recovery_notes, post_recovery_status)
    return RedirectResponse("/admin/missing?msg=Device+recovery+recorded+successfully", status_code=302)

@router.post("/devices/{serial}/recover")
async def record_device_recovery_path(
    serial: str,
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    _execute_device_recovery(db, user, serial, "Admin/Manager Recovery", "Working", "", "Unavailable")
    return RedirectResponse("/admin/missing?msg=Device+recovery+recorded+successfully", status_code=302)

# Export Missing Device Trail to Excel
@router.get("/missing/export")
async def export_missing_trail(
    view: Optional[str] = "all",
    q: Optional[str] = "",
    dept: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    devices = db.query(Device).all()
    emp_map = {e.employee_id: e for e in db.query(Employee).all()}
    all_missing_events = db.query(MissingEvent).order_by(MissingEvent.id.desc()).all()
    missing_by_serial: Dict[str, List[MissingEvent]] = {}
    for m in all_missing_events:
        missing_by_serial.setdefault(m.serial, []).append(m)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Missing Device Trail"

    headers = [
        "Device Serial", "Make", "Model", "Current Status", "Missing Category",
        "Total Times Missing", "Last Confirmed Missing", "Confirmed By (Admin)",
        "Investigation Reason", "Last Recovery Date", "Recovered By", "Recovery Condition"
    ]
    ws.append(headers)

    for d in devices:
        events = missing_by_serial.get(d.serial, [])
        m_count = len(events)
        last_event = events[0] if events else None

        is_currently_missing = d.status == "Missing" or (last_event and last_event.status == "CONFIRMED_MISSING")
        has_recovered = any(e.status == "RECOVERED" for e in events)

        if is_currently_missing:
            cat = "Confirmed Missing"
        elif d.status == "Issued":
            cat = "Currently Outstanding"
        elif has_recovered:
            cat = "Recovered"
        else:
            cat = "Never Missing"

        if view == "never_missing" and cat != "Never Missing":
            continue
        elif view == "outstanding" and cat != "Currently Outstanding":
            continue
        elif view == "confirmed_missing" and cat != "Confirmed Missing":
            continue
        elif view == "recovered" and cat != "Recovered":
            continue
        elif view == "missing_multiple" and m_count <= 1:
            continue

        ws.append([
            d.serial, d.make, d.model, d.status, cat, m_count,
            fmt_datetime(last_event.confirmed_at) if last_event else "",
            last_event.confirmed_by_name if last_event else "",
            last_event.reason if last_event else "",
            fmt_datetime(last_event.recovered_at) if (last_event and last_event.recovered_at) else "",
            last_event.recovered_by_name if (last_event and last_event.recovered_by_name) else "",
            last_event.recovery_condition if last_event else ""
        ])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"missing_device_trail_{today_kolkata_str()}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# -------------------------------------------------------------
# 7. TRANSACTION AUDIT LOG - CONTINUOUS 24/7 (REAL PAGINATION)
# -------------------------------------------------------------
@router.get("/transactions", response_class=HTMLResponse)
async def admin_transactions(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    q: Optional[str] = "",
    type_filter: Optional[str] = "",
    operator_filter: Optional[str] = "",
    dept_filter: Optional[str] = "",
    date_start: Optional[str] = "",
    date_end: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    query = db.query(Transaction)
    if q:
        q_clean = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Transaction.serial.ilike(q_clean),
                Transaction.employee_id.ilike(q_clean),
                Transaction.employee_name.ilike(q_clean),
                Transaction.department.ilike(q_clean),
                Transaction.operator_name.ilike(q_clean),
                Transaction.remarks.ilike(q_clean)
            )
        )
    if type_filter and type_filter != "All":
        query = query.filter(Transaction.transaction_type == type_filter)
    if operator_filter and operator_filter != "All":
        query = query.filter(Transaction.operator_name == operator_filter)
    if dept_filter and dept_filter != "All":
        query = query.filter(Transaction.department == dept_filter)
    if date_start:
        query = query.filter(Transaction.transaction_date >= date_start)
    if date_end:
        query = query.filter(Transaction.transaction_date <= date_end)

    total_records = query.count()
    total_pages = max(1, (total_records + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    offset = (current_page - 1) * page_size

    txs = query.order_by(Transaction.id.desc()).offset(offset).limit(page_size).all()

    all_operators = [u[0] for u in db.query(Transaction.operator_name).distinct().all() if u[0]]
    all_depts = [d[0] for d in db.query(Transaction.department).distinct().all() if d[0]]

    return templates.TemplateResponse(request=request, name="admin/transactions.html", context={
        "title": "Transaction Audit - Admin",
        "user": user,
        "transactions": txs,
        "page": current_page,
        "page_size": page_size,
        "total_records": total_records,
        "total_pages": total_pages,
        "has_prev": current_page > 1,
        "has_next": current_page < total_pages,
        "prev_page": current_page - 1,
        "next_page": current_page + 1,
        "q": q,
        "type_filter": type_filter,
        "operator_filter": operator_filter,
        "dept_filter": dept_filter,
        "date_start": date_start,
        "date_end": date_end,
        "operators": all_operators,
        "departments": all_depts
    })

# -------------------------------------------------------------
# 8. DEDICATED AUDIT & ACTIVITY HISTORY MODULE
# -------------------------------------------------------------
@router.get("/audit", response_class=HTMLResponse)
async def admin_audit_trail(
    request: Request,
    time_range: Optional[str] = "30d",
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    user_filter: Optional[str] = "",
    role_filter: Optional[str] = "",
    action_filter: Optional[str] = "",
    status_filter: Optional[str] = "",
    q: Optional[str] = "",
    page: int = 1,
    limit: int = 50,
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    query = db.query(AuditLog)
    now = now_kolkata()

    # Apply date filters (Default: 30 days)
    if time_range == "today":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(AuditLog.timestamp >= today_start)
    elif time_range == "7d":
        seven_days_ago = now - timedelta(days=7)
        query = query.filter(AuditLog.timestamp >= seven_days_ago)
    elif time_range == "30d":
        thirty_days_ago = now - timedelta(days=30)
        query = query.filter(AuditLog.timestamp >= thirty_days_ago)
    elif time_range == "custom" or (start_date or end_date):
        if start_date:
            try:
                s_dt = datetime.strptime(start_date, "%Y-%m-%d")
                query = query.filter(AuditLog.timestamp >= s_dt)
            except ValueError:
                pass
        if end_date:
            try:
                e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                query = query.filter(AuditLog.timestamp <= e_dt)
            except ValueError:
                pass

    if user_filter:
        query = query.filter(AuditLog.username == user_filter)
    if role_filter:
        query = query.filter(AuditLog.role == role_filter)
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)
    if status_filter:
        query = query.filter(AuditLog.status == status_filter)

    if q:
        q_clean = f"%{q.strip()}%"
        query = query.filter(
            or_(
                AuditLog.target_id.ilike(q_clean),
                AuditLog.username.ilike(q_clean),
                AuditLog.action.ilike(q_clean),
                AuditLog.details.ilike(q_clean)
            )
        )

    total_count = query.count()
    total_pages = max(1, (total_count + limit - 1) // limit)
    current_page = max(1, min(page, total_pages))

    logs_raw = query.order_by(AuditLog.id.desc()).offset((current_page - 1) * limit).limit(limit).all()

    logs_view = []
    for l in logs_raw:
        logs_view.append({
            "id": l.id,
            "time_fmt": fmt_datetime(l.timestamp),
            "username": l.username,
            "role": l.role,
            "action": l.action,
            "target_type": l.target_type,
            "target_id": l.target_id,
            "status": l.status,
            "details": l.details,
            "reason": l.reason
        })

    all_actions = [a[0] for a in db.query(AuditLog.action).distinct().all() if a[0]]

    return templates.TemplateResponse(request=request, name="admin/audit.html", context={
        "title": "Audit & Activity History - HHD Management",
        "user": user,
        "logs": logs_view,
        "total_count": total_count,
        "page": current_page,
        "total_pages": total_pages,
        "current_range": time_range,
        "q": q,
        "start_date": start_date,
        "end_date": end_date,
        "user_filter": user_filter,
        "role_filter": role_filter,
        "action_filter": action_filter,
        "status_filter": status_filter,
        "all_actions": all_actions
    })

# Export Audit Logs to Excel or CSV
@router.get("/audit/export")
async def export_audit_trail(
    format: str = "xlsx",
    time_range: Optional[str] = "30d",
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    action_filter: Optional[str] = "",
    status_filter: Optional[str] = "",
    q: Optional[str] = "",
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    query = db.query(AuditLog)
    now = now_kolkata()

    if time_range == "today":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(AuditLog.timestamp >= today_start)
    elif time_range == "7d":
        seven_days_ago = now - timedelta(days=7)
        query = query.filter(AuditLog.timestamp >= seven_days_ago)
    elif time_range == "30d":
        thirty_days_ago = now - timedelta(days=30)
        query = query.filter(AuditLog.timestamp >= thirty_days_ago)
    elif start_date or end_date:
        if start_date:
            try:
                s_dt = datetime.strptime(start_date, "%Y-%m-%d")
                query = query.filter(AuditLog.timestamp >= s_dt)
            except ValueError:
                pass
        if end_date:
            try:
                e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                query = query.filter(AuditLog.timestamp <= e_dt)
            except ValueError:
                pass

    if action_filter:
        query = query.filter(AuditLog.action == action_filter)
    if status_filter:
        query = query.filter(AuditLog.status == status_filter)
    if q:
        q_clean = f"%{q.strip()}%"
        query = query.filter(
            or_(
                AuditLog.target_id.ilike(q_clean),
                AuditLog.username.ilike(q_clean),
                AuditLog.action.ilike(q_clean),
                AuditLog.details.ilike(q_clean)
            )
        )

    logs = query.order_by(AuditLog.id.desc()).limit(10000).all()

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Event ID", "Timestamp (IST)", "User", "Role", "Action", "Target Type", "Target ID", "Status", "Details", "Reason"])
        for l in logs:
            writer.writerow([l.id, fmt_datetime(l.timestamp), l.username, l.role, l.action, l.target_type, l.target_id, l.status, l.details, l.reason or ""])
        output.seek(0)
        filename = f"audit_trail_{today_kolkata_str()}.csv"
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Audit Trail"
        ws.append(["Event ID", "Timestamp (IST)", "User", "Role", "Action", "Target Type", "Target ID", "Status", "Details", "Reason"])
        for l in logs:
            ws.append([l.id, fmt_datetime(l.timestamp), l.username, l.role, l.action, l.target_type, l.target_id, l.status, l.details, l.reason or ""])
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        filename = f"audit_trail_{today_kolkata_str()}.xlsx"
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

# -------------------------------------------------------------
# 8. DEVICE HISTORY TIMELINE
# -------------------------------------------------------------
@router.get("/device/timeline/{serial}", response_class=HTMLResponse)
async def admin_device_timeline(
    serial: str,
    request: Request,
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    dev = db.query(Device).filter(Device.serial == serial).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found.")

    emp_assigned = db.query(Employee).filter(Employee.employee_id == dev.assigned_employee_id).first() if dev.assigned_employee_id else None

    events = []
    # 1. Device Creation
    events.append({
        "timestamp": dev.created_at,
        "timestamp_fmt": fmt_datetime(dev.created_at),
        "type": "REGISTRATION",
        "title": "Device Registered in System",
        "description": f"Model: {dev.make} {dev.model} · Initial Status: {dev.status}",
        "actor": "Inventory Manager / Admin",
        "badge": "badge-secondary"
    })

    # 2. Transactions (Issue & Return)
    txs = db.query(Transaction).filter(Transaction.serial == serial).all()
    for t in txs:
        is_issue = t.transaction_type == "Issue"
        events.append({
            "timestamp": t.transaction_time,
            "timestamp_fmt": fmt_datetime(t.transaction_time),
            "type": t.transaction_type.upper(),
            "title": f"Device {t.transaction_type}",
            "description": f"Worker: {t.employee_name} ({t.employee_id}, {t.department}) · Issued by: {t.issued_by_operator_name or t.operator_name} · Returned by: {t.returned_by_operator_name or t.operator_name}",
            "actor": t.operator_name,
            "badge": "badge-issued" if is_issue else "badge-available"
        })

    # 3. Damage Reports
    dmgs = db.query(DamageReport).filter(DamageReport.serial == serial).all()
    for d in dmgs:
        events.append({
            "timestamp": d.report_time,
            "timestamp_fmt": fmt_datetime(d.report_time),
            "type": "DAMAGE_REPORT",
            "title": f"Fault Reported: {d.fault_type}",
            "description": f"{d.details} · Review status: {d.review_status}",
            "actor": d.reported_by_name or "Operator",
            "badge": "badge-damaged"
        })

    # 4. Service Records
    services = db.query(ServiceRecord).filter(ServiceRecord.serial == serial).all()
    for s in services:
        events.append({
            "timestamp": s.created_at,
            "timestamp_fmt": fmt_datetime(s.created_at),
            "type": "SERVICE_CENTRE",
            "title": f"Sent to Service Centre: {s.service_centre_name}",
            "description": f"Ref: {s.external_reference or 'N/A'} · Status: {s.status} · Sent by: {s.sent_by_name}",
            "actor": s.sent_by_name,
            "badge": "badge-warning"
        })

    # 5. Missing Events & Recoveries
    missings = db.query(MissingEvent).filter(MissingEvent.serial == serial).all()
    for m in missings:
        events.append({
            "timestamp": m.confirmed_at,
            "timestamp_fmt": fmt_datetime(m.confirmed_at),
            "type": "CONFIRMED_MISSING",
            "title": "Officially Confirmed Missing",
            "description": f"Reason: {m.reason} · Notes: {m.notes} · Confirmed by: {m.confirmed_by_name}",
            "actor": m.confirmed_by_name,
            "badge": "badge-damaged"
        })
        if m.recovered_at:
            events.append({
                "timestamp": m.recovered_at,
                "timestamp_fmt": fmt_datetime(m.recovered_at),
                "type": "RECOVERED",
                "title": "Device Physical Recovery Recorded",
                "description": f"Found by: {m.found_by} · Physical Condition: {m.recovery_condition} · Post-recovery Status: {m.post_recovery_status} · Notes: {m.recovery_notes}",
                "actor": m.recovered_by_name,
                "badge": "badge-available"
            })

    events.sort(key=lambda x: x["timestamp"] or datetime.min, reverse=True)

    incident_count = len(dmgs) + len(missings) + len(services)

    return templates.TemplateResponse(request=request, name="admin/timeline.html", context={
        "title": f"Timeline: {serial} - HHD Management",
        "user": user,
        "device": dev,
        "assigned_employee": emp_assigned,
        "events": events,
        "total_transactions": len(txs),
        "incident_count": incident_count
    })

# -------------------------------------------------------------
# 9. SYSTEM SETTINGS (DAILY REPORTING CUTOFF)
# -------------------------------------------------------------
@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    current_cutoff = get_configured_cutoff_time(db)
    return templates.TemplateResponse(request=request, name="admin/settings.html", context={
        "title": "Settings - HHD Management",
        "user": admin,
        "current_cutoff": current_cutoff,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.post("/settings")
async def update_admin_settings(
    request: Request,
    daily_cutoff_time: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    time_str = daily_cutoff_time.strip()
    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError()
    except Exception:
        return RedirectResponse("/admin/settings?error=Invalid+time+format.+Expected+HH:MM+24-hour+format.", status_code=302)

    setting = db.query(SystemSetting).filter(SystemSetting.key == "daily_cutoff_time").first()
    prev_val = setting.value if setting else "19:00"
    if setting:
        setting.value = time_str
        setting.updated_at = now_kolkata()
    else:
        setting = SystemSetting(key="daily_cutoff_time", value=time_str, description="Daily Reporting Cutoff Time")
        db.add(setting)

    record_audit(
        db=db, user=admin, action="SETTINGS_UPDATED", target_type="SYSTEM", target_id="daily_cutoff_time",
        details=f"Daily reporting cutoff changed: {prev_val} -> {time_str} by {admin.username}",
        previous_value=prev_val, new_value=time_str, status="SUCCESS"
    )
    db.commit()

    return RedirectResponse(f"/admin/settings?msg=Daily+cutoff+time+updated+to+{time_str}", status_code=302)

# -------------------------------------------------------------
# 10. USER MANAGEMENT (ADMIN, INVENTORY MANAGER, OPERATOR)
# -------------------------------------------------------------
@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    raw_users = db.query(User).order_by(User.role.asc(), User.username.asc()).all()
    legacy_default_usernames = {"hhdadmin", "operator1", "operator2"}
    legacy_accounts = [u.username for u in raw_users if u.username in legacy_default_usernames]
    user_rows = []
    for u in raw_users:
        is_locked, rem_time = login_limiter.is_locked_out(u.username.lower())
        user_rows.append({
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "active": u.active,
            "is_locked": is_locked,
            "lockout_remaining": rem_time,
            "created_at": u.created_at,
            "is_legacy_default": u.username in legacy_default_usernames
        })
    return templates.TemplateResponse(request=request, name="admin/users.html", context={
        "title": "User Accounts - Admin",
        "user": admin,
        "users": user_rows,
        "roles": [ROLE_ADMIN, ROLE_INVENTORY_MANAGER, ROLE_OPERATOR],
        "legacy_accounts": legacy_accounts,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.post("/users")
async def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("operator"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    uname = username.strip()
    if not uname or len(password) < 8 or role not in VALID_ROLES:
        return RedirectResponse("/admin/users?error=Username,+valid+role,+and+minimum+8+character+password+required", status_code=302)

    if password.lower() == uname.lower():
        return RedirectResponse("/admin/users?error=Password+cannot+be+the+same+as+username", status_code=302)

    existing = db.query(User).filter(func.lower(User.username) == uname.lower()).first()
    if existing:
        return RedirectResponse("/admin/users?error=Username+already+exists", status_code=302)

    now = now_kolkata()
    user = User(
        username=uname,
        password_hash=hash_password(password),
        full_name=full_name.strip() or uname,
        role=role,
        active=True,
        created_at=now,
        updated_at=now
    )
    db.add(user)
    db.flush()

    record_audit(
        db=db, user=admin, action="USER_CREATED", target_type="USER", target_id=str(user.id),
        details=f"Created {role} account '{uname}' ({full_name.strip() or uname}) by admin '{admin.username}'."
    )
    db.commit()

    return RedirectResponse("/admin/users?msg=User+account+created+successfully", status_code=302)

@router.post("/users/{user_id}/toggle")
async def toggle_user_active(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    if user_id == admin.id:
        return RedirectResponse("/admin/users?error=You+cannot+deactivate+your+own+admin+account", status_code=302)

    u = db.query(User).filter(User.id == user_id).first()
    if u:
        u.active = not u.active
        u.updated_at = now_kolkata()
        record_audit(
            db=db, user=admin, action="USER_TOGGLED", target_type="USER", target_id=str(u.id),
            details=f"User {u.username} active status set to {u.active} by admin {admin.username}"
        )
        db.commit()

    return RedirectResponse("/admin/users?msg=User+status+updated", status_code=302)

@router.post("/users/{user_id}/unlock")
async def unlock_user_account(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        login_limiter.unlock_user(u.username)
        u.active = True
        u.updated_at = now_kolkata()
        record_audit(
            db=db, user=admin, action="USER_UNLOCKED", target_type="USER", target_id=str(u.id),
            details=f"User {u.username} unlocked by admin {admin.username}"
        )
        db.commit()
    return RedirectResponse("/admin/users?msg=User+account+unlocked+successfully", status_code=302)

# -------------------------------------------------------------
# 11. CONTROLLED INITIAL DATA RESET (DEMO DATA CLEANUP)
# -------------------------------------------------------------
@router.get("/reset", response_class=HTMLResponse)
async def reset_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    dev_count = db.query(Device).count()
    emp_count = db.query(Employee).count()
    tx_count = db.query(Transaction).count()
    asgn_count = db.query(DeviceAssignment).count()
    dmg_count = db.query(DamageReport).count()

    return templates.TemplateResponse(request=request, name="admin/reset.html", context={
        "title": "Controlled Initial Data Reset - Admin",
        "user": admin,
        "counts": {
            "devices": dev_count,
            "employees": emp_count,
            "transactions": tx_count,
            "assignments": asgn_count,
            "damage": dmg_count
        },
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.post("/reset")
async def execute_initial_data_reset(
    request: Request,
    admin_password: str = Form(...),
    confirmation_text: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    if not verify_password(admin_password, admin.password_hash):
        return RedirectResponse("/admin/reset?error=Invalid+admin+password.+Reset+aborted.", status_code=302)

    if confirmation_text.strip() != "CONFIRM RESET":
        return RedirectResponse("/admin/reset?error=Confirmation+text+must+match+'CONFIRM+RESET'+exactly.", status_code=302)

    backup_res = create_safe_backup("pre_initial_data_reset")

    db.query(DeviceAssignment).delete()
    db.query(Transaction).delete()
    db.query(DamageReport).delete()
    db.query(MissingEvent).delete()
    db.query(ServiceRecord).delete()
    db.query(Device).delete()
    db.query(Employee).delete()

    record_audit(
        db=db, user=admin, action="INITIAL_DATA_RESET", target_type="OPERATIONAL_DATA", target_id="ALL",
        details=f"Demo operational data cleared after safety backup {backup_res['filename']}."
    )
    db.commit()

    return RedirectResponse(
        "/admin/import?msg=Demo+data+successfully+reset.+You+can+now+import+your+real+company+Device+and+Employee+Excel+files.",
        status_code=302
    )
