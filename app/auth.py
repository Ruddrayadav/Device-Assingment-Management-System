import hmac
import hashlib
import json
import base64
import time
from typing import Optional, Dict, Any, Tuple
import threading
from fastapi import Request, HTTPException, status, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.config import SECRET_KEY
from app.database import get_db
from app.models import User

# Brute-force & Rate Limiting Protection (username, client IP pair + separate high IP limit)
class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, lockout_window_sec: int = 300, max_ip_attempts: int = 30):
        self.max_attempts = max_attempts
        self.max_ip_attempts = max_ip_attempts
        self.lockout_window_sec = lockout_window_sec
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[str, Any]] = {}

    def _clean_expired(self, now: float):
        keys_to_del = []
        for k, v in self._store.items():
            if now > v.get("locked_until", 0) and (now - v.get("first_attempt", 0) > self.lockout_window_sec):
                keys_to_del.append(k)
        for k in keys_to_del:
            self._store.pop(k, None)

    def is_locked_out_pair(self, username: str, ip: str) -> Tuple[bool, int, str]:
        """
        Checks whether either the specific (username, ip) pair or the overall client IP is locked out.
        Returns (is_locked, remaining_seconds, reason) where reason is 'pair' or 'ip'.
        """
        now = time.time()
        with self._lock:
            self._clean_expired(now)
            pair_key = f"pair:{username.lower().strip()}:{ip}"
            ip_key = f"ip:{ip}"

            p_entry = self._store.get(pair_key)
            if p_entry and now < p_entry.get("locked_until", 0):
                return True, max(1, int(p_entry["locked_until"] - now)), "pair"

            i_entry = self._store.get(ip_key)
            if i_entry and now < i_entry.get("locked_until", 0):
                return True, max(1, int(i_entry["locked_until"] - now)), "ip"

            return False, 0, ""

    def record_failure_pair(self, username: str, ip: str) -> Tuple[int, int, bool]:
        """
        Records failure against (username, client_ip) pair and client_ip limit (30 attempts).
        Returns (pair_attempts, pair_lock_sec, is_ip_locked).
        """
        now = time.time()
        with self._lock:
            self._clean_expired(now)
            pair_key = f"pair:{username.lower().strip()}:{ip}"
            ip_key = f"ip:{ip}"

            # 1. Update (username, ip) pair
            p_entry = self._store.get(pair_key)
            if not p_entry or (now - p_entry.get("first_attempt", 0) > self.lockout_window_sec and now > p_entry.get("locked_until", 0)):
                p_entry = {"attempts": 1, "first_attempt": now, "locked_until": 0}
                self._store[pair_key] = p_entry
            else:
                p_entry["attempts"] += 1

            p_attempts = p_entry["attempts"]
            p_lock = 0
            if p_attempts >= self.max_attempts:
                p_entry["locked_until"] = now + self.lockout_window_sec
                p_lock = self.lockout_window_sec

            # 2. Update IP limit
            i_entry = self._store.get(ip_key)
            if not i_entry or (now - i_entry.get("first_attempt", 0) > self.lockout_window_sec and now > i_entry.get("locked_until", 0)):
                i_entry = {"attempts": 1, "first_attempt": now, "locked_until": 0}
                self._store[ip_key] = i_entry
            else:
                i_entry["attempts"] += 1

            ip_locked = False
            if i_entry["attempts"] >= self.max_ip_attempts:
                i_entry["locked_until"] = now + self.lockout_window_sec
                ip_locked = True

            return p_attempts, p_lock, ip_locked

    def is_locked_out(self, key: str) -> Tuple[bool, int]:
        now = time.time()
        with self._lock:
            self._clean_expired(now)
            entry = self._store.get(key)
            if entry and now < entry.get("locked_until", 0):
                return True, max(1, int(entry["locked_until"] - now))
            prefix = f"pair:{key.lower().strip()}:"
            for k, v in self._store.items():
                if k.startswith(prefix) and now < v.get("locked_until", 0):
                    return True, max(1, int(v["locked_until"] - now))
            return False, 0

    def record_failure(self, key: str) -> Tuple[int, int]:
        now = time.time()
        with self._lock:
            self._clean_expired(now)
            entry = self._store.get(key)
            if not entry or (now - entry.get("first_attempt", 0) > self.lockout_window_sec and now > entry.get("locked_until", 0)):
                entry = {"attempts": 1, "first_attempt": now, "locked_until": 0}
                self._store[key] = entry
            else:
                entry["attempts"] += 1

            attempts = entry["attempts"]
            if attempts >= self.max_attempts:
                entry["locked_until"] = now + self.lockout_window_sec
                return attempts, self.lockout_window_sec
            return attempts, 0

    def reset_pair(self, username: str, ip: str):
        with self._lock:
            self._store.pop(f"pair:{username.lower().strip()}:{ip}", None)
            self._store.pop(f"ip:{ip}", None)

    def reset(self, key: str):
        with self._lock:
            self._store.pop(key, None)
            prefix = f"pair:{key.lower().strip()}:"
            for k in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(k, None)

    def unlock_user(self, username: str):
        uname = username.lower().strip()
        with self._lock:
            self._store.pop(uname, None)
            prefix = f"pair:{uname}:"
            for k in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(k, None)

    def clear_all(self):
        with self._lock:
            self._store.clear()

login_limiter = LoginRateLimiter(max_attempts=5, lockout_window_sec=300, max_ip_attempts=30)


# Password hashing using PBKDF2-HMAC-SHA256 (standard library, zero native dependency risk on Windows)
def hash_password(password: str) -> str:
    salt = hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000)
    return f"pbkdf2_sha256$200000${salt}${derived.hex()}"

