from datetime import datetime, date
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
    role = Column(String(30), nullable=False, default="operator", index=True)  # "admin", "inventory_manager", "operator"
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
    # Department belongs strictly to employees, not devices. Column remains nullable for backward schema compatibility.
    department = Column(String(100), nullable=True, default=None, index=True)
    inventory_date = Column(String(50), nullable=True)
    cover_qty = Column(Integer, nullable=False, default=0)
    # Available, Issued, Unavailable, Damaged, Under Repair, Sent to Service Centre, Missing
    status = Column(String(30), nullable=False, default="Unavailable", index=True)
    unavailable_reason = Column(String(255), nullable=True, default="Pending Inspection")
    assigned_employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=True, index=True)
    condition = Column(String(30), nullable=False, default="Working")  # Working, Damaged, Under Repair, Missing
    notes = Column(Text, nullable=False, default="")
    
    # Service Centre Tracking
    service_centre_name = Column(String(120), nullable=True)
    service_reference = Column(String(100), nullable=True)
    service_sent_date = Column(String(20), nullable=True)
    service_expected_return = Column(String(20), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    assigned_employee = relationship("Employee", back_populates="devices")
    assignments = relationship("DeviceAssignment", back_populates="device", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="device")
    damage_reports = relationship("DamageReport", back_populates="device")
    missing_events = relationship("MissingEvent", back_populates="device", cascade="all, delete-orphan")
    service_records = relationship("ServiceRecord", back_populates="device", cascade="all, delete-orphan")

class DeviceAssignment(Base):
    __tablename__ = "device_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=False, index=True)
    
    issued_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    issued_date = Column(String(10), nullable=False, default=lambda: date.today().isoformat(), index=True)
    issued_by_operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    issued_by_operator_name = Column(String(100), nullable=False, default="")

    returned_at = Column(DateTime, nullable=True, index=True)
    returned_date = Column(String(10), nullable=True, index=True)
    returned_by_operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    returned_by_operator_name = Column(String(100), nullable=True)

    status = Column(String(20), nullable=False, default="ACTIVE", index=True)  # ACTIVE, RETURNED
    condition_on_return = Column(String(30), nullable=True)
    remarks = Column(Text, nullable=False, default="")

    device = relationship("Device", back_populates="assignments")
    employee = relationship("Employee", back_populates="assignments")

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_time = Column(DateTime, nullable=False, default=datetime.now, index=True)
    transaction_date = Column(String(10), nullable=False, default=lambda: date.today().isoformat(), index=True)  # YYYY-MM-DD
    transaction_type = Column(String(20), nullable=False, index=True)  # Issue, Return
    serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=False, index=True)
    employee_name = Column(String(120), nullable=False)
    department = Column(String(100), nullable=False)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    operator_name = Column(String(100), nullable=False)
    
    # Distinct accountability fields for Issuing vs Returning operator
    issued_by_operator_name = Column(String(100), nullable=True)
    returned_by_operator_name = Column(String(100), nullable=True)
    issued_at = Column(DateTime, nullable=True)
    returned_at = Column(DateTime, nullable=True)

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
    report_date = Column(String(10), nullable=False, default=lambda: date.today().isoformat(), index=True)
    serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=True, index=True)
    employee_name = Column(String(120), nullable=False, default="")
    department = Column(String(100), nullable=False, default="")
    fault_type = Column(String(50), nullable=False)  # Blank screen, Not powering on, etc.
    details = Column(Text, nullable=False, default="")
    source = Column(String(50), nullable=False, default="Operator Report", index=True)  # Operator Report, Excel Master Import, Manual Admin Entry, System Reconciliation
    reported_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reported_by_name = Column(String(100), nullable=False, default="")
    review_status = Column(String(30), nullable=False, default="Open", index=True)  # Open, Under Repair, Sent to Service Centre, Repaired/Available, Written Off, Closed
    resolution_notes = Column(Text, nullable=False, default="")
    resolved_by = Column(String(100), nullable=False, default="")
    resolved_at = Column(DateTime, nullable=True)

    device = relationship("Device", back_populates="damage_reports")
    reporter = relationship("User", back_populates="damage_reports")

class MissingEvent(Base):
    __tablename__ = "missing_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    employee_id = Column(String(64), ForeignKey("employees.employee_id"), nullable=True, index=True)
    employee_name = Column(String(120), nullable=False, default="")
    department = Column(String(100), nullable=False, default="")
    issued_at = Column(DateTime, nullable=True)
    issued_by_operator_name = Column(String(100), nullable=False, default="")
    
    confirmed_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    confirmed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_by_name = Column(String(100), nullable=False, default="")
    reason = Column(Text, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    status = Column(String(30), nullable=False, default="CONFIRMED_MISSING", index=True)  # CONFIRMED_MISSING, RECOVERED

    # Recovery details
    recovered_at = Column(DateTime, nullable=True)
    recovered_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    recovered_by_name = Column(String(100), nullable=True)
    found_by = Column(String(120), nullable=True)
    recovery_condition = Column(String(50), nullable=True)
    recovery_notes = Column(Text, nullable=True)
    post_recovery_status = Column(String(50), nullable=True)

    device = relationship("Device", back_populates="missing_events")

class ServiceRecord(Base):
    __tablename__ = "service_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    serial = Column(String(64), ForeignKey("devices.serial"), nullable=False, index=True)
    service_centre_name = Column(String(120), nullable=False)
    sent_date = Column(String(20), nullable=False, default=lambda: date.today().isoformat(), index=True)
    external_reference = Column(String(100), nullable=True)
    expected_return_date = Column(String(20), nullable=True)
    actual_return_date = Column(String(20), nullable=True)
    sent_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sent_by_name = Column(String(100), nullable=False, default="")
    service_result = Column(Text, nullable=True)
    inspection_notes = Column(Text, nullable=True)
    inspected_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    inspected_by_name = Column(String(100), nullable=True)
    status = Column(String(30), nullable=False, default="SENT", index=True)  # SENT, RETURNED, APPROVED, WRITTEN_OFF
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    device = relationship("Device", back_populates="service_records")

class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.now, index=True)
    user_id = Column(Integer, nullable=True)
    username = Column(String(64), nullable=False, index=True)
    role = Column(String(30), nullable=False, default="operator")
    action = Column(String(50), nullable=False, index=True)
    target_type = Column(String(50), nullable=False, index=True)
    target_id = Column(String(100), nullable=False, default="")
    previous_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    details = Column(Text, nullable=False, default="")
    status = Column(String(20), nullable=False, default="SUCCESS", index=True)  # SUCCESS, FAILED
    reason = Column(String(255), nullable=True)

# Composite indexes for high-throughput 24/7 queries
Index("idx_tx_date_type", Transaction.transaction_date, Transaction.transaction_type)
Index("idx_tx_operator", Transaction.operator_name, Transaction.transaction_date)
Index("idx_asgn_active_dev", DeviceAssignment.status, DeviceAssignment.device_serial)
Index("idx_asgn_active_emp", DeviceAssignment.status, DeviceAssignment.employee_id)
Index("idx_dmg_open_serial", DamageReport.serial, DamageReport.review_status)
Index("idx_missing_serial_status", MissingEvent.serial, MissingEvent.status)
Index("idx_service_serial_status", ServiceRecord.serial, ServiceRecord.status)
