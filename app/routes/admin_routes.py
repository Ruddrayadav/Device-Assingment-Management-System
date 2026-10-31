import csv
import io
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException, status, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, case
import openpyxl

from app.database import get_db
from app.models import (
    User, Device, Employee, Transaction, DamageReport,
    DeviceAssignment, AuditLog
)
from app.auth import require_admin, hash_password, verify_password, login_limiter
from app.backup import create_safe_backup
from app.config import DAMAGE_CATEGORIES, DEVICE_STATUSES, now_kolkata, today_kolkata_str
from app.timefmt import fmt_datetime


BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(prefix="/admin")

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
    today_str = date.today().isoformat()

    # Core Device KPIs
    total_devices = db.query(Device).count()
    available_devices = db.query(Device).filter(Device.status == "Available").count()
    issued_devices = db.query(Device).filter(Device.status == "Issued").count()
    damaged_devices = db.query(Device).filter(Device.status == "Damaged").count()
    missing_devices = db.query(Device).filter(Device.status == "Missing").count()
    repair_devices = db.query(Device).filter(Device.status == "Under Repair").count()

    # Core Employee & Operator KPIs
    total_employees = db.query(Employee).count()
    active_employees = db.query(Employee).filter(Employee.active == True).count()
    total_operators = db.query(User).filter(User.role == "operator").count()

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
    all_depts = [d[0] for d in db.query(Device.department).distinct().all() if d[0]]

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
            "total_employees": total_employees,
            "active_employees": active_employees,
            "total_operators": total_operators,
            "today_issues": today_issues,
            "today_returns": today_returns,
            "outstanding_count": outstanding_count,
            "open_damage_count": open_damage_count
        },
        "today_str": today_str,
        "recent_txs": recent_txs,
        "open_damage": open_damage,
        "operators": all_operators,
        "departments": all_depts,
        "start_date": start_date,
        "end_date": end_date,
        "operator_filter": operator_filter,
        "dept_filter": dept_filter
    })

# -------------------------------------------------------------
# 2. DEVICE INVENTORY MANAGEMENT
# -------------------------------------------------------------
@router.get("/devices", response_class=HTMLResponse)
async def admin_devices(
    request: Request,
    q: Optional[str] = "",
    status_filter: Optional[str] = "",
    dept_filter: Optional[str] = "",
    admin: User = Depends(require_admin),
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
                Device.department.ilike(q_clean),
                Device.notes.ilike(q_clean)
            )
        )
    if status_filter and status_filter != "All":
        query = query.filter(Device.status == status_filter)
    if dept_filter and dept_filter != "All":
        query = query.filter(Device.department == dept_filter)

    devices = query.order_by(Device.serial.asc()).limit(2000).all()

    device_rows = []
    emp_map = {e.employee_id: e.name for e in db.query(Employee).all()}
    for d in devices:
        holder_name = emp_map.get(d.assigned_employee_id, "") if d.assigned_employee_id else ""
        device_rows.append({
            "serial": d.serial,
            "make": d.make,
            "model": d.model,
            "department": getattr(d, "department", "GENERAL"),
            "inventory_date": getattr(d, "inventory_date", "") or "",
            "cover_qty": d.cover_qty,
            "status": d.status,
            "condition": d.condition,
            "notes": d.notes,
            "assigned_employee_id": d.assigned_employee_id,
            "employee_name": holder_name,
            "updated_at": fmt_datetime(d.updated_at)
        })

    all_depts = [d[0] for d in db.query(Device.department).distinct().all() if d[0]]

    return templates.TemplateResponse(request=request, name="admin/devices.html", context={
        "title": "Device Fleet Master - Admin",
        "user": admin,
        "devices": device_rows,
        "statuses": DEVICE_STATUSES,
        "departments": all_depts,
        "q": q,
        "status_filter": status_filter,
        "dept_filter": dept_filter
    })

