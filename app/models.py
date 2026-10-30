from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text,
    ForeignKey, Index
)
from sqlalchemy.orm import relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False, default="")
    role = Column(String(20), nullable=False, default="operator")  # "admin" or "operator"
    default_shift = Column(String(20), nullable=False, default="Morning")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    transactions = relationship("Transaction", back_populates="operator")
    damage_reports = relationship("DamageReport", back_populates="reporter")

class Employee(Base):
    __tablename__ = "employees"

    employee_id = Column(String(64), primary_key=True, index=True)
    name = Column(String(120), nullable=False, index=True)
    department = Column(String(100), nullable=False, index=True)
    active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    devices = relationship("Device", back_populates="assigned_employee")
    assignments = relationship("DeviceAssignment", back_populates="employee")
    transactions = relationship("Transaction", back_populates="employee")

class Device(Base):
    __tablename__ = "devices"

    serial = Column(String(64), primary_key=True, index=True)
    make = Column(String(50), nullable=False, default="CHAINWAY")
    model = Column(String(50), nullable=False, default="HHD")
    cover_qty = Column(Integer, nullable=False, default=0)
    status = Column(String(30), nullable=False, default="Available", index=True)  # Available, Issued, Damaged, Missing, Under Repair
    assigned_employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=True, index=True)
    condition = Column(String(30), nullable=False, default="Working")  # Working, Damaged, Missing, Under Repair
    notes = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    assigned_employee = relationship("Employee", back_populates="devices")
    assignments = relationship("DeviceAssignment", back_populates="device", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="device")
    damage_reports = relationship("DamageReport", back_populates="device")

class DeviceAssignment(Base):
    __tablename__ = "device_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=False, index=True)
    
    issued_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    issued_by_operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    issued_by_operator_name = Column(String(100), nullable=False, default="")
    issued_shift = Column(String(20), nullable=False, default="Morning")
    issued_operational_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD

    returned_at = Column(DateTime, nullable=True, index=True)
    returned_by_operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    returned_by_operator_name = Column(String(100), nullable=True)
    returned_shift = Column(String(20), nullable=True)
    returned_operational_date = Column(String(10), nullable=True, index=True)

    status = Column(String(20), nullable=False, default="ACTIVE", index=True)  # ACTIVE, RETURNED
    condition_on_return = Column(String(30), nullable=True)
    remarks = Column(Text, nullable=False, default="")

    device = relationship("Device", back_populates="assignments")
    employee = relationship("Employee", back_populates="assignments")

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_time = Column(DateTime, nullable=False, default=datetime.now, index=True)
    operational_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    shift_name = Column(String(20), nullable=False, index=True)       # Morning, Evening, Night
    transaction_type = Column(String(20), nullable=False, index=True)  # Issue, Return
    serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=False, index=True)
    employee_name = Column(String(120), nullable=False)
    department = Column(String(100), nullable=False)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    operator_name = Column(String(100), nullable=False)
    condition = Column(String(30), nullable=False, default="Working")
    cover_qty = Column(Integer, nullable=False, default=0)
    remarks = Column(Text, nullable=False, default="")
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)

    operator = relationship("User", back_populates="transactions")
    device = relationship("Device", back_populates="transactions")
    employee = relationship("Employee", back_populates="transactions")

class DamageReport(Base):
    __tablename__ = "damage_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_time = Column(DateTime, nullable=False, default=datetime.now, index=True)
    operational_date = Column(String(10), nullable=False, index=True)
    shift_name = Column(String(20), nullable=False, index=True)
    serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=True, index=True)
    employee_name = Column(String(120), nullable=False, default="")
    department = Column(String(100), nullable=False, default="")
    fault_type = Column(String(50), nullable=False)  # Blank screen, Not powering on, etc.
    details = Column(Text, nullable=False, default="")
    reported_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reported_by_name = Column(String(100), nullable=False)
    review_status = Column(String(30), nullable=False, default="Open", index=True)  # Open, Under Repair, Repaired/Available, Written Off, Closed
    resolution_notes = Column(Text, nullable=False, default="")
    resolved_by = Column(String(100), nullable=False, default="")
    resolved_at = Column(DateTime, nullable=True)

    device = relationship("Device", back_populates="damage_reports")
    reporter = relationship("User", back_populates="damage_reports")

class ShiftLog(Base):
    __tablename__ = "shift_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    operational_date = Column(String(10), nullable=False, index=True)
    shift_name = Column(String(20), nullable=False, index=True)
    opened_at = Column(DateTime, nullable=False, default=datetime.now)
    closed_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="OPEN", index=True)  # OPEN, CLOSED
    closed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    closed_by_name = Column(String(100), nullable=False, default="")
    
    total_issued = Column(Integer, nullable=False, default=0)
    total_returned = Column(Integer, nullable=False, default=0)
    total_working = Column(Integer, nullable=False, default=0)
    total_damaged = Column(Integer, nullable=False, default=0)
    total_missing = Column(Integer, nullable=False, default=0)
    total_outstanding = Column(Integer, nullable=False, default=0)
    notes = Column(Text, nullable=False, default="")

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.now, index=True)
    user_id = Column(Integer, nullable=True)
    username = Column(String(64), nullable=False)
    action = Column(String(50), nullable=False, index=True)
    target_type = Column(String(50), nullable=False, index=True)
    target_id = Column(String(100), nullable=False, default="")
    details = Column(Text, nullable=False, default="")

# Composite indexes for high-throughput queries
Index("idx_tx_opdate_shift", Transaction.operational_date, Transaction.shift_name)
Index("idx_asgn_active_dev", DeviceAssignment.status, DeviceAssignment.device_serial)
Index("idx_asgn_active_emp", DeviceAssignment.status, DeviceAssignment.employee_id)
Index("idx_dmg_open_serial", DamageReport.serial, DamageReport.review_status)

