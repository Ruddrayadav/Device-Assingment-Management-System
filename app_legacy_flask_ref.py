import os, io, csv, re, sqlite3, secrets
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (Flask, request, redirect, url_for, session, flash, render_template,send_file, abort, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "hhd_inventory.db")
# Anyone on the LAN can open /board (read-only wall display). Set HHD_PUBLIC_BOARD=0 to require login.
PUBLIC_BOARD = os.environ.get("HHD_PUBLIC_BOARD", "1") != "0"
CONDS = ("Working", "Damaged", "Missing", "Under Repair")


def _secret():
    s = os.environ.get("HHD_SECRET_KEY")
    if s:
        return s
    path = os.path.join(DATA_DIR, ".secret_key")      # persisted so restarts don't log everyone out
    try:
        s = open(path).read().strip()
        if s:
            return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(s)
    return s


app = Flask(__name__)
app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
                  SEND_FILE_MAX_AGE_DEFAULT=timedelta(hours=8))


def now():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def qall(sql, args=()):
    con = db()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def init_db():
    con = db()
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'operator',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS employees(
      employee_id TEXT PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS devices(
      serial TEXT PRIMARY KEY, make TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT 'HHD',
      cover_qty INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Available',
      assigned_employee_id TEXT, condition TEXT NOT NULL DEFAULT 'Working',
      notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(assigned_employee_id) REFERENCES employees(employee_id));
    CREATE TABLE IF NOT EXISTS transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_time TEXT NOT NULL,
      transaction_type TEXT NOT NULL, serial TEXT NOT NULL, employee_id TEXT NOT NULL,
      employee_name TEXT NOT NULL, department TEXT NOT NULL, operator TEXT NOT NULL,
      condition TEXT NOT NULL, cover_qty INTEGER NOT NULL DEFAULT 0, remarks TEXT NOT NULL DEFAULT '',
      FOREIGN KEY(serial) REFERENCES devices(serial), FOREIGN KEY(employee_id) REFERENCES employees(employee_id));
    CREATE TABLE IF NOT EXISTS damage_records(
      id INTEGER PRIMARY KEY AUTOINCREMENT, report_time TEXT NOT NULL, serial TEXT NOT NULL,
      employee_id TEXT, employee_name TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
      issue_type TEXT NOT NULL, details TEXT NOT NULL, reported_by TEXT NOT NULL,
      review_status TEXT NOT NULL DEFAULT 'Open', resolution TEXT NOT NULL DEFAULT '',
      FOREIGN KEY(serial) REFERENCES devices(serial));
    CREATE INDEX IF NOT EXISTS idx_tx_time ON transactions(transaction_time);
    CREATE INDEX IF NOT EXISTS idx_tx_serial ON transactions(serial);
    CREATE INDEX IF NOT EXISTS idx_tx_employee ON transactions(employee_id);
    CREATE INDEX IF NOT EXISTS idx_tx_serial_type ON transactions(serial, transaction_type, transaction_time);
    CREATE INDEX IF NOT EXISTS idx_dev_status ON devices(status);
    CREATE INDEX IF NOT EXISTS idx_dev_emp ON devices(assigned_employee_id);
    CREATE INDEX IF NOT EXISTS idx_dmg_serial ON damage_records(serial, review_status);
    """)
    if con.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        t = now()
        emps = [("1222905", "Ajay Kumar", "IB Puter"), ("1332772", "Sonu", "IB Puter"),
                ("1334506", "Rajkumar", "IB Receiver"), ("1331118", "Sunny", "RTO Puter"),
                ("1266377", "Ravinder", "RTO Associate"), ("1205341", "Sanjeev Kumar", "Dispatch Scanner"),
                ("1282781", "Bhupender", "RTO Puter"), ("1208400", "Sukhbir Singh", "Dispatch Scanner"),
                ("1192873", "Parmjeet", "Dispatch Scanner"), ("1163679", "Rajender", "Dispatch Scanner")]
        con.executemany("INSERT INTO employees(employee_id,name,department,created_at,updated_at) VALUES(?,?,?,?,?)",
                        [(a, b, c, t, t) for a, b, c in emps])
        serials = ["HC660DE260502008", "HC660DE260501682", "HC660DE260504219", "HC660DE260501783",
                   "HC660DE260501876", "HC660DE260501990", "HC660DE260501274", "HC660DE260401747",
                   "HC660DE260501998", "HC660DE260501980"]
        con.executemany("""INSERT INTO devices(serial,make,model,cover_qty,status,condition,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?)""", [(s, "CHAINWAY", "HHD", 2, "Available", "Working", t, t) for s in serials])
    con.commit()
    con.close()


# ---------------------------------------------------------------- auth / csrf
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(ok=False, state="error", message="Session expired. Sign in again."), 401
            return redirect(url_for("login", next=request.path))
        return fn(*a, **k)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(ok=False, state="error", message="Session expired. Sign in again."), 401
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify(ok=False, state="error", message="Administrator access is required."), 403
            flash("Administrator access is required.", "error")
            return redirect(url_for("dashboard"))
        return fn(*a, **k)
    return wrapper


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def asset(name):
    p = os.path.join(BASE_DIR, "static", name)
    try:
        return url_for("static", filename=name, v=int(os.path.getmtime(p)))
    except OSError:
        return url_for("static", filename=name)


app.jinja_env.globals["asset"] = asset


@app.before_request
def protect_post():
    if request.method == "POST":
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not token or token != session.get("_csrf"):
            if request.path.startswith("/api/"):
                return jsonify(ok=False, state="error", message="Security token expired. Refresh the page."), 400
            abort(400, "Security token missing or expired. Refresh the page and try again.")


@app.after_request
def no_cache_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def safe_next(n):
    return n if n and n.startswith("/") and not n.startswith("//") and "\\" not in n else None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        rows = qall("SELECT * FROM users WHERE username=? AND active=1", (request.form.get("username", "").strip(),))
        u = rows[0] if rows else None
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["user"], session["role"] = u["username"], u["role"]
            session["_csrf"] = secrets.token_urlsafe(24)
            return redirect(safe_next(request.args.get("next")) or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html", title="Sign in")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- core logic
class Fail(Exception):
    pass


def clean(s):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(s or "")).strip()


def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_device(con, code):
    r = con.execute("SELECT * FROM devices WHERE serial=? COLLATE NOCASE", (code,)).fetchone()
    if r or len(code) < 4:
        return r
    rows = con.execute("SELECT * FROM devices WHERE serial LIKE ? ESCAPE '\\' COLLATE NOCASE LIMIT 2",
                       ("%" + like_escape(code),)).fetchall()      # typed last digits, if unique
    return rows[0] if len(rows) == 1 else None


def find_employee(con, code):
    return con.execute("SELECT * FROM employees WHERE employee_id=? COLLATE NOCASE", (code,)).fetchone()


def holder_name(con, eid):
    r = con.execute("SELECT name FROM employees WHERE employee_id=?", (eid,)).fetchone()
    return r["name"] if r else str(eid)


def make_tx(typ, serial, eid=None, cond="Working", cover=None, remarks="", operator=""):
    """Atomic handover (issue) / takeover (return). Raises Fail with a user-readable message."""
    if typ not in ("Handover", "Takeover") or cond not in CONDS:
        raise Fail("Invalid transaction values.")
    if typ == "Handover":
        cond = "Working"
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        d = con.execute("SELECT * FROM devices WHERE serial=?", (serial,)).fetchone()
        if not d:
            raise Fail("Device not found. Add it to Device Inventory first.")
        if typ == "Handover":
            e = con.execute("SELECT * FROM employees WHERE employee_id=?", (eid,)).fetchone()
            if not e:
                raise Fail("Employee ID not found. Add it in Employee Master.")
            if not e["active"]:
                raise Fail(f"{e['name']} is inactive. Activate the employee first.")
            if d["status"] == "Issued":
                raise Fail(f"Already with {holder_name(con, d['assigned_employee_id'])}. Scan the device alone to return it.")
            if d["status"] != "Available":
                raise Fail(f"Device is marked {d['status']} and cannot be issued. Close its issue in Damage / Missing first.")
        else:
            if d["status"] != "Issued":
                raise Fail("This device is not issued, so there is nothing to take back.")
            e = con.execute("SELECT * FROM employees WHERE employee_id=?", (d["assigned_employee_id"],)).fetchone()
            if not e:
                raise Fail("The assigned employee record is missing. Contact the administrator.")
            if eid and eid != e["employee_id"]:
                raise Fail(f"Device is with {e['name']}, not this employee. Scan the device alone to return it.")
        cover = d["cover_qty"] if cover is None else max(0, int(cover or 0))
        t = now()
        con.execute("""INSERT INTO transactions(transaction_time,transaction_type,serial,employee_id,employee_name,
                       department,operator,condition,cover_qty,remarks) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (t, typ, d["serial"], e["employee_id"], e["name"], e["department"], operator, cond, cover, remarks))
        if typ == "Handover":
            status, assigned = "Issued", e["employee_id"]
        else:
            status, assigned = ("Available" if cond == "Working" else cond), None
        con.execute("UPDATE devices SET status=?,assigned_employee_id=?,condition=?,cover_qty=?,updated_at=? WHERE serial=?",
                    (status, assigned, cond, cover, t, d["serial"]))
        if typ == "Takeover" and cond != "Working":
            con.execute("""INSERT INTO damage_records(report_time,serial,employee_id,employee_name,department,issue_type,details,reported_by)
                           VALUES(?,?,?,?,?,?,?,?)""", (t, d["serial"], e["employee_id"], e["name"], e["department"],
                                                        cond, remarks or "No details entered", operator))
        held = con.execute("SELECT COUNT(*) FROM devices WHERE assigned_employee_id=? AND status='Issued'",
                           (e["employee_id"],)).fetchone()[0]
        con.commit()
        return {"serial": d["serial"], "type": typ, "condition": cond, "held": held,
                "employee": {"id": e["employee_id"], "name": e["name"], "dept": e["department"]}}
    except Fail:
        con.rollback()
        raise
    except sqlite3.Error:
        con.rollback()
        raise Fail("Database busy. Nothing was saved. Scan again.")
    finally:
        con.close()