@router.post("/devices")
async def save_device(
    request: Request,
    serial: str = Form(...),
    make: str = Form("CHAINWAY"),
    model: str = Form("HHD"),
    department: str = Form("GENERAL"),
    status: str = Form("Available"),
    condition: str = Form("Working"),
    cover_qty: int = Form(0),
    inventory_date: Optional[str] = Form(""),
    notes: Optional[str] = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    serial_clean = serial.strip()
    if not serial_clean:
        return RedirectResponse("/admin/devices?error=Serial+number+is+required", status_code=302)

    dev = db.query(Device).filter(Device.serial == serial_clean).first()
    now = datetime.now()

    if dev:
        # Update existing
        dev.make = make.strip()
        dev.model = model.strip()
        dev.department = department.strip() or "GENERAL"
        if inventory_date:
            dev.inventory_date = inventory_date.strip()
        dev.cover_qty = cover_qty
        if dev.status != "Issued":
            dev.status = status
            dev.condition = condition
        dev.notes = notes.strip() if notes else ""
        dev.updated_at = now
    else:
        # Create new
        dev = Device(
            serial=serial_clean,
            make=make.strip(),
            model=model.strip(),
            department=department.strip() or "GENERAL",
            inventory_date=inventory_date.strip() if inventory_date else date.today().isoformat(),
            cover_qty=cover_qty,
            status=status,
            condition=condition,
            notes=notes.strip() if notes else "Added via Admin UI",
            created_at=now,
            updated_at=now
        )
        db.add(dev)

    db.commit()
    return RedirectResponse(f"/admin/devices?msg=Device+{serial_clean}+saved+successfully", status_code=302)

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
    admin: User = Depends(require_admin),
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
        "title": "Employee Master - Admin",
        "user": admin,
        "employees": emp_rows,
        "q": q,
        "status_filter": status_filter,
        "dept_filter": dept_filter,
        "departments": all_depts,
        "prefill": add_prefill
    })

