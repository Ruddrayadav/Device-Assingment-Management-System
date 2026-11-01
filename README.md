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
    1. **Overall Summary**: Key device KPIs, active allocations, condition breakdown, and device usage.
    2. **Transaction Details**: Continuous chronological log of every scan (Date, 12-Hour Time, Type, Serial, Make/Model, Worker ID, Worker Name, Department, Operator Name, Condition, Notes).
    3. **Device Inventory**: Complete device inventory master (Make, Model, Serial, Department, Status, Held Worker, Condition, Inventory Date, Remarks).
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
    Database Location:   C:\HHD_Server\data\hhd_production.db
    Local Access:        http://127.0.0.1:8000
    LAN Access (PCs):    http://192.168.1.50:8000
    Wall Display Board:  http://192.168.1.50:8000/board
    Excel Master Import: http://192.168.1.50:8000/admin/import
    Security Status:     Administrator configured · Setup endpoint locked
   ====================================================================
   ```

### Step 3: First-Time Administrator Setup & Connect Operators
1. If this is a fresh installation, navigate to `http://127.0.0.1:8000` or `http://127.0.0.1:8000/setup` to configure your primary administrator account with a strong password (or run `python setup_admin.py` in the server terminal).
2. Log in to `/admin` using the administrator credentials.
3. In **Users** (`/admin/users`), create individual accounts for each warehouse operator (e.g., Rahul, Amit, Suresh) with individual usernames and secure passwords.
4. On Operator workstation PCs, open Google Chrome or Microsoft Edge, navigate to `http://192.168.1.50:8000`, and sign in with individual operator accounts.

### Step 4: Import Real Company Inventory
1. Log in to `/admin` with your administrator account.
2. (Optional) Visit `/admin/reset` to wipe demo test devices and test employees.
3. Visit `/admin/import` to upload your company's `Device Inventory` and `Employee Master` `.xlsx` or `.csv` files.
4. Review the interactive preview and click **Commit Import**.

---

## 4. Administrator Setup & Operator Accounts

### Secure First-Time Administrator Setup
When the application is launched on a newly installed system:
1. Open `http://127.0.0.1:8000` (or `http://127.0.0.1:8000/setup`) in your browser directly on the host server.
2. The First-Time Setup Wizard prompts you to create the company's primary Administrator account:
   - Administrator Full Name
   - Unique Username (3–64 characters)
   - Master Password (minimum 8 characters, password strength validation)
   - Password Confirmation
3. The first Administrator automatically receives the `admin` role.
4. Remote LAN workstations are automatically held with an informational status screen until the host administrator completes setup. Remote attempts to access `/setup` directly are rejected with HTTP 403 Forbidden.
5. Once completed, the `/setup` endpoint is **permanently disabled and locked**. Setup will never reopen, even if an administrator account is deactivated or locked.

### Administrator Recovery & CLI Utility
If an administrator forgets their password or needs console account recovery:
```bash
# List all configured administrator accounts
python setup_admin.py --list

# Reset password and activate an administrator
python setup_admin.py --reset <username>

# Clear rate-limiting lockout for an account
python setup_admin.py --unlock <username>

# Interactive administrator creation
python setup_admin.py
```

### Legacy Default Accounts Advisory
If upgrading from older versions where accounts like `hhdadmin`, `operator1`, or `operator2` existed, they are preserved intact without data loss. An advisory banner is displayed in the Admin Dashboard and User Management (`/admin/users`) encouraging administrators to review and update their passwords.

### Individual Operator Accounts
- Operators must log in with their own individual accounts. Shared administrator accounts are never used for scan operations.
- Administrators create and manage operator accounts at `/admin/users`.
- Every scan transaction (Issue, Return, Damage Report, Scan Rejection) permanently records the authenticated operator's ID and name for 100% accountability.

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
