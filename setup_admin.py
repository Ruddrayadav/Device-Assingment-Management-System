import sys
import argparse
import getpass
import re
from datetime import datetime
from app.database import engine, Base, SessionLocal, DB_PATH
from app.models import User, AuditLog, SystemSetting
from app.auth import hash_password, login_limiter, ROLE_ADMIN

def list_admins(db):
    print("=" * 68)
    print(" HHD SERVER HANDOVER SYSTEM — REGISTERED ADMINISTRATORS")
    print("=" * 68)
    admins = db.query(User).filter(User.role == ROLE_ADMIN).all()
    if not admins:
        print(" [!] No administrator accounts found in the database.")
        print(f" Database Location: {DB_PATH}")
        print(" Run 'python setup_admin.py' to configure the initial administrator.")
    else:
        print(f"{'Username':<20} {'Full Name':<25} {'Status':<10} {'Updated At'}")
        print("-" * 68)
        for a in admins:
            status = "Active" if a.active else "Inactive"
            is_locked, rem = login_limiter.is_locked_out(a.username.lower())
            if is_locked:
                status += f" (Locked {rem}s)"
            updated = a.updated_at.strftime("%Y-%m-%d %H:%M") if a.updated_at else "N/A"
            print(f"{a.username:<20} {a.full_name:<25} {status:<10} {updated}")
    print("=" * 68)

def unlock_user(db, username: str):
    user = db.query(User).filter(User.username.ilike(username)).first()
    if not user:
        print(f"[-] Error: User '{username}' was not found in the database.")
        return False
    login_limiter.reset(user.username.lower())
    if not user.active:
        user.active = True
        user.updated_at = datetime.now()
        db.commit()
        print(f"[+] User '{user.username}' was inactive and has now been reactivated.")
    print(f"[+] Rate-limiting lockout cleared successfully for '{user.username}'.")
    return True

def mark_setup_completed(db, now):
    setting = db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").first()
    if not setting:
        setting = SystemSetting(
            key="setup_completed",
            value="true",
            description="First-time setup completed flag",
            updated_at=now
        )
        db.add(setting)
    else:
        setting.value = "true"
        setting.updated_at = now

def interactive_setup(db, preset_username=None):
    print("=" * 68)
    print(" HHD SERVER HANDOVER SYSTEM — ADMINISTRATOR ACCOUNT SETUP & RECOVERY")
    print("=" * 68)
    print(f" Database: {DB_PATH}\n")

    if preset_username:
        username = preset_username.strip()
    else:
        while True:
            username = input("Enter administrator username: ").strip()
            if not username:
                print("Username cannot be empty. Please enter a valid username.")
                continue
            if len(username) < 3 or len(username) > 64:
                print("Username must be between 3 and 64 characters.")
                continue
            if not re.match(r"^[a-zA-Z0-9_\-\.]+$", username):
                print("Username may only contain letters, numbers, underscores, hyphens, and periods.")
                continue
            break

    existing = db.query(User).filter(User.username.ilike(username)).first()
    if existing:
        print(f"\nUser '{existing.username}' already exists (Current Role: {existing.role}, Active: {existing.active}).")
        confirm_update = input("Do you wish to reset their password and assign the Administrator role? [y/N]: ").strip().lower()
        if confirm_update not in ("y", "yes"):
            print("Operation aborted by user.")
            return

    while True:
        pwd = getpass.getpass("Enter new administrator password (minimum 8 characters): ")
        if len(pwd) < 8:
            print("Password must be at least 8 characters long.")
            continue
        if pwd.lower() == username.lower():
            print("Password cannot be identical to the username.")
            continue
        if pwd.lower() in ("password", "12345678", "admin12345"):
            print("Password is too common or weak. Please select a stronger password.")
            continue
        confirm = getpass.getpass("Confirm administrator password: ")
        if pwd != confirm:
            print("Passwords do not match. Try again.")
            continue
        break

    full_name_default = existing.full_name if (existing and existing.full_name) else "Warehouse Admin"
    full_name_input = input(f"Enter administrator full name [{full_name_default}]: ").strip()
    full_name = full_name_input or full_name_default
    now = datetime.now()

    if existing:
        print(f"\nUpdating user '{existing.username}' with new administrator credentials...")
        existing.password_hash = hash_password(pwd)
        existing.role = ROLE_ADMIN
        existing.full_name = full_name
        existing.active = True
        existing.updated_at = now
        audit = AuditLog(
            timestamp=now,
            user_id=existing.id,
            username=existing.username,
            role=ROLE_ADMIN,
            action="ADMIN_PASSWORD_RESET_CLI",
            target_type="SECURITY",
            target_id=str(existing.id),
            details=f"Administrator credentials for '{existing.username}' reset via console recovery CLI.",
            status="SUCCESS"
        )
        db.add(audit)
        target_name = existing.username
    else:
        print(f"\nCreating new administrator account '{username}'...")
        user = User(
            username=username,
            password_hash=hash_password(pwd),
            full_name=full_name,
            role=ROLE_ADMIN,
            active=True,
            created_at=now,
            updated_at=now
        )
        db.add(user)
        db.flush()
        audit = AuditLog(
            timestamp=now,
            user_id=user.id,
            username=username,
            role=ROLE_ADMIN,
            action="INITIAL_ADMIN_SETUP_CLI",
            target_type="SECURITY",
            target_id=str(user.id),
            details=f"Administrator account '{username}' created via console setup CLI.",
            status="SUCCESS"
        )
        db.add(audit)
        target_name = username

    mark_setup_completed(db, now)
    login_limiter.reset(target_name.lower())
    db.commit()

    print("\n" + "=" * 68)
    print(f" ✓ Administrator '{target_name}' successfully configured and activated.")
    print("   First-time setup mode is now permanently locked in the web app.")
    print("=" * 68 + "\n")

def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        parser = argparse.ArgumentParser(description="HHD Server Handover System — Administrator Setup & Recovery Utility")
        parser.add_argument("--list", action="store_true", help="List all configured administrator accounts")
        parser.add_argument("--unlock", type=str, metavar="USERNAME", help="Unlock a user locked out by login rate limiting")
        parser.add_argument("--reset", type=str, metavar="USERNAME", help="Reset password and promote user to administrator")
        args = parser.parse_args()

        if args.list:
            list_admins(db)
        elif args.unlock:
            unlock_user(db, args.unlock)
        elif args.reset:
            interactive_setup(db, preset_username=args.reset)
        else:
            interactive_setup(db)
    finally:
        db.close()

if __name__ == "__main__":
    main()
