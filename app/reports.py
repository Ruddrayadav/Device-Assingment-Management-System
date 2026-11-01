import io
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func, case
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from app.models import (
    Transaction, DamageReport, Device, DeviceAssignment, Employee, User
)
from app.timefmt import fmt_datetime, fmt_time
from app.config import now_kolkata, today_kolkata_str, DEFAULT_REPORT_DAYS

# Professional 24/7 Warehouse Palette
NAVY_HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
STEEL_HEADER_FILL = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
TEAL_HEADER_FILL = PatternFill(start_color="005E5D", end_color="005E5D", fill_type="solid")
GRAY_HEADER_FILL = PatternFill(start_color="475569", end_color="475569", fill_type="solid")
WHITE_BOLD_FONT = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="Segoe UI", size=14, bold=True, color="1F4E78")
SECTION_FONT = Font(name="Segoe UI", size=12, bold=True, color="1E293B")
REGULAR_FONT = Font(name="Segoe UI", size=10)
BOLD_FONT = Font(name="Segoe UI", size=10, bold=True)
ALERT_FONT = Font(name="Segoe UI", size=10, bold=True, color="DC2626")

THIN_BORDER = Border(
    left=Side(style='thin', color='CBD5E1'),
    right=Side(style='thin', color='CBD5E1'),
    top=Side(style='thin', color='CBD5E1'),
    bottom=Side(style='thin', color='CBD5E1')
)

def style_header_row(ws, row_idx: int, fill=NAVY_HEADER_FILL):
    for col_idx in range(1, ws.max_column + 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.fill = fill
        cell.font = WHITE_BOLD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[row_idx].height = 26

def autofit_columns(ws, min_width=12, max_width=45):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if "\n" in val_str:
                val_str = max(val_str.split("\n"), key=len)
            max_len = max(max_len, len(val_str))
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, min_width), max_width)

