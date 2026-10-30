# HHD Device Management System — Version 3.0
**High-Speed, 24/7 Continuous Barcode Device Allocation & Handover Management System**

A robust, locally hosted, zero-delay warehouse device allocation and return system designed for enterprise warehouse operations. Built for continuous 24/7 warehouse queues processing hundreds of employees and thousands of device transactions daily with multiple operators scanning simultaneously on the local network.

---

## 1. Version 3.0 Architectural & Operational Highlights

- **24/7 Continuous Operations (Zero Shift System)**:
  - The warehouse operates continuously 24 hours a day, 7 days a week.
  - Eliminated arbitrary shift boundaries, shift reconciliations, and midnight operational shift mapping.
  - Every transaction is recorded with exact server clock timestamps and calendar dates.

- **Individual Operator Identity & Accountability**:
  - Operators log in using their own individual accounts (e.g., *Rahul*, *Amit*, *Suresh*).
  - Every Issue, Return, Damage Report, and Audit entry permanently records:
    - Operator account ID
    - Operator full name
    - Exact millisecond timestamp
  - Administrators can filter all transactions, audit logs, and reports by specific operator name.
  - Independent browser sessions allow two or more operators to scan side-by-side without crosstalk.

- **Excel Master Import Wizard (`/admin/import`)**:
  - Dedicated import wizard for **Device Inventory** and **Employee Master** records.
  - Supports both `.xlsx` (Excel) and `.csv` files.
  - **Exact Company Device Format Supported**:
    | Sr.No. | Make | Model No | Serial Number | Department | Remarks | Date |
    |---|---|---|---|---|---|---|
    | 1 | CHAINWAY | MC95 | 2288 | INV MTS | Working | 28-Oct-26 |
    | 2 | ZEBRA | TC22 | s25119524701038 | INV MTS | Working | 28-Oct-26 |
  - **Exact Company Employee Master Format Supported**:
    | Sr.No. | Employee ID | Employee Name | Department | Status |
    |---|---|---|---|---|
    | 1 | 1282781 | Bhupender | RTO Puter | Active |
    | 2 | 1222905 | Ajay Kumar | IB Puter | Active |
  - **Strict Text Preservation**: Device serials and employee IDs are imported strictly as strings to preserve lowercase letters (e.g. `s25119524701038`), symbols, and leading zeros (e.g. `001234`).
  - **Interactive Preview & Rejection Explanations**: File analysis identifies missing required columns, validates values, flags duplicate entries within the file, and classifies records as `NEW` or `UPDATE` before committing.
  - **UPSERT Mode**: Adds new records and updates existing records. **Never resets currently issued devices to Available**, preserving operational continuity.
  - **REPLACE Mode**: Replaces existing master list. Includes safety locks: **blocks execution if any devices are currently issued**, and automatically takes an online safety backup before execution.
  - **Downloadable Sample Templates**: 600-row pre-formatted Excel templates with locked header rows, auto-filters, text-formatted serial columns, and instructions.

- **Controlled Initial Data Reset (`/admin/reset`)**:
  - Controlled procedure to wipe sample demo devices and dummy test records so companies can import their real master spreadsheets fresh.
  - Multi-tier safety lock: requires admin password verification and exact phrase typing (`CONFIRM RESET`).
  - Automatically takes a verified safety backup (`hhd_backup_<timestamp>_pre_initial_data_reset.db`) before wiping.
  - **Strictly preserves administrator accounts, operator credentials, and system security configurations**.

- **Professional 7-Sheet 24/7 Formatted Excel Report (`/reports/excel`)**:
  - Automatically generated multi-tab `.xlsx` workbook:
    1. **Overall Summary**: Key fleet KPIs, active allocations, condition breakdown, and fleet utilization.
    2. **Transaction Details**: Continuous chronological log of every scan (Date, 12-Hour Time, Type, Serial, Make/Model, Worker ID, Worker Name, Department, Operator Name, Condition, Notes).
    3. **Device Inventory**: Complete device fleet master (Make, Model, Serial, Department, Status, Held Worker, Condition, Inventory Date, Remarks).
    4. **Employee Master**: Complete workforce master with held device counts and active status.
    5. **Damage and Repair Reports**: Fault categories, reporter, review status, and repair notes.
    6. **Outstanding Devices**: Active unreturned devices with holding employee details, department, and exact issue timestamp.
    7. **Operator Activity Summary**: Accountability matrix tracking transactions, issues done, returns handled, and damages logged per operator.

- **Universal 12-Hour AM/PM Time Presentation**:
  - Standardized user-facing time format across all screens, tables, live boards, and exported reports: `hh:mm:ss AM/PM` (e.g., `03:45:12 PM`) and `YYYY-MM-DD hh:mm:ss AM/PM`.
  - Windows-compatible cross-platform formatting without unsupported strftime flags.

