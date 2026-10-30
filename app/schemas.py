from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field

class UserLogin(BaseModel):
    username: str
    password: str
    shift_name: Optional[str] = "Morning"

class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str = ""
    role: str = "operator"
    default_shift: str = "Morning"

class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    default_shift: str
    active: bool
    created_at: Any

    class Config:
        from_attributes = True

class EmployeeCreate(BaseModel):
    employee_id: str
    name: str
    department: str
    active: bool = True

class EmployeeOut(BaseModel):
    employee_id: str
    name: str
    department: str
    active: bool
    created_at: Any
    updated_at: Any
    held_count: Optional[int] = 0

    class Config:
        from_attributes = True

class DeviceCreate(BaseModel):
    serial: str
    make: str = "CHAINWAY"
    model: str = "HHD"
    cover_qty: int = 0
    status: str = "Available"
    condition: str = "Working"
    notes: str = ""

class DeviceOut(BaseModel):
    serial: str
    make: str
    model: str
    cover_qty: int
    status: str
    condition: str
    notes: str
    assigned_employee_id: Optional[str] = None
    assigned_employee_name: Optional[str] = None
    assigned_department: Optional[str] = None
    updated_at: Any

    class Config:
        from_attributes = True

class PendingScan(BaseModel):
    type: str  # "employee" or "device"
    code: str
    label: Optional[str] = ""

class ScanRequest(BaseModel):
    code: str
    mode: str = "Issue"  # "Issue" or "Return"
    pending: Optional[PendingScan] = None
    shift_name: Optional[str] = None
    idempotency_key: Optional[str] = None

class ScanResponse(BaseModel):
    ok: bool
    state: str  # "done", "pending", "error", "unknown"
    title: str = ""
    message: str = ""
    action: Optional[str] = None
    device: Optional[Dict[str, Any]] = None
    employee: Optional[Dict[str, Any]] = None
    pending: Optional[PendingScan] = None
    can_flag: bool = False
    can_add: bool = False
    shift_stats: Optional[Dict[str, Any]] = None

class DamageReportCreate(BaseModel):
    serial: str
    fault_type: str
    details: str = ""
    employee_id: Optional[str] = None
    shift_name: Optional[str] = None

class DamageUpdate(BaseModel):
    review_status: str
    resolution_notes: str = ""

class ShiftCloseRequest(BaseModel):
    operational_date: str
    shift_name: str
    notes: str = ""

