import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
HEADER_FONT = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
TEXT_FONT = Font(name="Segoe UI", size=10)
TITLE_FONT = Font(name="Segoe UI", size=14, bold=True, color="1E293B")
SUBTITLE_FONT = Font(name="Segoe UI", size=10, italic=True, color="64748B")

THIN_BORDER = Border(
    left=Side(style="thin", color="CBD5E1"),
    right=Side(style="thin", color="CBD5E1"),
    top=Side(style="thin", color="CBD5E1"),
    bottom=Side(style="thin", color="CBD5E1")
)

def generate_device_inventory_template() -> io.BytesIO:
    """
    Generates professionally styled Device_Inventory_Template.xlsx:
    Columns: Sr.No., Make, Model No, Serial Number, Department, Remarks, Date
    600 blank data rows, Serial Number formatted as TEXT, freeze header, auto-filter,
    Instructions sheet, and dropdown validation for Remarks (Working, Damaged, Under Repair).
    """
    wb = Workbook()

    # 1. Main Data Sheet
    ws = wb.active
    ws.title = "Device Inventory"
    ws.views.sheetView[0].showGridLines = True

    headers = ["Sr.No.", "Make", "Model No", "Serial Number", "Department", "Remarks", "Date"]
    ws.append(headers)

    # Style Header
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[1].height = 28

    # Add 600 blank data rows (rows 2 to 601)
    for r in range(2, 602):
        ws.row_dimensions[r].height = 20
        # Sr.No.
        c_sr = ws.cell(row=r, column=1, value=r - 1)
        c_sr.alignment = Alignment(horizontal="center", vertical="center")
        c_sr.font = TEXT_FONT
        c_sr.border = THIN_BORDER

        # Make, Model, Serial, Department, Remarks, Date
        for col_idx in range(2, 8):
            cell = ws.cell(row=r, column=col_idx)
            cell.font = TEXT_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center")
            # Serial Number is Column 4 -> Explicit TEXT format
            if col_idx == 4:
                cell.number_format = "@"
                cell.alignment = Alignment(horizontal="left", vertical="center")
            elif col_idx == 7:
                cell.number_format = "@"
                cell.alignment = Alignment(horizontal="center", vertical="center")

    # Dropdown Data Validation for Remarks (Working, Damaged, Under Repair)
    dv = DataValidation(
        type="list",
        formula1='"Working,Damaged,Under Repair"',
        allow_blank=True,
        showDropDown=True
    )
    dv.error = "Please select a valid condition: Working, Damaged, or Under Repair."
    dv.errorTitle = "Invalid Condition"
    ws.add_data_validation(dv)
    dv.add("F2:F601")

    # Freeze panes on A2
    ws.freeze_panes = "A2"

    # Auto-filter
    ws.auto_filter.ref = "A1:G601"

    # Column widths
    col_widths = {
        "A": 10,  # Sr.No.
        "B": 18,  # Make
        "C": 18,  # Model No
        "D": 26,  # Serial Number
        "E": 20,  # Department
        "F": 20,  # Remarks
        "G": 16   # Date
    }
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    # 2. Instructions Sheet
    ws_inst = wb.create_sheet(title="Instructions")
    ws_inst.views.sheetView[0].showGridLines = True
    ws_inst.column_dimensions["A"].width = 5
    ws_inst.column_dimensions["B"].width = 28
    ws_inst.column_dimensions["C"].width = 65

    ws_inst["B2"] = "HHD Device Inventory Excel Import Guide"
    ws_inst["B2"].font = TITLE_FONT
    ws_inst["B3"] = "Follow these instructions to populate your warehouse device master data."
    ws_inst["B3"].font = SUBTITLE_FONT

    instructions = [
        ("Column", "Requirement / Description"),
        ("Sr.No.", "Sequential reference number (1, 2, 3...)"),
        ("Make", "Manufacturer name, e.g. CHAINWAY, ZEBRA, HONEYWELL"),
        ("Model No", "Device model identifier, e.g. MC95, TC22, C66"),
        ("Serial Number", "REQUIRED & UNIQUE. Scanned device barcode. Stored as TEXT (supports leading zeros, lowercase, uppercase, letters, dashes)."),
        ("Department", "Device home warehouse department, e.g. INV MTS, IB Puter, Dispatch"),
        ("Remarks", "Initial device operational condition: Working, Damaged, or Under Repair (use dropdown)."),
        ("Date", "Inventory audit date, e.g. 28-Oct-26 or 2026-10-28.")
    ]

    for idx, (col_name, desc) in enumerate(instructions, start=5):
        cell_b = ws_inst.cell(row=idx, column=2, value=col_name)
        cell_c = ws_inst.cell(row=idx, column=3, value=desc)
        if idx == 5:
            cell_b.font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
            cell_b.fill = HEADER_FILL
            cell_c.font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
            cell_c.fill = HEADER_FILL
        else:
            cell_b.font = Font(name="Segoe UI", size=10, bold=True)
            cell_c.font = TEXT_FONT
            cell_b.border = THIN_BORDER
            cell_c.border = THIN_BORDER

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output

