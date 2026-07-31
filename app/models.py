import enum
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Boolean, Enum, Text, Index
)
from sqlalchemy.orm import relationship

from .database import Base


class SlotStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    BOOKED = "BOOKED"
    WITHDRAWN = "WITHDRAWN"  # removed due to a retroactive availability change


class BookingStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    # Home timezone used only for convenience when doctors submit availability
    # in local time. Everything is persisted in UTC.
    timezone = Column(String, nullable=False, default="UTC")

    availability_windows = relationship("AvailabilityWindow", back_populates="doctor")
    slots = relationship("Slot", back_populates="doctor")


class AvailabilityWindow(Base):
    """
    A doctor's broad statement of availability, e.g. "Monday 10:00-18:00".
    This is the source of truth from which Slot rows are materialized.
    Kept even after slots are generated so we can regenerate / audit / diff
    when a doctor edits their hours.
    """
    __tablename__ = "availability_windows"

    id = Column(Integer, primary_key=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    start_utc = Column(DateTime, nullable=False)
    end_utc = Column(DateTime, nullable=False)
    slot_duration_minutes = Column(Integer, nullable=False)
    buffer_minutes = Column(Integer, nullable=False, default=0)
    appointment_type = Column(String, nullable=False, default="default")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor", back_populates="availability_windows")
    slots = relationship("Slot", back_populates="availability_window")


class Slot(Base):
    """
    A materialized, individually-bookable unit of time.

    Materialized (rather than computed on the fly) so that:
      - we can put a real DB row + version column + unique constraint on it
        and get atomic, race-free booking guarantees straight from the DB
      - "what's free right now" is a simple indexed query, not a runtime
        computation that has to subtract bookings from windows on every
        read (which gets expensive and racy at scale)
      - cancellations/holds/audit history attach naturally to a stable id
    """
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    availability_window_id = Column(Integer, ForeignKey("availability_windows.id"), nullable=True)
    start_utc = Column(DateTime, nullable=False)
    end_utc = Column(DateTime, nullable=False)
    appointment_type = Column(String, nullable=False, default="default")

    status = Column(Enum(SlotStatus), nullable=False, default=SlotStatus.AVAILABLE)
    # Optimistic-concurrency version. Every state-changing update is a
    # conditional `UPDATE ... WHERE id=:id AND version=:version`, so two
    # concurrent transactions can never both "win" a transition.
    version = Column(Integer, nullable=False, default=0)

    hold_expires_at = Column(DateTime, nullable=True)
    held_by = Column(String, nullable=True)

    doctor = relationship("Doctor", back_populates="slots")
    availability_window = relationship("AvailabilityWindow", back_populates="slots")
    bookings = relationship("Booking", back_populates="slot")

    __table_args__ = (
        # A given doctor can't have two slots of the same appointment_type
        # starting at the same instant. Prevents duplicate slot generation
        # even if generate_slots_for_window is called twice concurrently.
        Index(
            "uix_doctor_slot_start_type",
            "doctor_id", "start_utc", "appointment_type",
            unique=True,
        ),
    )


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True)
    slot_id = Column(Integer, ForeignKey("slots.id"), nullable=False)
    patient_id = Column(String, nullable=False)
    status = Column(Enum(BookingStatus), nullable=False, default=BookingStatus.ACTIVE)
    created_at = Column(DateTime, default=datetime.utcnow)
    cancelled_at = Column(DateTime, nullable=True)
    # Set when this booking was replaced by a reschedule, points at the
    # booking that superseded it, for audit trails.
    rescheduled_to_booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=True)

    slot = relationship("Slot", back_populates="bookings")

    __table_args__ = (
        # THE core double-booking guarantee, enforced by the database
        # itself, not just application logic: at most one ACTIVE booking
        # can ever exist for a given slot. Even if two requests raced past
        # every check in the service layer, the second INSERT fails here.
        Index(
            "uix_one_active_booking_per_slot",
            "slot_id",
            unique=True,
            sqlite_where=(status == BookingStatus.ACTIVE.value),
            postgresql_where=(status == BookingStatus.ACTIVE.value),
        ),
    )


class AuditLog(Base):
    """Append-only log of every state transition, for auditability."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    entity_type = Column(String, nullable=False)
    entity_id = Column(Integer, nullable=False)
    action = Column(String, nullable=False)
    actor = Column(String, nullable=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