def log_issue(con, d, issue, details, user, eid=None):
    """Shared by the Damage page and the quick 'report a problem' button."""
    e = con.execute("SELECT * FROM employees WHERE employee_id=?", (eid or d["assigned_employee_id"] or "",)).fetchone()
    t = now()
    con.execute("""INSERT INTO damage_records(report_time,serial,employee_id,employee_name,department,issue_type,details,reported_by)
                   VALUES(?,?,?,?,?,?,?,?)""", (t, d["serial"], e["employee_id"] if e else None, e["name"] if e else "",
                                                e["department"] if e else "", issue, details or "No details entered", user))
    if issue == "Accessory Missing":
        con.execute("UPDATE devices SET updated_at=? WHERE serial=?", (t, d["serial"]))
    elif d["status"] == "Issued":          # keep it returnable; the return sets the final status
        con.execute("UPDATE devices SET condition=?,updated_at=? WHERE serial=?", (issue, t, d["serial"]))
    else:
        con.execute("UPDATE devices SET status=?,condition=?,updated_at=? WHERE serial=?", (issue, issue, t, d["serial"]))


# ---------------------------------------------------------------- board data
def board_data(con):
    today = date.today().isoformat()
    t0 = today + " 00:00:00"
    rows = con.execute("""SELECT d.serial,d.status,d.condition,d.cover_qty,d.assigned_employee_id eid,e.name ename,e.department dept,
        (SELECT MAX(transaction_time) FROM transactions t WHERE t.serial=d.serial AND t.transaction_type='Handover') last_ho
        FROM devices d LEFT JOIN employees e ON e.employee_id=d.assigned_employee_id ORDER BY d.serial""").fetchall()
    avail, allot, unavail, not_today = [], [], [], []
    for r in rows:
        it = {"serial": r["serial"], "status": r["status"], "eid": r["eid"], "ename": r["ename"], "dept": r["dept"],
              "last_ho": r["last_ho"]}
        if r["status"] == "Available":
            avail.append(it)
        elif r["status"] == "Issued":
            allot.append(it)
        else:
            unavail.append(it)
        if r["status"] in ("Available", "Issued") and (not r["last_ho"] or r["last_ho"] < t0):
            not_today.append(it)
    allot.sort(key=lambda i: i["last_ho"] or "", reverse=True)
    not_today.sort(key=lambda i: (i["status"] != "Issued", i["last_ho"] or ""))   # still-held first, then longest idle
    recent = [dict(time=r["transaction_time"], type=r["transaction_type"], serial=r["serial"], name=r["employee_name"],
                   cond=r["condition"], by=r["operator"]) for r in con.execute(
        "SELECT * FROM transactions ORDER BY id DESC LIMIT 14")]
    one = lambda sql, a=(): con.execute(sql, a).fetchone()[0]
    issues = one("SELECT COUNT(*) FROM damage_records WHERE review_status='Open'")
    last_tx = one("SELECT COALESCE(MAX(id),0) FROM transactions")
    last_dev = one("SELECT COALESCE(MAX(updated_at),'') FROM devices")
    ver = f"{last_tx}|{last_dev}|{issues}|{today}"
    return {"version": ver, "server_now": now(), "today": today,
            "available": avail, "allotted": allot, "unavailable": unavail, "not_today": not_today, "recent": recent,
            "counts": {"total": len(rows), "available": len(avail), "allotted": len(allot), "unavailable": len(unavail),
                       "not_today": len(not_today), "not_today_held": sum(1 for i in not_today if i["status"] == "Issued"),
                       "issues": issues,
                       "out_today": one("SELECT COUNT(*) FROM transactions WHERE transaction_type='Handover' AND transaction_time>=?", (t0,)),
                       "back_today": one("SELECT COUNT(*) FROM transactions WHERE transaction_type='Takeover' AND transaction_time>=?", (t0,))}}


