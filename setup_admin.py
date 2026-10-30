import getpass
from datetime import datetime
from app.database import engine, Base, SessionLocal
from app.models import User
from app.auth import hash_password

def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    print("=" * 60)
    print(" HHD Management System - Administrator Setup Utility")
    print("=" * 60)

    username = input("Enter admin username [hhdadmin]: ").strip() or "hhdadmin"
    existing = db.query(User).filter(User.username == username).first()

    while True:
        pwd = getpass.getpass("Enter admin password (min 8 characters): ")
        if len(pwd) < 8:
            print("Password must be at least 8 characters long.")
            continue
        confirm = getpass.getpass("Confirm admin password: ")
        if pwd != confirm:
            print("Passwords do not match. Try again.")
            continue
        break

    full_name = input("Enter administrator full name [Warehouse Admin]: ").strip() or "Warehouse Admin"
    now = datetime.now()

    if existing:
        print(f"Updating existing user '{username}' to administrator...")
        existing.password_hash = hash_password(pwd)
        existing.role = "admin"
        existing.full_name = full_name
        existing.active = True
        existing.updated_at = now
    else:
        print(f"Creating new administrator account '{username}'...")
        user = User(
            username=username,
            password_hash=hash_password(pwd),
            full_name=full_name,
            role="admin",
            default_shift="Morning",
            active=True,
            created_at=now,
            updated_at=now
        )
        db.add(user)

    db.commit()
    db.close()
    print("✓ Administrator account ready successfully!")

if __name__ == "__main__":
    main()
