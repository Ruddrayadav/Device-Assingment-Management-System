import os
import shutil
import tempfile
import atexit

# Create temporary directories for test data and backups
_temp_dir = tempfile.mkdtemp(prefix="hhd_test_")
_test_data_dir = os.path.join(_temp_dir, "data")
_test_backup_dir = os.path.join(_temp_dir, "backups")
os.makedirs(_test_data_dir, exist_ok=True)
os.makedirs(_test_backup_dir, exist_ok=True)

# Point environment variables BEFORE app or config is imported
os.environ["HHD_DATA_DIR"] = _test_data_dir
os.environ["HHD_BACKUP_DIR"] = _test_backup_dir

def _cleanup():
    try:
        shutil.rmtree(_temp_dir, ignore_errors=True)
    except Exception:
        pass

atexit.register(_cleanup)

def seed_test_users():
    """Seeds test administrator and operators in isolated test database for test cases."""
    from datetime import datetime
    from app.database import SessionLocal
    from app.models import User
    from app.auth import hash_password
    db = SessionLocal()
    try:
        now = datetime.now()
        admin_user = db.query(User).filter(User.username == "hhdadmin").first()
        if not admin_user:
            db.add(User(
                username="hhdadmin",
                password_hash=hash_password("admin12345"),
                full_name="Warehouse Administrator",
                role="admin",
                active=True,
                created_at=now,
                updated_at=now
            ))
        else:
            admin_user.active = True
            admin_user.role = "admin"
            admin_user.password_hash = hash_password("admin12345")
        if not db.query(User).filter(User.username == "operator1").first():
            db.add(User(
                username="operator1",
                password_hash=hash_password("operator123"),
                full_name="Rahul (Operator 1)",
                role="operator",
                active=True,
                created_at=now,
                updated_at=now
            ))
        if not db.query(User).filter(User.username == "operator2").first():
            db.add(User(
                username="operator2",
                password_hash=hash_password("operator123"),
                full_name="Amit (Operator 2)",
                role="operator",
                active=True,
                created_at=now,
                updated_at=now
            ))
        db.commit()
    finally:
        db.close()

seed_initial_data_if_needed = seed_test_users


