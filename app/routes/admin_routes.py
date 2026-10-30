import csv
import io
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, case
import openpyxl

from app.database import get_db
from app.models import (
    User, Device, Employee, Transaction, DamageReport,
    DeviceAssignment, ShiftLog, AuditLog
)
from app.auth import require_admin, hash_password
from app.shifts import calculate_shift_reconciliation, get_current_shift_and_op_date
from app.config import DAMAGE_CATEGORIES, DEVICE_STATUSES
from app.timefmt import fmt_datetime

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(prefix="/admin")

# -------------------------------------------------------------
# 1. ADMIN DASHBOARD
# -------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    total_devices = db.query(Device).count()
    available_devices = db.query(Device).filter(Device.status == "Available").count()
    issued_devices = db.query(Device).filter(Device.status == "Issued").count()
    damaged_devices = db.query(Device).filter(Device.status == "Damaged").count()
    missing_devices = db.query(Device).filter(Device.status == "Missing").count()
    repair_devices = db.query(Device).filter(Device.status == "Under Repair").count()

    total_employees = db.query(Employee).count()
    active_employees = db.query(Employee).filter(Employee.active == True).count()
    total_operators = db.query(User).filter(User.role == "operator").count()

    # Current shift reconciliation
    cur_shift, cur_opdate = get_current_shift_and_op_date()
    reconciliation = calculate_shift_reconciliation(db, cur_opdate, cur_shift)

    # Recent transactions
    recent_txs = db.query(Transaction).order_by(Transaction.id.desc()).limit(10).all()

    # Open damage reports
    open_damage = db.query(DamageReport).filter(DamageReport.review_status == "Open").order_by(DamageReport.id.desc()).limit(5).all()

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
            "total_operators": total_operators
        },
        "cur_shift": cur_shift,
        "cur_opdate": cur_opdate,
        "reconciliation": reconciliation,
        "recent_txs": recent_txs,
        "open_damage": open_damage
    })

# -------------------------------------------------------------
# 2. DEVICE INVENTORY MANAGEMENT
# -------------------------------------------------------------
@router.get("/devices", response_class=HTMLResponse)
async def admin_devices(
    request: Request,
    q: Optional[str] = "",
    status_filter: Optional[str] = "",
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
                Device.notes.ilike(q_clean)
            )
        )
    if status_filter and status_filter != "All":
        query = query.filter(Device.status == status_filter)

    devices = query.order_by(Device.serial.asc()).limit(1500).all()

    # Attach employee info
    device_rows = []
    for d in devices:
        emp = db.query(Employee).filter(Employee.employee_id == d.assigned_employee_id).first() if d.assigned_employee_id else None
        device_rows.append({
            "serial": d.serial,
            "make": d.make,
            "model": d.model,
            "cover_qty": d.cover_qty,
            "status": d.status,
            "condition": d.condition,
            "notes": d.notes,
            "assigned_employee_id": d.assigned_employee_id,
            "employee_name": emp.name if emp else "",
            "department": emp.department if emp else "",
            "updated_at": fmt_datetime(d.updated_at)
        })

    return templates.TemplateResponse(request=request, name="admin/devices.html", context={
        "title": "Device Inventory - Admin",
        "user": admin,
        "devices": device_rows,
        "q": q,
        "status_filter": status_filter,
        "device_statuses": DEVICE_STATUSES
    })

