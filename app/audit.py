import json
from datetime import datetime
from typing import Optional, Any
from sqlalchemy.orm import Session
from app.models import AuditLog, User
from app.config import now_kolkata

def record_audit(
    db: Session,
    user: Optional[User] = None,
    action: str = "UNKNOWN",
    target_type: str = "SYSTEM",
    target_id: str = "",
    details: str = "",
    previous_value: Optional[Any] = None,
    new_value: Optional[Any] = None,
    status: str = "SUCCESS",
    reason: Optional[str] = None,
    username_override: Optional[str] = None,
    role_override: Optional[str] = None
) -> Optional[AuditLog]:
    """
    Centralized, append-only, immutable audit trail recorder.
    Ensures safe serialization of previous/new values and captures user snapshot.
    """
    try:
        if user:
            u_id = user.id
            u_name = user.full_name or user.username
            u_role = user.role
        else:
            u_id = None
            u_name = username_override or "SYSTEM"
            u_role = role_override or "system"

        prev_str = None
        if previous_value is not None:
            prev_str = json.dumps(previous_value, default=str) if isinstance(previous_value, (dict, list)) else str(previous_value)

        new_str = None
        if new_value is not None:
            new_str = json.dumps(new_value, default=str) if isinstance(new_value, (dict, list)) else str(new_value)

        log = AuditLog(
            timestamp=now_kolkata(),
            user_id=u_id,
            username=u_name[:64],
            role=u_role[:30],
            action=action[:50],
            target_type=target_type[:50],
            target_id=str(target_id)[:100],
            previous_value=prev_str,
            new_value=new_str,
            details=details,
            status=status[:20],
            reason=reason[:255] if reason else None
        )
        db.add(log)
        db.flush()
        return log
    except Exception as ex:
        # Never let an audit logging failure break business operations silently,
        # but do not raise if db session is in an aborted state
        print(f"[AUDIT ERROR] Failed to record audit log: {ex}")
        return None

