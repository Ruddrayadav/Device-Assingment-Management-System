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