- **Database Files Admin Center (`/admin/backups`)**:
  - Real-time live database health card: file size, WAL size, last scan time, and live record counts.
  - Full inspection of backup files in `backups/` using read-only immutable SQLite handles without lock contention.
  - Secure chunked upload of `.db` snapshots with magic header validation and schema integrity check.

- **Continuous Zero-Enter Barcode Scanning & SQLite Concurrency**:
  - Operators **never** press Enter or submit buttons. Scans process in ~1.2 milliseconds.
  - Concurrency serialized with thread-safe re-entrant lock (`SCAN_WRITE_LOCK`) and SQLite WAL mode (`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=20000`).

---

## 2. System Architecture

```
                    ┌───────────────────────────────┐
                    │      Windows Server PC        │
                    │   FastAPI + SQLite WAL DB     │
                    │      (Port: 8000)             │
                    └──────────────┬────────────────┘
                                   │ LAN (Wi-Fi / Ethernet)
            ┌──────────────────────┴──────────────────────┐
            ▼                                             ▼
┌──────────────────────────────┐              ┌──────────────────────────────┐
│  Operator 1 PC (Rahul)       │              │  Operator 2 PC (Amit)        │
│  USB HID Barcode Scanner     │              │  USB HID Barcode Scanner     │
│  http://192.168.1.50:8000    │              │  http://192.168.1.50:8000    │
└──────────────────────────────┘              └──────────────────────────────┘
```

- **Backend**: Python 3.10+, FastAPI, SQLAlchemy 2.0, Pydantic, openpyxl, PBKDF2-HMAC-SHA256.
- **Database**: SQLite in WAL mode.
- **Client Computers**: Access through Chrome or Edge via the server's LAN IP. Clients never access the `.db` file directly.

---

## 3. Quick Start (Windows Deployment)

### Step 1: Install
1. Copy the application folder to `C:\HHD_Manager` on the Windows Server computer.
2. Double-click `INSTALL.bat`.
   - Creates an isolated `.venv` virtual environment.
   - Installs required dependencies.
   - Automatically migrates existing schemas and creates tables.

### Step 2: Start Server
1. Double-click `Start.bat`.
2. The terminal displays the server LAN IP and access URLs:
   ```
   ====================================================================
    HHD DEVICE MANAGEMENT SYSTEM — VERSION 3.0 (24/7 CONTINUOUS)
   ====================================================================
    Local Access:        http://127.0.0.1:8000
    LAN Access (PCs):    http://192.168.1.50:8000
    Wall Display Board:  http://192.168.1.50:8000/board
    Excel Master Import: http://192.168.1.50:8000/admin/import
    Admin Credentials:   Username: hhdadmin / Password: admin12345
    Operator 1 Login:    Username: operator1 / Password: operator123
    Operator 2 Login:    Username: operator2 / Password: operator123
   ====================================================================
   ```

### Step 3: Connect Operator PCs
1. On Operator PC 1 and Operator PC 2, open Google Chrome or Microsoft Edge.
2. Enter the server's LAN address (e.g. `http://192.168.1.50:8000`).
3. Sign in with individual operator accounts.

### Step 4: Import Real Company Inventory
1. Log in to `/admin` as `hhdadmin`.
2. (Optional) Visit `/admin/reset` to wipe demo test devices and test employees.
3. Visit `/admin/import` to upload your company's `Device Inventory` and `Employee Master` `.xlsx` or `.csv` files.
4. Review the interactive preview and click **Commit Import**.

---

## 4. Default Credentials

| Role | Username | Password | Full Name | Access Level |
| :--- | :--- | :--- | :--- | :--- |
| **Administrator** | `hhdadmin` | `admin12345` | Warehouse Administrator | Full management, imports, resets, reports, backups |
| **Operator 1** | `operator1` | `operator123` | Rahul (Operator 1) | Workstation scan station (Issue / Return) |
| **Operator 2** | `operator2` | `operator123` | Amit (Operator 2) | Workstation scan station (Issue / Return) |

*Admins can create additional operator accounts and reset passwords in **Admin -> Users**.*

---

## 5. Keyboard Shortcuts on Operator Workstation

- **F2**: Switch to **Issue Mode** (Scan Worker ID + Device).
- **F4**: Switch to **Return Mode** (Scan ONLY Device barcode).
- **Esc**: Clear pending scan state / reset scan box.

---

## 6. Running the Automated Test Suite

To verify all 43 unit, concurrency, import validation, 12-hour time formatting, and latency tests:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Benchmark performance verified:
- **100 consecutive scans completed in ~0.119 seconds (1.19ms per scan)**.
- **Race condition test verified**: Concurrent operators issuing and returning devices simultaneously are cleanly serialized with zero duplicate allocations.
- **Import validation verified**: Text serial preservation, leading zeros, duplicate detection, UPSERT non-reset of issued devices, and REPLACE active device safety block.