def verify_password(plain_password: str, hashed: str) -> bool:
    if not hashed:
        return False
    # Check for PBKDF2 style hash (Werkzeug / Flask / PBKDF2) - Pure Standard Library
    if hashed.startswith("pbkdf2:sha256:"):
        try:
            parts = hashed.split("$")
            if len(parts) == 3:
                method_part, salt, hashval = parts
                iterations = int(method_part.split(":")[-1])
                derived = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), iterations)
                return hmac.compare_digest(derived.hex(), hashval)
        except Exception:
            pass

    # Check for Scrypt style hash (Pure Standard Library)
    if hashed.startswith("scrypt:"):
        try:
            parts = hashed.split("$")
            if len(parts) == 3:
                method_part, salt, hashval = parts
                subparts = method_part.split(":")
                n = int(subparts[1]) if len(subparts) > 1 else 32768
                r = int(subparts[2]) if len(subparts) > 2 else 8
                p = int(subparts[3]) if len(subparts) > 3 else 1
                derived = hashlib.scrypt(plain_password.encode("utf-8"), salt=salt.encode("utf-8"), n=n, r=r, p=p)
                return hmac.compare_digest(derived.hex(), hashval)
        except Exception:
            pass
    # Custom PBKDF2 format
    if hashed.startswith("pbkdf2_sha256$"):
        try:
            parts = hashed.split("$")
            iterations = int(parts[1])
            salt = parts[2]
            stored_hash = parts[3]
            derived = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), iterations)
            return hmac.compare_digest(derived.hex(), stored_hash)
        except Exception:
            return False
    # Check if bcrypt
    if hashed.startswith("$2a$") or hashed.startswith("$2b$"):
        try:
            import bcrypt
            return bcrypt.checkpw(plain_password.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False
    return False

# Session token encoder & decoder (HMAC SHA-256)
def sign_session(data: Dict[str, Any], max_age_seconds: int = 43200) -> str:
    payload = {
        "data": data,
        "exp": int(time.time()) + max_age_seconds
    }
    raw = json.dumps(payload, separators=(',', ':')).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(raw).decode("utf-8")
    signature = hmac.new(SECRET_KEY.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{b64_payload}.{signature}"

def decode_session(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    token = token.strip('"')
    if "." not in token:
        return None
    try:
        b64_payload, signature = token.rsplit(".", 1)
        expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        raw = base64.urlsafe_b64decode(b64_payload.encode("utf-8")).decode("utf-8")
        payload = json.loads(raw)
        if time.time() > payload.get("exp", 0):
            return None
        return payload.get("data")
    except Exception:
        return None

ROLE_ADMIN = "admin"
ROLE_INVENTORY_MANAGER = "inventory_manager"
ROLE_OPERATOR = "operator"
ROLE_DISPLAY = "display"
VALID_ROLES = {ROLE_ADMIN, ROLE_INVENTORY_MANAGER, ROLE_OPERATOR, ROLE_DISPLAY}

def is_local_client(request: Request) -> bool:
    """
    Checks if an HTTP request originated from the local Windows host server machine.
    Permits 127.0.0.1, ::1, localhost, and testclient.
    Blocks remote LAN computers from accessing unauthenticated setup.
    """
    client_host = request.client.host if request.client else "127.0.0.1"

    # Check X-Forwarded-For if client/proxy passes it
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first_ip = forwarded.split(",")[0].strip()
        if first_ip not in ("127.0.0.1", "::1", "localhost", "testclient", "0.0.0.0"):
            return False

    return client_host in ("127.0.0.1", "::1", "localhost", "testclient", "0.0.0.0")

def is_initial_setup_required(db: Session) -> bool:
    """
    Returns True ONLY if no administrator account exists in the database.
    Once an admin is created or setup is marked complete, first-time setup is permanently closed.
    Setup will NEVER reopen merely because an existing admin cannot log in or is deactivated.
    """
    try:
        from app.models import SystemSetting
        setting = db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").first()
        if setting and setting.value == "true":
            return False

        # If ANY administrator account exists (active or inactive), setup is closed
        admin_count = db.query(User).filter(User.role == ROLE_ADMIN).count()
        if admin_count > 0:
            return False
    except Exception:
        return True

    return True

def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get("hhd_session")
    if not token:
        return None
    data = decode_session(token)
    if not data or "username" not in data:
        return None
    user = db.query(User).filter(User.username == data["username"], User.active == True).first()
    return user

def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    if is_initial_setup_required(db):
        if not is_local_client(request):
            if request.url.path.startswith("/api/"):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Initial administrator setup must be completed on the central host server before warehouse operations can begin."
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Server setup in progress. Please configure the primary administrator on the central host server computer."
            )
        if request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Initial administrator setup required. Please visit /setup."
            )
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/setup"}
        )
    user = get_current_user_optional(request, db)
    if not user:
        if request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or not authenticated. Please log in."
            )
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"/login?next={request.url.path}"}
        )

    # Display role is read-only and restricted exclusively to the wall display board
    if user.role == ROLE_DISPLAY:
        path = request.url.path
        if path not in ("/board", "/api/board/live") and not path.startswith("/static/"):
            if path.startswith("/api/"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Display role is restricted exclusively to the wall display board."
                )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Wall display account is restricted to display board only."
            )

    return user

def require_admin(request: Request, user: User = Depends(require_user)) -> User:
    if user.role != ROLE_ADMIN:
        if request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access required."
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required."
        )
    return user

def require_inventory_manager(request: Request, user: User = Depends(require_user)) -> User:
    """Allows Admin and Inventory Manager roles."""
    if user.role not in (ROLE_ADMIN, ROLE_INVENTORY_MANAGER):
        if request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Inventory Manager or Administrator access required."
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inventory Manager or Administrator access required."
        )
    return user

