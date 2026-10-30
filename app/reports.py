import io
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, case
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from app.models import (
    Transaction, DamageReport, Device, DeviceAssignment, Employee, ShiftLog
)

# Professional Color Palette
NAVY_HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
STEEL_HEADER_FILL = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
TEAL_HEADER_FILL = PatternFill(start_color="005E5D", end_color="005E5D", fill_type="solid")
WHITE_BOLD_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="1F4E78")
SECTION_FONT = Font(name="Calibri", size=12, bold=True, color="333333")
REGULAR_FONT = Font(name="Calibri", size=10)
BOLD_FONT = Font(name="Calibri", size=10, bold=True)
ALERT_FONT = Font(name="Calibri", size=10, bold=True, color="C00000")

THIN_BORDER = Border(
    left=Side(style='thin', color='D9D9D9'),
    right=Side(style='thin', color='D9D9D9'),
    top=Side(style='thin', color='D9D9D9'),
    bottom=Side(style='thin', color='D9D9D9')
)
BOTTOM_DOUBLE = Border(bottom=Side(style='double', color='1F4E78'))

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
    shift_name: Optional[str] = None
) -> io.BytesIO:
    """
    Generates a 5-sheet comprehensive, professional Excel workbook:
    Sheet 1: Daily Summary
    Sheet 2: Transaction Details
    Sheet 3: Damage Reports
    Sheet 4: Outstanding Devices
    Sheet 5: Shift Summary
    """
    wb = openpyxl.Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    # -------------------------------------------------------------
    # SHEET 1: Daily Summary
    # -------------------------------------------------------------
    ws1 = wb.create_sheet(title="Daily Summary")
    ws1.views.sheetView[0].showGridLines = True

    ws1.append(["HHD DEVICE ALLOCATION - DAILY OPERATIONAL SUMMARY"])
    ws1.cell(row=1, column=1).font = TITLE_FONT
    ws1.append([f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
    ws1.cell(row=2, column=1).font = REGULAR_FONT
    ws1.append([])

    headers1 = [
        "Operational Date", "Total Issued", "Total Returned",
        "Working Fleet", "Damaged Devices", "Missing Devices", "Under Repair", "Outstanding Balance"
    ]
    ws1.append(headers1)
    style_header_row(ws1, 4, NAVY_HEADER_FILL)

    # Aggregate by operational_date
    tx_query = db.query(
        Transaction.operational_date,
        func.sum(case((Transaction.transaction_type == 'Issue', 1), else_=0)).label("issued"),
        func.sum(case((Transaction.transaction_type == 'Return', 1), else_=0)).label("returned")
    )
    if start_date:
        tx_query = tx_query.filter(Transaction.operational_date >= start_date)
    if end_date:
        tx_query = tx_query.filter(Transaction.operational_date <= end_date)
    if shift_name and shift_name != "All":
        tx_query = tx_query.filter(Transaction.shift_name == shift_name)

    daily_stats = tx_query.group_by(Transaction.operational_date).order_by(Transaction.operational_date.desc()).all()

    fleet_working = db.query(Device).filter(Device.status.in_(["Available", "Issued"])).count()
    fleet_damaged = db.query(Device).filter(Device.status == "Damaged").count()
    fleet_missing = db.query(Device).filter(Device.status == "Missing").count()
    fleet_repair = db.query(Device).filter(Device.status == "Under Repair").count()

    row_num = 5
    for row in daily_stats:
        issued = row.issued or 0
        returned = row.returned or 0
        diff = issued - returned

        ws1.append([
            row.operational_date,
            issued,
            returned,
            fleet_working,
            fleet_damaged,
            fleet_missing,
            fleet_repair,
            diff
        ])
        for c in range(1, len(headers1) + 1):
            cell = ws1.cell(row=row_num, column=c)
            cell.font = REGULAR_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(horizontal="center" if c in (1, 8) else "right")
            if c == 8 and diff > 0:
                cell.font = ALERT_FONT
        row_num += 1

    ws1.freeze_panes = 'A5'
    autofit_columns(ws1)

    # -------------------------------------------------------------
    # SHEET 2: Transaction Details
    # -------------------------------------------------------------
    ws2 = wb.create_sheet(title="Transaction Details")
    ws2.views.sheetView[0].showGridLines = True

    headers2 = [
        "TX ID", "Timestamp", "Operational Date", "Shift", "Operator",
        "Employee ID", "Employee Name", "Department", "Device Serial",
        "Action", "Condition", "Covers", "Remarks"
    ]
    ws2.append(headers2)
    style_header_row(ws2, 1, STEEL_HEADER_FILL)

    tx_q = db.query(Transaction)
    if start_date:
        tx_q = tx_q.filter(Transaction.operational_date >= start_date)
    if end_date:
        tx_q = tx_q.filter(Transaction.operational_date <= end_date)
    if shift_name and shift_name != "All":
        tx_q = tx_q.filter(Transaction.shift_name == shift_name)
    txs = tx_q.order_by(Transaction.id.desc()).all()

    r2 = 2
    for t in txs:
        ws2.append([
            t.id,
            t.transaction_time.strftime("%Y-%m-%d %H:%M:%S") if t.transaction_time else "",
            t.operational_date,
            t.shift_name,
            t.operator_name,
            t.employee_id,
            t.employee_name,
            t.department,
            t.serial,
            t.transaction_type,
            t.condition,
            t.cover_qty,
            t.remarks
        ])
        for c in range(1, len(headers2) + 1):
            cell = ws2.cell(row=r2, column=c)
            cell.font = REGULAR_FONT
            cell.border = THIN_BORDER
            if c in (1, 4, 10, 11, 12):
                cell.alignment = Alignment(horizontal="center")
            elif c == 10:
                cell.font = BOLD_FONT
        r2 += 1

    ws2.freeze_panes = 'A2'
    ws2.auto_filter.ref = ws2.dimensions
    autofit_columns(ws2)

    # -------------------------------------------------------------
    # SHEET 3: Damage Reports
    # -------------------------------------------------------------
    ws3 = wb.create_sheet(title="Damage Reports")
    ws3.views.sheetView[0].showGridLines = True

    headers3 = [
        "Report ID", "Report Time", "Operational Date", "Shift", "Device Serial",
        "Employee ID", "Employee Name", "Department", "Fault Type",
        "Details", "Reported By", "Review Status", "Resolution", "Resolved At"
    ]
    ws3.append(headers3)
    style_header_row(ws3, 1, TEAL_HEADER_FILL)

    dmg_q = db.query(DamageReport)
    if start_date:
        dmg_q = dmg_q.filter(DamageReport.operational_date >= start_date)
    if end_date:
        dmg_q = dmg_q.filter(DamageReport.operational_date <= end_date)
    if shift_name and shift_name != "All":
        dmg_q = dmg_q.filter(DamageReport.shift_name == shift_name)
    dmgs = dmg_q.order_by(DamageReport.id.desc()).all()

    r3 = 2
    for d in dmgs:
        ws3.append([
            d.id,
            d.report_time.strftime("%Y-%m-%d %H:%M:%S") if d.report_time else "",
            d.operational_date,
            d.shift_name,
            d.serial,
            d.employee_id or "—",
            d.employee_name or "—",
            d.department or "—",
            d.fault_type,
            d.details,
            d.reported_by_name,
            d.review_status,
            d.resolution_notes or "",
            d.resolved_at.strftime("%Y-%m-%d %H:%M:%S") if d.resolved_at else ""
        ])
        for c in range(1, len(headers3) + 1):
            cell = ws3.cell(row=r3, column=c)
            cell.font = REGULAR_FONT
            cell.border = THIN_BORDER
            if c in (1, 4, 12):
                cell.alignment = Alignment(horizontal="center")
            if c == 12 and d.review_status == "Open":
                cell.font = ALERT_FONT
        r3 += 1

    ws3.freeze_panes = 'A2'
    ws3.auto_filter.ref = ws3.dimensions
    autofit_columns(ws3)

    # -------------------------------------------------------------
    # SHEET 4: Outstanding Devices
    # -------------------------------------------------------------
    ws4 = wb.create_sheet(title="Outstanding Devices")
    ws4.views.sheetView[0].showGridLines = True

    headers4 = [
        "Device Serial", "Model", "Assigned Worker ID", "Worker Name",
        "Department", "Issued Shift", "Operational Date", "Issue Time",
        "Issued By Operator", "Current Device Status"
    ]
    ws4.append(headers4)
    style_header_row(ws4, 1, NAVY_HEADER_FILL)

    # Devices currently in 'Issued' state with active assignment
    active_assignments = db.query(DeviceAssignment).filter(DeviceAssignment.status == "ACTIVE").all()

    r4 = 2
    for asgn in active_assignments:
        dev = db.query(Device).filter(Device.serial == asgn.device_serial).first()
        emp = db.query(Employee).filter(Employee.employee_id == asgn.employee_id).first()

        ws4.append([
            asgn.device_serial,
            dev.model if dev else "HHD",
            asgn.employee_id,
            emp.name if emp else "Unknown",
            emp.department if emp else "Unknown",
            asgn.issued_shift,
            asgn.issued_operational_date,
            asgn.issued_at.strftime("%Y-%m-%d %H:%M:%S") if asgn.issued_at else "",
            asgn.issued_by_operator_name,
            dev.status if dev else "Issued"
        ])
        for c in range(1, len(headers4) + 1):
            cell = ws4.cell(row=r4, column=c)
            cell.font = REGULAR_FONT
            cell.border = THIN_BORDER
            if c in (3, 6, 7, 10):
                cell.alignment = Alignment(horizontal="center")
        r4 += 1

    ws4.freeze_panes = 'A2'
    ws4.auto_filter.ref = ws4.dimensions
    autofit_columns(ws4)

    # -------------------------------------------------------------
    # SHEET 5: Shift Summary
    # -------------------------------------------------------------
    ws5 = wb.create_sheet(title="Shift Summary")
    ws5.views.sheetView[0].showGridLines = True

    headers5 = [
        "Operational Date", "Shift Name", "Total Issued", "Total Returned",
        "Working Returned", "Damaged Returned", "Discrepancy (Unreturned)", "Shift Status"
    ]
    ws5.append(headers5)
    style_header_row(ws5, 1, STEEL_HEADER_FILL)

    # Group by operational date and shift
    shift_txs_q = db.query(
        Transaction.operational_date,
        Transaction.shift_name,
        func.sum(case((Transaction.transaction_type == 'Issue', 1), else_=0)).label("issued"),
        func.sum(case((Transaction.transaction_type == 'Return', 1), else_=0)).label("returned"),
        func.sum(case(((Transaction.transaction_type == 'Return') & (Transaction.condition == 'Working'), 1), else_=0)).label("working"),
        func.sum(case(((Transaction.transaction_type == 'Return') & (Transaction.condition != 'Working'), 1), else_=0)).label("damaged")
    )
    if start_date:
        shift_txs_q = shift_txs_q.filter(Transaction.operational_date >= start_date)
    if end_date:
        shift_txs_q = shift_txs_q.filter(Transaction.operational_date <= end_date)
    if shift_name and shift_name != "All":
        shift_txs_q = shift_txs_q.filter(Transaction.shift_name == shift_name)

    shift_stats = shift_txs_q.group_by(Transaction.operational_date, Transaction.shift_name).order_by(
        Transaction.operational_date.desc(), Transaction.shift_name.asc()
    ).all()

    r5 = 2
    for s in shift_stats:
        issued = s.issued or 0
        returned = s.returned or 0
        working = s.working or 0
        damaged = s.damaged or 0
        discrepancy = issued - returned

        # Check shift log status
        log = db.query(ShiftLog).filter(
            ShiftLog.operational_date == s.operational_date,
            ShiftLog.shift_name == s.shift_name
        ).first()
        status_label = "CLOSED" if (log and log.status == "CLOSED") else "OPEN"

        ws5.append([
            s.operational_date,
            s.shift_name,
            issued,
            returned,
            working,
            damaged,
            discrepancy,
            status_label
        ])
        for c in range(1, len(headers5) + 1):
            cell = ws5.cell(row=r5, column=c)
            cell.font = REGULAR_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(horizontal="center" if c in (1, 2, 8) else "right")
            if c == 7 and discrepancy > 0:
                cell.font = ALERT_FONT
        r5 += 1

    ws5.freeze_panes = 'A2'
    ws5.auto_filter.ref = ws5.dimensions
    autofit_columns(ws5)

    # Save to buffer
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