def generate_excel_report(
    db: Session,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    operator: Optional[str] = None,
    employee: Optional[str] = None,
    serial: Optional[str] = None,
    department: Optional[str] = None,
    transaction_type: Optional[str] = None,
    condition: Optional[str] = None,
    all_history: bool = False
) -> io.BytesIO:
    """
    High-performance, non-blocking 7-sheet continuous 24/7 operational Excel workbook.
    Optimized for high-volume warehouses (30,000+ transactions without locking scanning):
    - Defaults to last 30 days unless all_history=True or custom date range provided.
    - Yields transactions in batches without loading entire history into memory.
    - Uses grouped SQL queries to eliminate N+1 latency.
    - Pre-sets column dimensions for transaction log to avoid expensive cell-by-cell loops.
    """
    # Default date range to last 30 days if not explicitly specified and not all_history
    if not start_date and not all_history:
        start_date = (now_kolkata() - timedelta(days=DEFAULT_REPORT_DAYS)).strftime("%Y-%m-%d")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # Remove default blank sheet

    # -------------------------------------------------------------
    # Build filtered transaction query
    # -------------------------------------------------------------
    tx_q = db.query(Transaction)
    if start_date:
        tx_q = tx_q.filter(Transaction.transaction_date >= start_date)
    if end_date:
        tx_q = tx_q.filter(Transaction.transaction_date <= end_date)
    if operator:
        tx_q = tx_q.filter(Transaction.operator_name.ilike(f"%{operator}%"))
    if employee:
        tx_q = tx_q.filter(
            (Transaction.employee_id.ilike(f"%{employee}%")) |
            (Transaction.employee_name.ilike(f"%{employee}%"))
        )
    if serial:
        tx_q = tx_q.filter(Transaction.serial.ilike(f"%{serial}%"))
    if department:
        tx_q = tx_q.filter(Transaction.department == department)
    if transaction_type:
        tx_q = tx_q.filter(Transaction.transaction_type == transaction_type)
    if condition:
        tx_q = tx_q.filter(Transaction.condition == condition)

    # -------------------------------------------------------------
    # SHEET 1: Overall Summary
    # -------------------------------------------------------------
    ws1 = wb.create_sheet(title="Overall Summary")
    ws1.views.sheetView[0].showGridLines = True

    ws1.append(["HHD DEVICE MANAGEMENT SYSTEM - 24/7 OPERATIONAL REPORT"])
    ws1.cell(row=1, column=1).font = TITLE_FONT
    ws1.append([f"Report Generated: {fmt_datetime(now_kolkata())} (Asia/Kolkata)"])
    ws1.cell(row=2, column=1).font = REGULAR_FONT

    filter_desc = []
    if start_date or end_date:
        filter_desc.append(f"Date: {start_date or 'Beginning'} to {end_date or 'Current'}")
    if all_history:
        filter_desc.append("All Historical Data")
    if operator:
        filter_desc.append(f"Operator: {operator}")
    if department:
        filter_desc.append(f"Department: {department}")
    if filter_desc:
        ws1.append([f"Scope & Filters: {', '.join(filter_desc)}"])
    else:
        ws1.append(["Scope: Last 30 Days (Continuous 24/7)"])
    ws1.append([])

    # Metrics via fast SQL queries
    total_devs = db.query(func.count(Device.serial)).scalar() or 0
    avail_devs = db.query(func.count(Device.serial)).filter(Device.status == "Available").scalar() or 0
    issued_devs = db.query(func.count(Device.serial)).filter(Device.status == "Issued").scalar() or 0
    damaged_devs = db.query(func.count(Device.serial)).filter(Device.status == "Damaged").scalar() or 0
    repair_devs = db.query(func.count(Device.serial)).filter(Device.status == "Under Repair").scalar() or 0
    missing_devs = db.query(func.count(Device.serial)).filter(Device.status == "Missing").scalar() or 0

    total_emps = db.query(func.count(Employee.employee_id)).scalar() or 0
    active_emps = db.query(func.count(Employee.employee_id)).filter(Employee.active == True).scalar() or 0

    period_issues = tx_q.filter(Transaction.transaction_type == "Issue").count()
    period_returns = tx_q.filter(Transaction.transaction_type == "Return").count()
    open_damage_cnt = db.query(func.count(DamageReport.id)).filter(DamageReport.review_status.in_(["Open", "Pending Inspection"])).scalar() or 0

    ws1.append(["Key Operational Metric", "Value"])
    metrics = [
        ("Total Devices", total_devs),
        ("Available in Warehouse", avail_devs),
        ("Currently Issued to Employees", issued_devs),
        ("Damaged Devices", damaged_devs),
        ("Devices Under Repair", repair_devs),
        ("Missing / Lost Devices", missing_devs),
        ("Total Registered Employees", total_emps),
        ("Active Eligible Employees", active_emps),
        ("Total Issues in Reporting Period", period_issues),
        ("Total Returns in Reporting Period", period_returns),
        ("Open Unresolved Damage Reports", open_damage_cnt)
    ]
    style_header_row(ws1, 5, fill=NAVY_HEADER_FILL)
    for m_label, m_val in metrics:
        ws1.append([m_label, m_val])
        row_c = ws1.max_row
        ws1.cell(row=row_c, column=1).font = BOLD_FONT
        ws1.cell(row=row_c, column=2).font = REGULAR_FONT
        ws1.cell(row=row_c, column=1).border = THIN_BORDER
        ws1.cell(row=row_c, column=2).border = THIN_BORDER

    # Department distribution
    ws1.append([])
    ws1.append(["Department Device Distribution"])
    ws1.cell(row=ws1.max_row, column=1).font = SECTION_FONT
    dept_header_row = ws1.max_row + 1
    ws1.append(["Department", "Total Devices", "Available", "Issued", "Damaged / Repair"])
    style_header_row(ws1, dept_header_row, fill=STEEL_HEADER_FILL)

    dept_rows = db.query(
        Device.department,
        func.count(Device.serial),
        func.sum(case((Device.status == "Available", 1), else_=0)),
        func.sum(case((Device.status == "Issued", 1), else_=0)),
        func.sum(case((Device.status.in_(["Damaged", "Under Repair"]), 1), else_=0))
    ).group_by(Device.department).all()

    for d_row in dept_rows:
        ws1.append([d_row[0] or "GENERAL", d_row[1], d_row[2] or 0, d_row[3] or 0, d_row[4] or 0])
        r_c = ws1.max_row
        for col_i in range(1, 6):
            ws1.cell(row=r_c, column=col_i).font = REGULAR_FONT
            ws1.cell(row=r_c, column=col_i).border = THIN_BORDER

    autofit_columns(ws1)

    # -------------------------------------------------------------
    # SHEET 2: Transaction Details (Batched Streaming)
    # -------------------------------------------------------------
    ws2 = wb.create_sheet(title="Transaction Details")
    ws2.views.sheetView[0].showGridLines = True
    ws2.append([
        "ID", "Exact Timestamp", "Date", "Action Type", "Device Serial",
        "Employee ID", "Employee Name", "Department",
        "Operator ID", "Operator Name", "Condition", "Covers", "Remarks"
    ])
    style_header_row(ws2, 1, fill=NAVY_HEADER_FILL)
    ws2.freeze_panes = "A2"

    # Set predetermined column widths to avoid heavy O(N*M) cell scanning
    tx_col_widths = {
        "A": 10, "B": 24, "C": 14, "D": 14, "E": 18,
        "F": 16, "G": 22, "H": 20, "I": 12, "J": 20,
        "K": 14, "L": 10, "M": 30
    }
    for col_letter, width in tx_col_widths.items():
        ws2.column_dimensions[col_letter].width = width

    # Stream in chunks of 1000 without loading all rows into ORM memory
    for t in tx_q.order_by(Transaction.id.desc()).yield_per(1000):
        ws2.append([
            t.id,
            fmt_datetime(t.transaction_time),
            t.transaction_date,
            t.transaction_type,
            t.serial,
            t.employee_id,
            t.employee_name,
            t.department,
            t.operator_id or "",
            t.operator_name,
            t.condition,
            t.cover_qty,
            t.remarks
        ])

    # -------------------------------------------------------------
    # SHEET 3: Device Inventory
    # -------------------------------------------------------------
    ws3 = wb.create_sheet(title="Device Inventory")
    ws3.views.sheetView[0].showGridLines = True
    ws3.append([
        "Serial Number", "Make", "Model", "Department",
        "Status", "Condition", "Assigned Employee ID", "Assigned Employee Name",
        "Inventory Date", "Notes", "Last Updated"
    ])
    style_header_row(ws3, 1, fill=STEEL_HEADER_FILL)
    ws3.freeze_panes = "A2"

    dev_list = db.query(Device).order_by(Device.serial).all()
    emp_map = {e.employee_id: e.name for e in db.query(Employee.employee_id, Employee.name).all()}

    for d in dev_list:
        holder_name = emp_map.get(d.assigned_employee_id, "") if d.assigned_employee_id else ""
        ws3.append([
            d.serial,
            d.make,
            d.model,
            getattr(d, "department", "GENERAL"),
            d.status,
            d.condition,
            d.assigned_employee_id or "",
            holder_name,
            getattr(d, "inventory_date", "") or "",
            d.notes,
            fmt_datetime(d.updated_at)
        ])
    autofit_columns(ws3)

    # -------------------------------------------------------------
    # SHEET 4: Employee Master
    # -------------------------------------------------------------
    ws4 = wb.create_sheet(title="Employee Master")
    ws4.views.sheetView[0].showGridLines = True
    ws4.append([
        "Employee ID", "Employee Name", "Department", "Active Status",
        "Currently Held Devices", "Created At"
    ])
    style_header_row(ws4, 1, fill=TEAL_HEADER_FILL)
    ws4.freeze_panes = "A2"

    emp_list = db.query(Employee).order_by(Employee.employee_id).all()
    held_by_emp = dict(
        db.query(Device.assigned_employee_id, func.count(Device.serial))
        .filter(Device.status == "Issued", Device.assigned_employee_id.isnot(None))
        .group_by(Device.assigned_employee_id).all()
    )

    for e in emp_list:
        ws4.append([
            e.employee_id,
            e.name,
            e.department,
            "Active" if e.active else "Inactive",
            held_by_emp.get(e.employee_id, 0),
            fmt_datetime(e.created_at)
        ])
    autofit_columns(ws4)

    # -------------------------------------------------------------
    # SHEET 5: Damage and Repair Reports
    # -------------------------------------------------------------
    ws5 = wb.create_sheet(title="Damage and Repair Reports")
    ws5.views.sheetView[0].showGridLines = True
    ws5.append([
        "ID", "Report Time", "Date", "Device Serial",
        "Employee ID", "Employee Name", "Department",
        "Fault Type", "Record Source", "Details", "Reported By", "Review Status",
        "Resolution Notes", "Resolved By", "Resolved At"
    ])
    style_header_row(ws5, 1, fill=NAVY_HEADER_FILL)
    ws5.freeze_panes = "A2"

    dmg_list = db.query(DamageReport).order_by(DamageReport.id.desc()).all()
    for d in dmg_list:
        ws5.append([
            d.id,
            fmt_datetime(d.report_time),
            getattr(d, "report_date", d.report_time.strftime("%Y-%m-%d")),
            d.serial,
            d.employee_id or "",
            d.employee_name or "",
            d.department or "",
            d.fault_type,
            getattr(d, "source", "Operator Report"),
            d.details,
            d.reported_by_name,
            d.review_status,
            d.resolution_notes or "",
            d.resolved_by or "",
            fmt_datetime(d.resolved_at) if d.resolved_at else ""
        ])
    autofit_columns(ws5)

    # -------------------------------------------------------------
    # SHEET 6: Outstanding Devices (Single Joined Query)
    # -------------------------------------------------------------
    ws6 = wb.create_sheet(title="Outstanding Devices")
    ws6.views.sheetView[0].showGridLines = True
    ws6.append([
        "Device Serial", "Make", "Model", "Department",
        "Assigned Employee ID", "Employee Name", "Employee Department",
        "Issued Timestamp", "Issued Date", "Issued By Operator", "Held Duration (Hours)"
    ])
    style_header_row(ws6, 1, fill=GRAY_HEADER_FILL)
    ws6.freeze_panes = "A2"

    active_rows = db.query(
        DeviceAssignment.device_serial,
        Device.make,
        Device.model,
        Device.department,
        DeviceAssignment.employee_id,
        Employee.name,
        Employee.department,
        DeviceAssignment.issued_at,
        DeviceAssignment.issued_date,
        DeviceAssignment.issued_by_operator_name
    ).outerjoin(Device, Device.serial == DeviceAssignment.device_serial
    ).outerjoin(Employee, Employee.employee_id == DeviceAssignment.employee_id
    ).filter(DeviceAssignment.status == "ACTIVE").all()

    now_dt = now_kolkata()
    for a in active_rows:
        duration_hrs = round((now_dt - a[7]).total_seconds() / 3600, 1) if a[7] else 0
        ws6.append([
            a[0],
            a[1] or "",
            a[2] or "",
            a[3] or "GENERAL",
            a[4],
            a[5] or "",
            a[6] or "",
            fmt_datetime(a[7]),
            a[8] or (a[7].strftime("%Y-%m-%d") if a[7] else ""),
            a[9] or "",
            duration_hrs
        ])
    autofit_columns(ws6)

    # -------------------------------------------------------------
    # SHEET 7: Operator Activity Summary (Single Grouped Query)
    # -------------------------------------------------------------
    ws7 = wb.create_sheet(title="Operator Activity Summary")
    ws7.views.sheetView[0].showGridLines = True
    ws7.append([
        "Operator ID", "Operator Username", "Operator Full Name", "Role", "Status",
        "Total Transactions", "Issues Done", "Returns Handled", "Damages Logged",
        "First Activity", "Last Activity"
    ])
    style_header_row(ws7, 1, fill=STEEL_HEADER_FILL)
    ws7.freeze_panes = "A2"

    operators = db.query(User).order_by(User.full_name).all()

    # Bulk aggregation query per operator
    op_stats_q = db.query(
        Transaction.operator_id,
        func.count(Transaction.id),
        func.sum(case((Transaction.transaction_type == "Issue", 1), else_=0)),
        func.sum(case((Transaction.transaction_type == "Return", 1), else_=0)),
        func.min(Transaction.transaction_time),
        func.max(Transaction.transaction_time)
    )
    if start_date:
        op_stats_q = op_stats_q.filter(Transaction.transaction_date >= start_date)
    if end_date:
        op_stats_q = op_stats_q.filter(Transaction.transaction_date <= end_date)
    op_stats = {r[0]: r for r in op_stats_q.group_by(Transaction.operator_id).all()}

    dmg_by_op = dict(
        db.query(DamageReport.reported_by_id, func.count(DamageReport.id))
        .filter(DamageReport.reported_by_id.isnot(None))
        .group_by(DamageReport.reported_by_id).all()
    )

    for op in operators:
        stats = op_stats.get(op.id)
        total_tx = stats[1] if stats else 0
        issues_cnt = stats[2] if stats else 0
        returns_cnt = stats[3] if stats else 0
        first_act = stats[4] if stats else None
        last_act = stats[5] if stats else None
        dmg_cnt = dmg_by_op.get(op.id, 0)

        ws7.append([
            op.id,
            op.username,
            op.full_name,
            op.role,
            "Active" if op.active else "Inactive",
            total_tx,
            issues_cnt,
            returns_cnt,
            dmg_cnt,
            fmt_datetime(first_act) if first_act else "No activity",
            fmt_datetime(last_act) if last_act else "No activity"
        ])
    autofit_columns(ws7)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