def get_board():
    con = db()
    try:
        return board_data(con)
    finally:
        con.close()


def boot_payload(readonly):
    return {"board": get_board(), "csrf": csrf_token() if session.get("user") else "", "readonly": readonly,
            "admin": session.get("role") == "admin"}


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", title="Dashboard", boot=boot_payload(False), readonly=False)


@app.route("/board")
def board():
    if not (PUBLIC_BOARD or session.get("user")):
        return redirect(url_for("login", next="/board"))
    return render_template("dashboard.html", title="Device board", boot=boot_payload(True), readonly=True,
                           bare=not session.get("user"))


@app.route("/api/board")
def api_board():
    if not (PUBLIC_BOARD or session.get("user")):
        return jsonify(ok=False), 401
    b = get_board()
    if request.args.get("v") == b["version"]:
        return jsonify(changed=False)
    return jsonify(changed=True, board=b)


# ---------------------------------------------------------------- fast scan API
def reply(state, message, pending=None, title="", **kw):
    return jsonify(ok=state in ("done", "pending"), state=state, title=title, message=message, pending=pending, **kw)


@app.post("/api/scan")
@login_required
def api_scan():
    data = request.get_json(silent=True) or {}
    code = clean(data.get("code"))[:64]
    pend = data.get("pending") or {}
    ptype, pcode = pend.get("type"), clean(pend.get("code"))[:64]
    keep = {"type": ptype, "code": pcode} if ptype in ("device", "employee") and pcode else None
    if not code:
        return reply("error", "Nothing was scanned.", keep)
    user = session["user"]
    con = db()
    try:
        d, e = find_device(con, code), find_employee(con, code)
        if d and e:                              # same value in both tables: use the pending scan to decide
            if ptype == "employee":
                e = None
            else:
                d = None
        if not d and not e:
            return reply("unknown", f"“{code}” is not a registered device or employee.", keep, "Unknown barcode",
                         code=code, can_add=session.get("role") == "admin")
        held = lambda eid: con.execute("SELECT COUNT(*) FROM devices WHERE assigned_employee_id=? AND status='Issued'", (eid,)).fetchone()[0]

        def finish(typ, serial, eid):
            r = make_tx(typ, serial, eid, operator=user)
            if typ == "Handover":
                msg = f"{r['employee']['name']} · {r['employee']['dept']}" + (f" · now holds {r['held']} devices" if r["held"] > 1 else "")
                return reply("done", msg, None, "Handed over", action=typ, device=serial, board=get_board())
            return reply("done", f"from {r['employee']['name']} · {r['employee']['dept']}", None, "Returned",
                         action=typ, device=serial, board=get_board(), can_flag=True)

        if d:
            pe = find_employee(con, pcode) if ptype == "employee" and pcode else None
            if d["status"] == "Issued":                       # device alone = return
                return finish("Takeover", d["serial"], pe["employee_id"] if pe else None)
            if d["status"] != "Available":
                return reply("error", f"Device is marked {d['status']}. Close its issue in Damage / Missing before issuing it.",
                             keep, d["serial"][-8:] + " unavailable", device=d["serial"])
            if pe:
                return finish("Handover", d["serial"], pe["employee_id"])
            return reply("pending", "Now scan the employee ID.", {"type": "device", "code": d["serial"]},
                         "Device ready", device=d["serial"])
        if not e["active"]:
            return reply("error", f"{e['name']} is inactive. Activate the employee in Employee Master.", keep, "Employee inactive")
        pd = find_device(con, pcode) if ptype == "device" and pcode else None
        if pd:
            return finish("Handover", pd["serial"], e["employee_id"])
        n = held(e["employee_id"])
        return reply("pending", f"{e['department']} · holds {n} device{'s' if n != 1 else ''}. Now scan the device.",
                     {"type": "employee", "code": e["employee_id"]}, e["name"], employee=e["employee_id"])
    except Fail as ex:
        return reply("error", str(ex), None, "Not recorded")
    finally:
        con.close()