@router.post("/employees")
async def save_employee(
    request: Request,
    employee_id: str = Form(...),
    name: str = Form(...),
    department: str = Form(...),
    active: bool = Form(True),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    eid = employee_id.strip()
    ename = name.strip()
    edept = department.strip()
    if not eid or not ename or not edept:
        return RedirectResponse("/admin/employees?error=All+fields+are+required", status_code=302)

    emp = db.query(Employee).filter(Employee.employee_id == eid).first()
    now = datetime.now()
    if emp:
        emp.name = ename
        emp.department = edept
        emp.active = active
        emp.updated_at = now
    else:
        emp = Employee(
            employee_id=eid,
            name=ename,
            department=edept,
            active=active,
            created_at=now,
            updated_at=now
        )
        db.add(emp)

    db.commit()
    return RedirectResponse(f"/admin/employees?msg=Employee+{eid}+saved+successfully", status_code=302)

# -------------------------------------------------------------
# 4. TRANSACTION AUDIT LOG - CONTINUOUS 24/7 (REAL PAGINATION)
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
    admin: User = Depends(require_admin),
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
        "user": admin,
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
# 5. DAMAGE & REPAIR MANAGEMENT & RECONCILIATION
# -------------------------------------------------------------
@router.get("/damage", response_class=HTMLResponse)
async def admin_damage(
    request: Request,
    status_filter: Optional[str] = "",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    query = db.query(DamageReport)
    if status_filter and status_filter != "All":
        query = query.filter(DamageReport.review_status == status_filter)

    reports = query.order_by(
        case((DamageReport.review_status == "Open", 1), (DamageReport.review_status == "Pending Inspection", 2), (DamageReport.review_status == "Under Repair", 3), else_=4),
        DamageReport.id.desc()
    ).limit(1000).all()

    # Damage Reconciliation Checks (Part 5 Requirements)
    open_dmg_serials = set(
        r[0] for r in db.query(DamageReport.serial)
        .filter(DamageReport.review_status.in_(["Open", "Pending Inspection"]))
        .all()
    )
    open_rep_serials = set(
        r[0] for r in db.query(DamageReport.serial)
        .filter(DamageReport.review_status.in_(["Open", "Pending Inspection", "Under Repair"]))
        .all()
    )

    damaged_without_records = db.query(Device).filter(
        Device.status == "Damaged",
        Device.serial.notin_(open_dmg_serials) if open_dmg_serials else True
    ).all()

    repair_without_records = db.query(Device).filter(
        Device.status == "Under Repair",
        Device.serial.notin_(open_rep_serials) if open_rep_serials else True
    ).all()

    avail_serials = set(r[0] for r in db.query(Device.serial).filter(Device.status == "Available").all())
    open_dmg_available = db.query(DamageReport).filter(
        DamageReport.review_status.in_(["Open", "Pending Inspection"]),
        DamageReport.serial.in_(avail_serials) if avail_serials else False
    ).all()

    dup_rows = db.query(DamageReport.serial, func.count(DamageReport.id)).filter(
        DamageReport.review_status.in_(["Open", "Pending Inspection"])
    ).group_by(DamageReport.serial).having(func.count(DamageReport.id) > 1).all()
    duplicate_open_serials = [r[0] for r in dup_rows]

    reconciliation = {
        "damaged_without_records": damaged_without_records,
        "repair_without_records": repair_without_records,
        "open_dmg_available": open_dmg_available,
        "duplicate_open_serials": duplicate_open_serials,
        "has_issues": bool(damaged_without_records or repair_without_records or open_dmg_available or duplicate_open_serials)
    }

    return templates.TemplateResponse(request=request, name="admin/damage.html", context={
        "title": "Damage & Repair Management - Admin",
        "user": admin,
        "reports": reports,
        "status_filter": status_filter,
        "reconciliation": reconciliation,
        "damage_categories": DAMAGE_CATEGORIES
    })

@router.post("/damage/{report_id}/update")
async def update_damage_status(
    report_id: int,
    review_status: str = Form(...),
    resolution_notes: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    report = db.query(DamageReport).filter(DamageReport.id == report_id).first()
    if not report:
        return RedirectResponse("/admin/damage?error=Report+not+found", status_code=302)

    now = now_kolkata()
    report.review_status = review_status
    report.resolution_notes = resolution_notes.strip()
    if review_status in ("Repaired/Available", "Closed"):
        report.resolved_by = admin.full_name or admin.username
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
                dev.updated_at = now
    elif review_status == "Under Repair":
        dev = db.query(Device).filter(Device.serial == report.serial).first()
        if dev and dev.status != "Issued":
            dev.status = "Under Repair"
            dev.updated_at = now

    db.commit()
    return RedirectResponse("/admin/damage?msg=Damage+report+updated+successfully", status_code=302)

# -------------------------------------------------------------
# 5b. CONTINUOUS OUTSTANDING DEVICES REPORT & MISSING WORKFLOW
# -------------------------------------------------------------
@router.get("/outstanding", response_class=HTMLResponse)
async def admin_outstanding_devices(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    active_rows = db.query(
        DeviceAssignment, Device, Employee
    ).outerjoin(Device, Device.serial == DeviceAssignment.device_serial
    ).outerjoin(Employee, Employee.employee_id == DeviceAssignment.employee_id
    ).filter(DeviceAssignment.status == "ACTIVE"
    ).order_by(DeviceAssignment.issued_at.desc()).all()

    now_dt = now_kolkata()
    outstanding = []
    for asgn, dev, emp in active_rows:
        duration_hrs = round((now_dt - asgn.issued_at).total_seconds() / 3600, 1) if asgn.issued_at else 0
        outstanding.append({
            "serial": asgn.device_serial,
            "make": dev.make if dev else "",
            "model": dev.model if dev else "",
            "device_department": getattr(dev, "department", "GENERAL") if dev else "",
            "employee_id": asgn.employee_id,
            "employee_name": emp.name if emp else asgn.employee_id,
            "department": emp.department if emp else "",
            "issued_at": asgn.issued_at,
            "issued_at_fmt": fmt_datetime(asgn.issued_at),
            "issued_date": asgn.issued_date,
            "operator_name": asgn.issued_by_operator_name,
            "duration_hours": duration_hrs
        })

    return templates.TemplateResponse(request=request, name="admin/outstanding.html", context={
        "title": "Outstanding Issued Devices - Continuous 24/7",
        "user": admin,
        "outstanding": outstanding,
        "total_count": len(outstanding)
    })

@router.post("/devices/{serial}/mark-missing")
async def mark_device_missing(
    serial: str,
    notes: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    dev = db.query(Device).filter(Device.serial == serial).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found.")

    now = now_kolkata()
    dev.status = "Missing"
    dev.condition = "Missing"
    dev.notes = (dev.notes + f" | Marked Missing by {admin.full_name or admin.username}: {notes}").strip(" |")
    dev.updated_at = now

    # Close active assignment
    asgn = db.query(DeviceAssignment).filter(
        DeviceAssignment.device_serial == serial,
        DeviceAssignment.status == "ACTIVE"
    ).first()
    if asgn:
        asgn.status = "RETURNED"
        asgn.returned_at = now
        asgn.returned_date = today_kolkata_str()
        asgn.returned_by_operator_id = admin.id
        asgn.returned_by_operator_name = admin.full_name or admin.username
        asgn.condition_on_return = "Missing"
        asgn.remarks = f"Marked missing by admin: {notes}"

    audit = AuditLog(
        timestamp=now,
        user_id=admin.id,
        username=admin.username,
        action="MARK_DEVICE_MISSING",
        target_type="DEVICE",
        target_id=serial,
        details=f"Device marked missing by admin {admin.full_name or admin.username}. Notes: {notes}"
    )
    db.add(audit)
    db.commit()

    return RedirectResponse("/admin/outstanding?msg=Device+marked+as+Missing+successfully", status_code=302)


# -------------------------------------------------------------
# 6. USER MANAGEMENT (OPERATORS & ADMINS)
# -------------------------------------------------------------
@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    raw_users = db.query(User).order_by(User.role.asc(), User.username.asc()).all()
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
            "created_at": u.created_at
        })
    return templates.TemplateResponse(request=request, name="admin/users.html", context={
        "title": "User Accounts & Operators - Admin",
        "user": admin,
        "users": user_rows
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
    if not uname or len(password) < 6 or role not in ("admin", "operator"):
        return RedirectResponse("/admin/users?error=Username+and+minimum+6+char+password+required", status_code=302)

    existing = db.query(User).filter(User.username == uname).first()
    if existing:
        return RedirectResponse("/admin/users?error=Username+already+exists", status_code=302)

    now = datetime.now()
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
        u.updated_at = datetime.now()
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
        u.updated_at = datetime.now()
        db.commit()
    return RedirectResponse("/admin/users?msg=User+account+unlocked+successfully", status_code=302)

# -------------------------------------------------------------
# 7. CONTROLLED INITIAL DATA RESET (DEMO DATA CLEANUP)
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
    # 1. Verify password
    if not verify_password(admin_password, admin.password_hash):
        return RedirectResponse("/admin/reset?error=Invalid+admin+password.+Reset+aborted.", status_code=302)

    # 2. Verify confirmation phrase
    if confirmation_text.strip() != "CONFIRM RESET":
        return RedirectResponse("/admin/reset?error=Confirmation+text+must+match+'CONFIRM+RESET'+exactly.", status_code=302)

    # 3. Create verified pre-reset database backup
    backup_res = create_safe_backup("pre_initial_data_reset")

    # 4. Wipe operational demo data
    db.query(DeviceAssignment).delete()
    db.query(Transaction).delete()
    db.query(DamageReport).delete()
    db.query(Device).delete()
    db.query(Employee).delete()

    # Log audit entry
    audit = AuditLog(
        timestamp=datetime.now(),
        user_id=admin.id,
        username=admin.username,
        action="INITIAL_DATA_RESET",
        target_type="OPERATIONAL_DATA",
        target_id="ALL",
        details=f"Demo operational data cleared after safety backup {backup_res['filename']}."
    )
    db.add(audit)
    db.commit()

    return RedirectResponse(
        "/admin/import?msg=Demo+data+successfully+reset.+You+can+now+import+your+real+company+Device+and+Employee+Excel+files.",
        status_code=302
    )