@router.post("/devices")
async def save_device(
    request: Request,
    serial: str = Form(...),
    make: str = Form("CHAINWAY"),
    model: str = Form("HHD"),
    cover_qty: int = Form(0),
    status: str = Form("Available"),
    condition: str = Form("Working"),
    notes: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    serial_clean = serial.strip()
    if not serial_clean:
        return RedirectResponse("/admin/devices?error=Serial+is+required", status_code=302)

    dev = db.query(Device).filter(Device.serial == serial_clean).first()
    now = datetime.now()
    if dev:
        dev.make = make.strip()
        dev.model = model.strip()
        dev.cover_qty = max(0, cover_qty)
        if dev.status != "Issued":
            dev.status = status
            dev.condition = condition
        dev.notes = notes.strip()
        dev.updated_at = now
    else:
        dev = Device(
            serial=serial_clean,
            make=make.strip(),
            model=model.strip(),
            cover_qty=max(0, cover_qty),
            status=status,
            condition=condition,
            notes=notes.strip(),
            created_at=now,
            updated_at=now
        )
        db.add(dev)

    db.commit()
    return RedirectResponse("/admin/devices?msg=Device+saved+successfully", status_code=302)

@router.post("/devices/import")
async def import_devices(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    contents = await file.read()
    now = datetime.now()
    imported_count = 0

    try:
        if file.filename.endswith(('.xlsx', '.xls')):
            wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
            sheet = wb.active
            rows = list(sheet.iter_rows(values_only=True))
            if len(rows) > 1:
                # Find column indexes
                headers = [str(h or "").strip().lower() for h in rows[0]]
                s_idx = next((i for i, h in enumerate(headers) if "serial" in h), 0)
                make_idx = next((i for i, h in enumerate(headers) if "make" in h), None)
                model_idx = next((i for i, h in enumerate(headers) if "model" in h), None)
                cover_idx = next((i for i, h in enumerate(headers) if "cover" in h), None)
                notes_idx = next((i for i, h in enumerate(headers) if "remark" in h or "note" in h), None)

                for r in rows[1:]:
                    if not r or len(r) <= s_idx or not r[s_idx]:
                        continue
                    serial = str(r[s_idx]).strip()
                    if not serial:
                        continue
                    make = str(r[make_idx]).strip() if (make_idx is not None and r[make_idx]) else "CHAINWAY"
                    model = str(r[model_idx]).strip() if (model_idx is not None and r[model_idx]) else "HHD"
                    try:
                        covers = int(r[cover_idx]) if (cover_idx is not None and r[cover_idx]) else 0
                    except (ValueError, TypeError):
                        covers = 0
                    notes = str(r[notes_idx]).strip() if (notes_idx is not None and r[notes_idx]) else ""

                    dev = db.query(Device).filter(Device.serial == serial).first()
                    if dev:
                        dev.make = make
                        dev.model = model
                        dev.cover_qty = covers
                        dev.notes = notes
                        dev.updated_at = now
                    else:
                        dev = Device(
                            serial=serial,
                            make=make,
                            model=model,
                            cover_qty=covers,
                            status="Available",
                            condition="Working",
                            notes=notes,
                            created_at=now,
                            updated_at=now
                        )
                        db.add(dev)
                    imported_count += 1
        else:
            # CSV import
            text = contents.decode("utf-8-sig", errors="replace")
            reader = csv.reader(io.StringIO(text))
            rows = list(reader)
            if len(rows) > 1:
                headers = [h.strip().lower() for h in rows[0]]
                s_idx = next((i for i, h in enumerate(headers) if "serial" in h), 0)
                make_idx = next((i for i, h in enumerate(headers) if "make" in h), None)
                model_idx = next((i for i, h in enumerate(headers) if "model" in h), None)

                for r in rows[1:]:
                    if not r or len(r) <= s_idx or not r[s_idx].strip():
                        continue
                    serial = r[s_idx].strip()
                    make = r[make_idx].strip() if (make_idx is not None and len(r) > make_idx) else "CHAINWAY"
                    model = r[model_idx].strip() if (model_idx is not None and len(r) > model_idx) else "HHD"

                    dev = db.query(Device).filter(Device.serial == serial).first()
                    if dev:
                        dev.make = make
                        dev.model = model
                        dev.updated_at = now
                    else:
                        dev = Device(
                            serial=serial,
                            make=make,
                            model=model,
                            status="Available",
                            condition="Working",
                            created_at=now,
                            updated_at=now
                        )
                        db.add(dev)
                    imported_count += 1

        db.commit()
        return RedirectResponse(f"/admin/devices?msg=Imported+{imported_count}+devices+successfully", status_code=302)
    except Exception as ex:
        db.rollback()
        return RedirectResponse(f"/admin/devices?error=Import+failed:+{str(ex)[:80]}", status_code=302)

# -------------------------------------------------------------
# 3. EMPLOYEE MASTER MANAGEMENT
# -------------------------------------------------------------
@router.get("/employees", response_class=HTMLResponse)
async def admin_employees(
    request: Request,
    q: Optional[str] = "",
    status_filter: Optional[str] = "",
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

    return templates.TemplateResponse(request=request, name="admin/employees.html", context={
        "title": "Employee Master - Admin",
        "user": admin,
        "employees": emp_rows,
        "q": q,
        "status_filter": status_filter,
        "prefill": add_prefill
    })

@router.post("/employees")
async def save_employee(
    request: Request,
    employee_id: str = Form(...),
    name: str = Form(...),
    department: str = Form(...),
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
        emp.active = True
        emp.updated_at = now
    else:
        emp = Employee(
            employee_id=eid,
            name=ename,
            department=edept,
            active=True,
            created_at=now,
            updated_at=now
        )
        db.add(emp)

    db.commit()
    return RedirectResponse("/admin/employees?msg=Employee+saved+successfully", status_code=302)

@router.post("/employees/{eid}/toggle")
async def toggle_employee_status(
    eid: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    emp = db.query(Employee).filter(Employee.employee_id == eid).first()
    if not emp:
        return RedirectResponse("/admin/employees?error=Employee+not+found", status_code=302)

    if emp.active:
        # Check if worker holds any devices
        held = db.query(Device).filter(Device.assigned_employee_id == emp.employee_id, Device.status == "Issued").first()
        if held:
            return RedirectResponse(f"/admin/employees?error=Cannot+deactivate+{emp.name}.+Device+{held.serial}+is+still+with+them.+Return+it+first.", status_code=302)
        emp.active = False
    else:
        emp.active = True

    emp.updated_at = datetime.now()
    db.commit()
    return RedirectResponse("/admin/employees?msg=Employee+status+updated", status_code=302)

@router.post("/employees/import")
async def import_employees(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    contents = await file.read()
    now = datetime.now()
    count = 0

    try:
        if file.filename.endswith(('.xlsx', '.xls')):
            wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
            sheet = wb.active
            rows = list(sheet.iter_rows(values_only=True))
            if len(rows) > 1:
                headers = [str(h or "").strip().lower() for h in rows[0]]
                id_idx = next((i for i, h in enumerate(headers) if "id" in h or "code" in h), 0)
                name_idx = next((i for i, h in enumerate(headers) if "name" in h), 1)
                dept_idx = next((i for i, h in enumerate(headers) if "dept" in h or "department" in h), 2)

                for r in rows[1:]:
                    if not r or len(r) <= id_idx or not r[id_idx]:
                        continue
                    eid = str(r[id_idx]).strip()
                    ename = str(r[name_idx]).strip() if len(r) > name_idx and r[name_idx] else "Worker"
                    edept = str(r[dept_idx]).strip() if len(r) > dept_idx and r[dept_idx] else "General"

                    emp = db.query(Employee).filter(Employee.employee_id == eid).first()
                    if emp:
                        emp.name = ename
                        emp.department = edept
                        emp.updated_at = now
                    else:
                        emp = Employee(
                            employee_id=eid,
                            name=ename,
                            department=edept,
                            active=True,
                            created_at=now,
                            updated_at=now
                        )
                        db.add(emp)
                    count += 1
        else:
            text = contents.decode("utf-8-sig", errors="replace")
            reader = csv.reader(io.StringIO(text))
            rows = list(reader)
            if len(rows) > 1:
                headers = [h.strip().lower() for h in rows[0]]
                id_idx = next((i for i, h in enumerate(headers) if "id" in h or "code" in h), 0)
                name_idx = next((i for i, h in enumerate(headers) if "name" in h), 1)
                dept_idx = next((i for i, h in enumerate(headers) if "dept" in h or "department" in h), 2)

                for r in rows[1:]:
                    if not r or len(r) <= id_idx or not r[id_idx].strip():
                        continue
                    eid = r[id_idx].strip()
                    ename = r[name_idx].strip() if len(r) > name_idx and r[name_idx].strip() else "Worker"
                    edept = r[dept_idx].strip() if len(r) > dept_idx and r[dept_idx].strip() else "General"

                    emp = db.query(Employee).filter(Employee.employee_id == eid).first()
                    if emp:
                        emp.name = ename
                        emp.department = edept
                        emp.updated_at = now
                    else:
                        emp = Employee(
                            employee_id=eid,
                            name=ename,
                            department=edept,
                            active=True,
                            created_at=now,
                            updated_at=now
                        )
                        db.add(emp)
                    count += 1

        db.commit()
        return RedirectResponse(f"/admin/employees?msg=Imported+{count}+employees+successfully", status_code=302)
    except Exception as ex:
        db.rollback()
        return RedirectResponse(f"/admin/employees?error=Import+failed:+{str(ex)[:80]}", status_code=302)

# -------------------------------------------------------------
# 4. FULL TRANSACTION AUDIT
# -------------------------------------------------------------
@router.get("/transactions", response_class=HTMLResponse)
async def admin_transactions(
    request: Request,
    q: Optional[str] = "",
    type_filter: Optional[str] = "",
    shift_filter: Optional[str] = "",
    date_filter: Optional[str] = "",
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
    if shift_filter and shift_filter != "All":
        query = query.filter(Transaction.shift_name == shift_filter)
    if date_filter:
        query = query.filter(Transaction.operational_date == date_filter)

    txs = query.order_by(Transaction.id.desc()).limit(2500).all()

    return templates.TemplateResponse(request=request, name="admin/transactions.html", context={
        "title": "Transaction Audit - Admin",
        "user": admin,
        "transactions": txs,
        "q": q,
        "type_filter": type_filter,
        "shift_filter": shift_filter,
        "date_filter": date_filter
    })

# -------------------------------------------------------------
# 5. DAMAGE & REPAIR MANAGEMENT
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
        case((DamageReport.review_status == "Open", 1), (DamageReport.review_status == "Under Repair", 2), else_=3),
        DamageReport.id.desc()
    ).limit(1000).all()

    return templates.TemplateResponse(request=request, name="admin/damage.html", context={
        "title": "Damage & Repair Management - Admin",
        "user": admin,
        "reports": reports,
        "status_filter": status_filter,
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

    now = datetime.now()
    report.review_status = review_status
    report.resolution_notes = resolution_notes.strip()
    if review_status in ("Repaired/Available", "Closed"):
        report.resolved_by = admin.full_name or admin.username
        report.resolved_at = now

        # Check if there are other open damage records for this device
        other_open = db.query(DamageReport).filter(
            DamageReport.serial == report.serial,
            DamageReport.id != report.id,
            DamageReport.review_status.in_(["Open", "Under Repair"])
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
# 6. SHIFTS & RECONCILIATION DASHBOARD
# -------------------------------------------------------------
@router.get("/shifts", response_class=HTMLResponse)
async def admin_shifts(
    request: Request,
    op_date: Optional[str] = "",
    shift_name: Optional[str] = "",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    cur_shift, cur_date = get_current_shift_and_op_date()
    target_date = op_date.strip() if op_date else cur_date
    target_shift = shift_name.strip() if shift_name else cur_shift

    reconciliation = calculate_shift_reconciliation(db, target_date, target_shift)

    # Shift history
    shift_logs = db.query(ShiftLog).order_by(ShiftLog.id.desc()).limit(30).all()

    return templates.TemplateResponse(request=request, name="admin/shifts.html", context={
        "title": "Shift Reconciliation - Admin",
        "user": admin,
        "cur_shift": cur_shift,
        "cur_date": cur_date,
        "target_date": target_date,
        "target_shift": target_shift,
        "reconciliation": reconciliation,
        "shift_logs": shift_logs
    })

@router.post("/shifts/close")
async def close_shift(
    request: Request,
    operational_date: str = Form(...),
    shift_name: str = Form(...),
    notes: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    reconciliation = calculate_shift_reconciliation(db, operational_date, shift_name)
    now = datetime.now()

    shift_log = db.query(ShiftLog).filter(
        ShiftLog.operational_date == operational_date,
        ShiftLog.shift_name == shift_name
    ).first()

    if not shift_log:
        shift_log = ShiftLog(
            operational_date=operational_date,
            shift_name=shift_name,
            opened_at=now,
            status="CLOSED",
            closed_at=now,
            closed_by_user_id=admin.id,
            closed_by_name=admin.full_name or admin.username,
            total_issued=reconciliation["total_issued"],
            total_returned=reconciliation["total_returned"],
            total_working=reconciliation["working_returned"],
            total_damaged=reconciliation["damaged_returned"],
            total_missing=reconciliation["missing_returned"],
            total_outstanding=reconciliation["outstanding_count"],
            notes=notes.strip()
        )
        db.add(shift_log)
    else:
        shift_log.status = "CLOSED"
        shift_log.closed_at = now
        shift_log.closed_by_user_id = admin.id
        shift_log.closed_by_name = admin.full_name or admin.username
        shift_log.total_issued = reconciliation["total_issued"]
        shift_log.total_returned = reconciliation["total_returned"]
        shift_log.total_working = reconciliation["working_returned"]
        shift_log.total_damaged = reconciliation["damaged_returned"]
        shift_log.total_missing = reconciliation["missing_returned"]
        shift_log.total_outstanding = reconciliation["outstanding_count"]
        shift_log.notes = notes.strip()

    db.commit()
    return RedirectResponse(f"/admin/shifts?op_date={operational_date}&shift_name={shift_name}&msg=Shift+closed+and+reconciled+successfully", status_code=302)

# -------------------------------------------------------------
# 7. USER MANAGEMENT
# -------------------------------------------------------------
@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    users = db.query(User).order_by(User.role.asc(), User.username.asc()).all()
    return templates.TemplateResponse(request=request, name="admin/users.html", context={
        "title": "User Accounts - Admin",
        "user": admin,
        "users": users
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
    if not uname or len(password) < 8 or role not in ("admin", "operator"):
        return RedirectResponse("/admin/users?error=Username+and+minimum+8+char+password+required", status_code=302)

    existing = db.query(User).filter(User.username == uname).first()
    if existing:
        return RedirectResponse("/admin/users?error=Username+already+exists", status_code=302)

    now = datetime.now()
    user = User(
        username=uname,
        password_hash=hash_password(password),
        full_name=full_name.strip() or uname,
        role=role,
        default_shift="Morning",
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
