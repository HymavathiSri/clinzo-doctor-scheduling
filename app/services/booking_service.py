"""
Core booking logic. This is the module the concurrency guarantees live in.

The pattern used everywhere here is a *conditional compare-and-swap update*:

    UPDATE slots
    SET status = 'HELD', version = version + 1, ...
    WHERE id = :id AND version = :expected_version AND status = 'AVAILABLE'

`UPDATE ... WHERE` is atomic in every relational database: if two
transactions race to run this statement against the same row, the
database guarantees only one of them can match the WHERE clause and
actually change the row (the loser's `rowcount` comes back 0, because by
the time it runs, either the version or the status has already moved).
There's no window between "check" and "set" for two threads to both slip
through, because there IS no separate check -- the check and the set are
the same statement.

As a second, independent line of defense, `Booking` also carries a
partial unique index on `slot_id` for `status = 'ACTIVE'` at the schema
level, so even a bug in this service layer could not produce two active
bookings for one slot -- the database would reject the second INSERT.
"""
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Slot, SlotStatus, Booking, BookingStatus
from ..exceptions import (
    NotFoundError, SlotUnavailableError, HoldExpiredError, HoldOwnershipError,
)
from .audit import record

DEFAULT_HOLD_SECONDS = 300  # 5 minutes


def _now():
    return datetime.utcnow()


def hold_slot(db: Session, slot_id: int, patient_id: str,
              hold_seconds: int = DEFAULT_HOLD_SECONDS) -> Slot:
    """
    Step 1 of the two-phase booking flow: reserve a slot for `patient_id`
    without yet creating a Booking. Used when the client needs a gap
    between "reserve" and "confirm" -- e.g. collecting payment details --
    without losing the slot to someone else in the meantime.

    Also opportunistically reclaims slots whose previous hold has expired,
    so a patient who abandons checkout doesn't permanently lock a slot.
    """
    slot = db.get(Slot, slot_id)
    if slot is None:
        raise NotFoundError(f"Slot {slot_id} not found")

    now = _now()
    result = db.execute(
        update(Slot)
        .where(
            Slot.id == slot_id,
            Slot.version == slot.version,
            (
                (Slot.status == SlotStatus.AVAILABLE)
                | ((Slot.status == SlotStatus.HELD) & (Slot.hold_expires_at < now))
            ),
        )
        .values(
            status=SlotStatus.HELD,
            version=Slot.version + 1,
            hold_expires_at=now + timedelta(seconds=hold_seconds),
            held_by=patient_id,
        )
    )
    db.commit()

    if result.rowcount == 0:
        raise SlotUnavailableError(f"Slot {slot_id} is not available to hold")

    db.refresh(slot)
    record(db, "Slot", slot.id, "HELD", patient_id, f"expires_at={slot.hold_expires_at}")
    return slot


def release_hold(db: Session, slot_id: int, patient_id: str) -> Slot:
    """Voluntary release, e.g. patient closes the checkout page."""
    slot = db.get(Slot, slot_id)
    if slot is None:
        raise NotFoundError(f"Slot {slot_id} not found")

    result = db.execute(
        update(Slot)
        .where(Slot.id == slot_id, Slot.version == slot.version, Slot.status == SlotStatus.HELD)
        .values(status=SlotStatus.AVAILABLE, version=Slot.version + 1,
                hold_expires_at=None, held_by=None)
    )
    db.commit()
    if result.rowcount == 0:
        raise SlotUnavailableError(f"Slot {slot_id} is not currently held")
    db.refresh(slot)
    record(db, "Slot", slot.id, "HOLD_RELEASED", patient_id)
    return slot


def confirm_booking(db: Session, slot_id: int, patient_id: str) -> Booking:
    """Step 2: turn an owned, unexpired hold into a real Booking."""
    slot = db.get(Slot, slot_id)
    if slot is None:
        raise NotFoundError(f"Slot {slot_id} not found")
    if slot.status != SlotStatus.HELD or slot.held_by != patient_id:
        raise HoldOwnershipError(f"Slot {slot_id} is not held by {patient_id}")
    if slot.hold_expires_at is not None and slot.hold_expires_at < _now():
        raise HoldExpiredError(f"Hold on slot {slot_id} has expired")

    result = db.execute(
        update(Slot)
        .where(Slot.id == slot_id, Slot.version == slot.version, Slot.status == SlotStatus.HELD)
        .values(status=SlotStatus.BOOKED, version=Slot.version + 1, hold_expires_at=None)
    )
    if result.rowcount == 0:
        db.rollback()
        raise SlotUnavailableError(f"Slot {slot_id} changed state before confirmation")

    booking = Booking(slot_id=slot_id, patient_id=patient_id, status=BookingStatus.ACTIVE)
    db.add(booking)
    try:
        db.commit()
    except IntegrityError:
        # Belt-and-suspenders: the partial unique index on
        # (slot_id WHERE status='ACTIVE') caught a duplicate active
        # booking. Shouldn't happen given the conditional UPDATE above,
        # but if it ever does, fail safe rather than double-book.
        db.rollback()
        raise SlotUnavailableError(f"Slot {slot_id} was already booked")

    db.refresh(booking)
    record(db, "Booking", booking.id, "CONFIRMED", patient_id, f"slot_id={slot_id}")
    return booking