@app.post("/api/return")
@login_required
def api_return():
    j = request.get_json(silent=True) or {}
    try:
        r = make_tx("Takeover", clean(j.get("serial")), None, j.get("condition", "Working"), j.get("cover_qty"),
                    clean(j.get("remarks"))[:300], session["user"])
    except Fail as ex:
        return reply("error", str(ex), None, "Not recorded")
    msg = f"from {r['employee']['name']} · {r['employee']['dept']}"
    return reply("done", msg + ("" if r["condition"] == "Working" else f" · marked {r['condition']}"), None,
                 "Returned", action="Takeover", device=r["serial"], board=get_board())


@app.post("/api/flag")
@login_required
def api_flag():
    j = request.get_json(silent=True) or {}
    issue = j.get("issue_type")
    if issue not in ("Damaged", "Missing", "Under Repair", "Accessory Missing"):
        return reply("error", "Invalid issue type.")
    con = db()
    try:
        d = con.execute("SELECT * FROM devices WHERE serial=?", (clean(j.get("serial")),)).fetchone()
        if not d:
            return reply("error", "Device not found.")
        log_issue(con, d, issue, clean(j.get("details"))[:300], session["user"])
        con.commit()
    finally:
        con.close()
    return reply("done", f"{d['serial']} marked {issue}.", None, "Issue logged", action="Flag", device=d["serial"], board=get_board())