def generate_employee_master_template() -> io.BytesIO:
    """
    Generates professionally styled Employee_Master_Template.xlsx:
    Columns: Sr.No., Employee ID, Employee Name, Department, Status
    600 blank data rows, Employee ID formatted as TEXT, freeze header, auto-filter,
    Instructions sheet, and dropdown validation for Status (Active, Inactive).
    """
    wb = Workbook()

    # 1. Main Data Sheet
    ws = wb.active
    ws.title = "Employee Master"
    ws.views.sheetView[0].showGridLines = True

    headers = ["Sr.No.", "Employee ID", "Employee Name", "Department", "Status"]
    ws.append(headers)

    # Style Header
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[1].height = 28

    # Add 600 blank data rows (rows 2 to 601)
    for r in range(2, 602):
        ws.row_dimensions[r].height = 20
        # Sr.No.
        c_sr = ws.cell(row=r, column=1, value=r - 1)
        c_sr.alignment = Alignment(horizontal="center", vertical="center")
        c_sr.font = TEXT_FONT
        c_sr.border = THIN_BORDER

        for col_idx in range(2, 6):
            cell = ws.cell(row=r, column=col_idx)
            cell.font = TEXT_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center")
            # Employee ID is Column 2 -> Explicit TEXT format
            if col_idx == 2:
                cell.number_format = "@"
                cell.alignment = Alignment(horizontal="left", vertical="center")
            elif col_idx == 5:
                cell.alignment = Alignment(horizontal="center", vertical="center")

    # Dropdown Data Validation for Status (Active, Inactive)
    dv = DataValidation(
        type="list",
        formula1='"Active,Inactive"',
        allow_blank=True,
        showDropDown=True
    )
    dv.error = "Please select a valid status: Active or Inactive."
    dv.errorTitle = "Invalid Status"
    ws.add_data_validation(dv)
    dv.add("E2:E601")

    # Freeze panes on A2
    ws.freeze_panes = "A2"

    # Auto-filter
    ws.auto_filter.ref = "A1:E601"

    # Column widths
    col_widths = {
        "A": 10,  # Sr.No.
        "B": 22,  # Employee ID
        "C": 30,  # Employee Name
        "D": 24,  # Department
        "E": 16   # Status
    }
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    # 2. Instructions Sheet
    ws_inst = wb.create_sheet(title="Instructions")
    ws_inst.views.sheetView[0].showGridLines = True
    ws_inst.column_dimensions["A"].width = 5
    ws_inst.column_dimensions["B"].width = 24
    ws_inst.column_dimensions["C"].width = 65

    ws_inst["B2"] = "Employee Master Excel Import Guide"
    ws_inst["B2"].font = TITLE_FONT
    ws_inst["B3"] = "Follow these instructions to populate your company employee master data."
    ws_inst["B3"].font = SUBTITLE_FONT

    instructions = [
        ("Column", "Requirement / Description"),
        ("Sr.No.", "Sequential reference number (1, 2, 3...)"),
        ("Employee ID", "REQUIRED & UNIQUE. Scanned worker badge barcode or employee ID. Stored strictly as TEXT (leading zeros and letters preserved)."),
        ("Employee Name", "Full name of the worker / employee."),
        ("Department", "Warehouse department, e.g. INV MTS, IB Puter, Dispatch, RTO"),
        ("Status", "Operational status: Active (can take devices) or Inactive (blocked from device allocation).")
    ]

    for idx, (col_name, desc) in enumerate(instructions, start=5):
        cell_b = ws_inst.cell(row=idx, column=2, value=col_name)
        cell_c = ws_inst.cell(row=idx, column=3, value=desc)
        if idx == 5:
            cell_b.font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
            cell_b.fill = HEADER_FILL
            cell_c.font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
            cell_c.fill = HEADER_FILL
        else:
            cell_b.font = Font(name="Segoe UI", size=10, bold=True)
            cell_c.font = TEXT_FONT
            cell_b.border = THIN_BORDER
            cell_c.border = THIN_BORDER

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