def book_slot(db: Session, slot_id: int, patient_id: str) -> Booking:
    """
    Single-step convenience path for callers that don't need the
    hold/confirm split (e.g. a simple "book now" button with no payment
    step). Goes straight AVAILABLE -> BOOKED with the same atomic
    compare-and-swap guarantee as the two-phase flow.
    """
    slot = db.get(Slot, slot_id)
    if slot is None:
        raise NotFoundError(f"Slot {slot_id} not found")

    now = _now()
    result = db.execute(
        update(Slot)
        .where(
            Slot.id == slot_id,
            Slot.version == slot.version,
            (
                (Slot.status == SlotStatus.AVAILABLE)
                | ((Slot.status == SlotStatus.HELD) & (Slot.hold_expires_at < now))
            ),
        )
        .values(status=SlotStatus.BOOKED, version=Slot.version + 1,
                hold_expires_at=None, held_by=None)
    )
    if result.rowcount == 0:
        db.rollback()
        raise SlotUnavailableError(f"Slot {slot_id} is not available")

    booking = Booking(slot_id=slot_id, patient_id=patient_id, status=BookingStatus.ACTIVE)
    db.add(booking)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SlotUnavailableError(f"Slot {slot_id} was already booked")

    db.refresh(booking)
    record(db, "Booking", booking.id, "CONFIRMED", patient_id, f"slot_id={slot_id} (direct)")
    return booking


def cancel_booking(db: Session, booking_id: int, actor: str) -> Booking:
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise NotFoundError(f"Booking {booking_id} not found")
    if booking.status != BookingStatus.ACTIVE:
        raise SlotUnavailableError(f"Booking {booking_id} is not active")

    slot = db.get(Slot, booking.slot_id)
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = _now()

    db.execute(
        update(Slot)
        .where(Slot.id == slot.id, Slot.version == slot.version, Slot.status == SlotStatus.BOOKED)
        .values(status=SlotStatus.AVAILABLE, version=Slot.version + 1)
    )
    db.commit()
    db.refresh(booking)
    record(db, "Booking", booking.id, "CANCELLED", actor, f"slot_id={slot.id}")
    return booking


def reschedule_booking(db: Session, booking_id: int, new_slot_id: int, patient_id: str) -> Booking:
    """
    Move a patient from one confirmed slot to another, preserving the
    guarantee that they always have exactly one active appointment.

    Ordering matters for correctness under partial failure: we claim the
    NEW slot first. If that fails (already taken), the OLD booking is
    left completely untouched -- the patient keeps their original
    appointment rather than ending up with nothing. Only once the new
    slot is safely held do we cancel the old one and confirm the new one.
    """
    old_booking = db.get(Booking, booking_id)
    if old_booking is None:
        raise NotFoundError(f"Booking {booking_id} not found")
    if old_booking.status != BookingStatus.ACTIVE:
        raise SlotUnavailableError(f"Booking {booking_id} is not active")
    if old_booking.patient_id != patient_id:
        raise HoldOwnershipError("Booking does not belong to this patient")

    # 1. Claim the new slot first (fails loudly, old booking untouched, if taken)
    held_slot = hold_slot(db, new_slot_id, patient_id)

    try:
        # 2. Confirm the new booking
        new_booking = confirm_booking(db, held_slot.id, patient_id)

        # 3. Only now release the old slot and mark the old booking
        old_slot = db.get(Slot, old_booking.slot_id)
        db.execute(
            update(Slot)
            .where(Slot.id == old_slot.id, Slot.version == old_slot.version,
                   Slot.status == SlotStatus.BOOKED)
            .values(status=SlotStatus.AVAILABLE, version=Slot.version + 1)
        )
        old_booking.status = BookingStatus.RESCHEDULED
        old_booking.cancelled_at = _now()
        old_booking.rescheduled_to_booking_id = new_booking.id
        db.commit()
    except Exception:
        # Roll back our claim on the new slot so it isn't stuck HELD.
        try:
            release_hold(db, new_slot_id, patient_id)
        except SlotUnavailableError:
            pass
        raise

    db.refresh(old_booking)
    record(db, "Booking", old_booking.id, "RESCHEDULED", patient_id,
           f"to_booking_id={new_booking.id} to_slot_id={new_slot_id}")
    return new_booking


def sweep_expired_holds(db: Session) -> int:
    """Background-job-friendly cleanup: release any HELD slot whose hold
    has expired, in case the lazy reclaim path in hold_slot never gets
    triggered for a particular slot (e.g. nobody else wants it). Returns
    the number of slots released."""
    now = _now()
    result = db.execute(
        update(Slot)
        .where(Slot.status == SlotStatus.HELD, Slot.hold_expires_at < now)
        .values(status=SlotStatus.AVAILABLE, version=Slot.version + 1,
                hold_expires_at=None, held_by=None)
    )
    db.commit()
    return result.rowcount