@app.post("/api/device/add")
@admin_required
def api_device_add():
    serial = clean((request.get_json(silent=True) or {}).get("serial"))[:64]
    if not serial:
        return reply("error", "Serial is required.")
    t = now()
    con = db()
    try:
        con.execute("INSERT INTO devices(serial,make,model,cover_qty,status,condition,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (serial, "CHAINWAY", "HHD", 2, "Available", "Working", t, t))
        con.commit()
    except sqlite3.IntegrityError:
        return reply("error", "That serial already exists.")
    finally:
        con.close()
    return reply("done", f"{serial} added to inventory.", None, "Device added", device=serial, board=get_board())


# ---------------------------------------------------------------- master data pages
@app.route("/devices", methods=["GET", "POST"])
@login_required
def devices():
    con = db()
    if request.method == "POST":
        if session.get("role") != "admin":
            con.close()
            abort(403)
        serial = clean(request.form.get("serial"))
        if not serial:
            flash("Serial number is required.", "error")
        else:
            t = now()
            try:
                con.execute("""INSERT INTO devices(serial,make,model,cover_qty,status,condition,created_at,updated_at)
                    VALUES(?,?,?,?, 'Available','Working',?,?) ON CONFLICT(serial) DO UPDATE SET
                    make=excluded.make,model=excluded.model,cover_qty=excluded.cover_qty,updated_at=excluded.updated_at""",
                            (serial, request.form.get("make", "").strip(), request.form.get("model", "HHD").strip(),
                             max(0, int(request.form.get("cover_qty", 0) or 0)), t, t))
                con.commit()
                flash("Device saved.")
            except (sqlite3.Error, ValueError):
                con.rollback()
                flash("Could not save device. Check serial and data.", "error")
    q = request.args.get("q", "").strip()
    rows = con.execute("""SELECT d.*,e.name employee_name,e.department FROM devices d LEFT JOIN employees e ON e.employee_id=d.assigned_employee_id
        WHERE d.serial LIKE ? OR d.make LIKE ? OR d.model LIKE ? OR d.status LIKE ? OR COALESCE(e.name,'') LIKE ?
        ORDER BY d.serial LIMIT 1000""", tuple([f"%{q}%"] * 5)).fetchall()
    con.close()
    return render_template("devices.html", title="Device Inventory", rows=rows, q=q)


@app.route("/employees", methods=["GET", "POST"])
@login_required
def employees():
    con = db()
    if request.method == "POST":
        if session.get("role") != "admin":
            con.close()
            abort(403)
        eid = clean(request.form.get("employee_id"))
        name, dept = request.form.get("name", "").strip(), request.form.get("department", "").strip()
        if not (eid and name and dept):
            flash("Employee ID, name and department are required.", "error")
        else:
            t = now()
            try:
                con.execute("""INSERT INTO employees(employee_id,name,department,created_at,updated_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(employee_id) DO UPDATE SET name=excluded.name,department=excluded.department,active=1,updated_at=excluded.updated_at""",
                            (eid, name, dept, t, t))
                con.commit()
                flash("Employee saved.")
            except sqlite3.Error:
                con.rollback()
                flash("Could not save employee.", "error")
    q = request.args.get("q", "").strip()
    rows = con.execute("""SELECT e.*, (SELECT group_concat(serial, ', ') FROM devices WHERE assigned_employee_id=e.employee_id AND status='Issued') devices
        FROM employees e WHERE e.employee_id LIKE ? OR e.name LIKE ? OR e.department LIKE ? ORDER BY e.name LIMIT 2000""",
                      (f"%{q}%",) * 3).fetchall()
    con.close()
    return render_template("employees.html", title="Employee Master", rows=rows, q=q, prefill=request.args.get("add", ""))


@app.post("/employees/<eid>/toggle")
@admin_required
def employee_toggle(eid):
    con = db()
    r = con.execute("SELECT active FROM employees WHERE employee_id=?", (eid,)).fetchone()
    if r:
        if r["active"] and con.execute("SELECT 1 FROM devices WHERE assigned_employee_id=? AND status='Issued'", (eid,)).fetchone():
            flash("This employee still holds a device. Take it back before deactivating.", "error")
        else:
            con.execute("UPDATE employees SET active=?,updated_at=? WHERE employee_id=?", (0 if r["active"] else 1, now(), eid))
            con.commit()
            flash("Employee updated.")
    con.close()
    return redirect(url_for("employees"))


@app.route("/transactions")
@login_required
def transactions():
    q, typ = request.args.get("q", "").strip(), request.args.get("type", "")
    rows = qall("""SELECT * FROM transactions WHERE
      (serial LIKE ? OR employee_id LIKE ? OR employee_name LIKE ? OR department LIKE ? OR operator LIKE ?)
      AND (?='' OR transaction_type=?) ORDER BY id DESC LIMIT 3000""", tuple([f"%{q}%"] * 5 + [typ, typ]))
    return render_template("transactions.html", title="Transaction Audit", rows=rows, q=q, typ=typ)


@app.route("/damage", methods=["GET", "POST"])
@login_required
def damage():
    con = db()
    if request.method == "POST":
        serial, eid = clean(request.form.get("serial")), clean(request.form.get("employee_id"))
        issue, details = request.form.get("issue_type", "Damaged"), request.form.get("details", "").strip()
        d = find_device(con, serial) if serial else None
        if not d or not details or issue not in ("Damaged", "Missing", "Under Repair", "Accessory Missing"):
            flash("A valid device serial and issue details are required.", "error")
        else:
            log_issue(con, d, issue, details, session["user"], eid or None)
            con.commit()
            flash("Issue recorded.")
    rows = con.execute("SELECT * FROM damage_records ORDER BY (review_status='Closed'), id DESC LIMIT 1000").fetchall()
    con.close()
    return render_template("damage.html", title="Damage / Missing", rows=rows)


@app.post("/damage/<int:rid>/update")
@login_required
def damage_update(rid):
    status = request.form.get("review_status", "Open")
    if status not in ("Open", "Under Review", "Closed"):
        abort(400)
    con = db()
    con.execute("UPDATE damage_records SET review_status=?,resolution=? WHERE id=?", (status, request.form.get("resolution", "").strip(), rid))
    r = con.execute("SELECT serial FROM damage_records WHERE id=?", (rid,)).fetchone()
    msg = "Issue review updated."
    if r and status == "Closed":
        d = con.execute("SELECT * FROM devices WHERE serial=?", (r["serial"],)).fetchone()
        still_open = con.execute("SELECT 1 FROM damage_records WHERE serial=? AND review_status!='Closed'", (r["serial"],)).fetchone()
        if d and not still_open and d["status"] in ("Damaged", "Missing", "Under Repair"):
            con.execute("UPDATE devices SET status='Available',condition='Working',updated_at=? WHERE serial=?", (now(), d["serial"]))
            msg = f"Issue closed. {d['serial']} is Available again."
    con.commit()
    con.close()
    flash(msg)
    return redirect(url_for("damage"))


# ---------------------------------------------------------------- exports / backup / users
def send_csv(filename, headers, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(headers)
    w.writerows(rows)
    data = io.BytesIO(("\ufeff" + out.getvalue()).encode("utf-8"))
    return send_file(data, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=filename)


@app.route("/export/devices")
@login_required
def export_devices():
    rows = qall("""SELECT d.serial,d.make,d.model,d.cover_qty,d.status,d.condition,d.assigned_employee_id,
      COALESCE(e.name,''),COALESCE(e.department,''),d.notes,d.updated_at FROM devices d LEFT JOIN employees e ON e.employee_id=d.assigned_employee_id ORDER BY d.serial""")
    return send_csv("HHD_Device_Inventory.csv", ["Serial", "Make", "Model", "Cover Qty", "Status", "Condition", "Employee ID", "Employee", "Department", "Notes", "Updated"], rows)


@app.route("/export/employees")
@login_required
def export_employees():
    rows = qall("""SELECT e.employee_id,e.name,e.department,e.active,COALESCE((SELECT group_concat(serial,'; ') FROM devices WHERE assigned_employee_id=e.employee_id),'')
      FROM employees e ORDER BY e.name""")
    return send_csv("HHD_Employee_Master.csv", ["Employee ID", "Name", "Department", "Active", "Assigned Devices"], rows)


@app.route("/export/transactions")
@login_required
def export_transactions():
    rows = qall("SELECT id,transaction_time,transaction_type,serial,employee_id,employee_name,department,operator,condition,cover_qty,remarks FROM transactions ORDER BY id")
    return send_csv("HHD_Transaction_Audit.csv", ["ID", "Date Time", "Type", "Serial", "Employee ID", "Employee", "Department", "Operator", "Condition", "Cover Qty", "Remarks"], rows)


@app.route("/export/damage")
@login_required
def export_damage():
    rows = qall("SELECT id,report_time,serial,employee_id,employee_name,department,issue_type,details,reported_by,review_status,resolution FROM damage_records ORDER BY id")
    return send_csv("HHD_Damage_Missing.csv", ["ID", "Reported", "Serial", "Employee ID", "Employee", "Department", "Issue", "Details", "Reported By", "Review Status", "Resolution"], rows)


@app.route("/exports")
@login_required
def exports():
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")], reverse=True)[:20]
    return render_template("exports.html", title="Reports / Backup", files=files)


@app.route("/backup", methods=["POST"])
@admin_required
def make_backup():
    dest = os.path.join(BACKUP_DIR, "hhd_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".db")
    con = db()
    try:
        target = sqlite3.connect(dest)
        con.backup(target)
        target.close()
        flash("Database backup created: " + os.path.basename(dest))
    except sqlite3.Error:
        flash("Backup failed. Check disk space and permissions.", "error")
    finally:
        con.close()
    return redirect(url_for("exports"))


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    con = db()
    if request.method == "POST":
        username, password, role = request.form.get("username", "").strip(), request.form.get("password", ""), request.form.get("role", "operator")
        if not username or len(password) < 10 or role not in ("admin", "operator"):
            flash("Enter a username, a password of at least 10 characters and a valid role.", "error")
        else:
            try:
                con.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                            (username, generate_password_hash(password), role, now()))
                con.commit()
                flash("User created.")
            except sqlite3.IntegrityError:
                flash("Username already exists.", "error")
    rows = con.execute("SELECT username,role,active,created_at FROM users ORDER BY username").fetchall()
    con.close()
    return render_template("users.html", title="Users", rows=rows)


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", title="Bad request", message=getattr(e, "description", str(e))), 400


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", title="Not allowed", message="You do not have permission to do that."), 403


def serve():
    init_db()
    host, port = os.environ.get("HHD_HOST", "0.0.0.0"), int(os.environ.get("HHD_PORT", "8080"))
    print(f"HHD Manager listening on http://{host}:{port}   (board: /board)")
    try:
        from waitress import serve as wserve          # production-grade server, installed by INSTALL.bat
        wserve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    serve()
