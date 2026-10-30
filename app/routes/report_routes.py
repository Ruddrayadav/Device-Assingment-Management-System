import csv
import io
from datetime import datetime
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import Response, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Device, Employee, Transaction, DamageReport
from app.auth import require_user, require_admin
from app.reports import generate_excel_report
from app.timefmt import fmt_datetime

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()

@router.get("/admin/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    operators = db.query(User.full_name).filter(User.full_name != "").distinct().all()
    departments = db.query(Device.department).distinct().all()
    return templates.TemplateResponse(request=request, name="admin/reports.html", context={
        "title": "Reports & Analytics - Admin",
        "user": admin,
        "operators": [o[0] for o in operators if o[0]],
        "departments": [d[0] for d in departments if d[0]]
    })

@router.get("/reports/excel")
async def download_excel_report(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    operator: Optional[str] = Query(None),
    employee: Optional[str] = Query(None),
    serial: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    excel_stream = generate_excel_report(
        db,
        start_date=start_date,
        end_date=end_date,
        operator=operator,
        employee=employee,
        serial=serial,
        department=department,
        transaction_type=transaction_type,
        condition=condition
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"HHD_Warehouse_Report_{timestamp}.xlsx"

    return Response(
        content=excel_stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.get("/reports/csv/devices")
async def export_devices_csv(
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    devices = db.query(Device).order_by(Device.serial.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Serial", "Make", "Model", "Department", "Cover Qty", "Status", "Condition", "Assigned Employee ID", "Assigned Employee Name", "Notes", "Updated At"])

    for d in devices:
        emp = db.query(Employee).filter(Employee.employee_id == d.assigned_employee_id).first() if d.assigned_employee_id else None
        writer.writerow([
            d.serial, d.make, d.model, getattr(d, "department", "GENERAL"),
            d.cover_qty, d.status, d.condition,
            d.assigned_employee_id or "", emp.name if emp else "",
            d.notes, fmt_datetime(d.updated_at)
        ])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Device_Inventory.csv"'}
    )

@router.get("/reports/csv/employees")
async def export_employees_csv(
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    employees = db.query(Employee).order_by(Employee.name.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Employee ID", "Name", "Department", "Active", "Assigned Devices"])

    for e in employees:
        held = db.query(Device).filter(Device.assigned_employee_id == e.employee_id, Device.status == "Issued").all()
        held_str = "; ".join(d.serial for d in held)
        writer.writerow([e.employee_id, e.name, e.department, "Yes" if e.active else "No", held_str])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Employee_Master.csv"'}
    )

@router.get("/reports/csv/transactions")
async def export_transactions_csv(
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    txs = db.query(Transaction).order_by(Transaction.id.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["TX ID", "Exact Time", "Date", "Action", "Device Serial", "Employee ID", "Employee Name", "Department", "Operator", "Condition", "Covers", "Remarks"])

    for t in txs:
        writer.writerow([
            t.id, fmt_datetime(t.transaction_time),
            t.transaction_date, t.transaction_type, t.serial,
            t.employee_id, t.employee_name, t.department, t.operator_name,
            t.condition, t.cover_qty, t.remarks
        ])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Transaction_Audit.csv"'}
    )

@router.get("/reports/csv/damage")
async def export_damage_csv(
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    dmgs = db.query(DamageReport).order_by(DamageReport.id.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report ID", "Report Time", "Date", "Device Serial", "Employee ID", "Employee Name", "Department", "Fault Type", "Details", "Reported By", "Review Status", "Resolution", "Resolved At"])

    for d in dmgs:
        writer.writerow([
            d.id, fmt_datetime(d.report_time),
            getattr(d, "report_date", d.report_time.strftime("%Y-%m-%d")),
            d.serial, d.employee_id or "", d.employee_name or "",
            d.department or "", d.fault_type, d.details, d.reported_by_name,
            d.review_status, d.resolution_notes or "",
            fmt_datetime(d.resolved_at) if d.resolved_at else ""
        ])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Damage_Reports.csv"'}
    )
