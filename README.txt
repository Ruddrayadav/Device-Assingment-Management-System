HHD HANDOVER / TAKEOVER MANAGER — WINDOWS SERVER STARTER
========================================================

What this is
------------
A small Flask web application backed by SQLite. It is designed for a Windows Server on the warehouse LAN and 2–10 concurrent operators using Edge/Chrome and USB/Bluetooth barcode scanners. The server holds the single database; client PCs only use a browser.

Features
--------
- Login accounts, admin/operator roles and password hashing
- Device and employee master records (admin-managed)
- USB scanner workflow: device serial + employee ID
- Handover and takeover validation to prevent duplicate issue/incorrect return
- Transaction audit history with operator, date/time, condition and remarks
- Damage/missing/under-repair issue log and review status
- CSV exports for Excel
- On-demand SQLite-consistent database backups
- Seeds the 10 sample employee/device records from the supplied list

WHAT'S NEW IN THIS VERSION
--------------------------
Scan desk (home page)
- One scan field, always focused. No dropdowns, no page reloads.
- Scan a DEVICE ALONE  -> it is taken back from whoever holds it (one scan).
- Scan DEVICE then EMPLOYEE ID (or employee then device) -> handed over (two scans).
- Beep on success / error. Esc cancels. Typing the last 4+ digits of a serial also works.
- Click a red tile to take back as Damaged / Missing / Under repair. "Report a problem" appears after each return.
- Unknown barcode: admins get an "Add as device" button that continues straight into the handover.
Live board
- Green dot = Available, red dot = Allotted. Updates for everyone every 3 seconds.
- "Not handed over today" lists devices with no handover today: devices still with staff from earlier days (shown first) and idle stock.
- /board is a read-only display page (no login) for a wall screen or any PC. Set HHD_PUBLIC_BOARD=0 to require login.
Logic fixes
- A Damaged/Missing/Under Repair device can no longer be handed over.
- Closing the last open issue returns the device to Available.
- Reporting damage on an issued device no longer blocks its return.
- "Accessory Missing" is logged without taking the device out of service.
- Login redirect cannot be abused; secret key is saved in data\.secret_key so restarts keep users signed in.
- Employees can be activated/deactivated. Waitress server is used when installed (INSTALL.bat installs it).
Existing databases work unchanged (only indexes are added).

Requirements
------------
- Windows Server / Windows 10/11 PC
- Python 3.10+ installed from python.org (enable PATH)
- LAN access from operator PCs
- Reserve a static IP/DNS name for the server if practical

INSTALLATION
------------
1. Copy/extract this folder to e.g. C:\HHDManager on the Windows Server.
2. Run INSTALL.bat as a Windows user allowed to install Python packages.
3. Create the first admin username and password when prompted. Use a strong unique password.
4. Run RUN.bat. The console should say it is listening on port 8080.
5. On the server, open http://127.0.0.1:8080 and sign in.
6. Find the server's LAN IP using `ipconfig`.
7. In Windows Defender Firewall, create an inbound TCP rule for port 8080 restricted to the warehouse LAN/subnet only.
8. From each operator PC, open http://SERVER-LAN-IP:8080 in Edge/Chrome and sign in.
9. In Users, create individual accounts for each operator. Do not share accounts.
10. Add/import remaining inventory and employee records manually through master pages (CSV export is included; CSV import is not in this starter).

BARCODE SCANNER
---------------
Most USB scanners work as a USB HID keyboard. Connect it to the PC and configure the scanner suffix to Enter/CR if available. The scan desk keeps focus automatically, so just scan: device, then employee ID. If a scanner uses a different interface, switch it to HID keyboard mode per scanner manufacturer instructions.

DATABASE AND BACKUPS
--------------------
Database: data\hhd_inventory.db
Backups: backups\
Use Reports / Backup -> Run backup now. Also configure Windows Task Scheduler to back up the entire `backups` folder to approved network storage. Retain backups per company policy. Test a restore procedure periodically. Do not copy only the .db file while the application is writing; use the built-in backup button or stop the application first.

IMPORTANT SECURITY / OPERATIONS
-------------------------------
- This starter uses HTTP, suitable only for a trusted isolated warehouse LAN. Do not expose it to the internet. For sensitive production use, put it behind HTTPS (IIS reverse proxy or another approved reverse proxy), use a strong secret key, Windows Server hardening, and backups.
- Set HHD_SECRET_KEY to a persistent random secret before production. If not set, a random key is created each start, which logs users out when the app restarts.
  PowerShell example (for the current user): [Environment]::SetEnvironmentVariable("HHD_SECRET_KEY","paste-a-long-random-secret-here","User")
  Restart the app after setting it.
- Restrict the server folder permissions to the service/admin accounts. Protect backups because they contain employee/device data.
- Run this as a dedicated, non-administrator Windows account for routine use.
- This is a starter application, not a fully hardened enterprise system. Validate with IT/security before production use.
- SQLite is suitable for a small warehouse workload, but all users must access it through this web app. Never place the live SQLite database on a network share and let each PC open it directly.
- For 2–10 users, SQLite WAL and a busy timeout are configured. If transaction volume or concurrency grows substantially, migrate to SQL Server/PostgreSQL.

First-time seeded records
-------------------------
The application seeds the 10 employee and 10 Chainway device records from the supplied list when the database is first initialized. All devices start as Available. Confirm physical inventory before using live transactions.

Troubleshooting
---------------
- Port in use: set HHD_PORT to another port before starting.
- Other PCs cannot connect: check `ipconfig`, server network profile, Windows Firewall inbound TCP rule and client/server VLAN routing.
- Scanner not entering text: use scanner configuration barcode to set USB HID keyboard mode.
- Forgot password: ask the system administrator to reset it using an approved maintenance process; no public password reset route is provided.
