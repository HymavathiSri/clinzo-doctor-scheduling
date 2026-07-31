"""
Turns AvailabilityWindow rows into bookable Slot rows, and handles what
happens to those slots when a doctor edits or removes a window later.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AvailabilityWindow, Slot, SlotStatus
from ..exceptions import InvalidWindowError, NotFoundError
from .audit import record


def generate_slots_for_window(db: Session, window: AvailabilityWindow) -> list[Slot]:
    """
    Slice [window.start_utc, window.end_utc) into fixed-duration slots,
    each followed by `buffer_minutes` of dead time before the next slot
    starts. Idempotent: re-running it for the same window only creates
    slots that don't already exist (matched on doctor+start+type), so it's
    safe to call again after extending a window.
    """
    if window.slot_duration_minutes <= 0:
        raise InvalidWindowError("slot_duration_minutes must be positive")
    if window.end_utc <= window.start_utc:
        raise InvalidWindowError("end_utc must be after start_utc")

    duration = timedelta(minutes=window.slot_duration_minutes)
    buffer = timedelta(minutes=window.buffer_minutes)

    existing_starts = {
        s.start_utc
        for s in db.scalars(
            select(Slot).where(
                Slot.doctor_id == window.doctor_id,
                Slot.appointment_type == window.appointment_type,
                Slot.start_utc >= window.start_utc,
                Slot.start_utc < window.end_utc,
            )
        )
    }

    created = []
    cursor = window.start_utc
    while cursor + duration <= window.end_utc:
        slot_end = cursor + duration
        if cursor not in existing_starts:
            slot = Slot(
                doctor_id=window.doctor_id,
                availability_window_id=window.id,
                start_utc=cursor,
                end_utc=slot_end,
                appointment_type=window.appointment_type,
                status=SlotStatus.AVAILABLE,
            )
            db.add(slot)
            created.append(slot)
        cursor = slot_end + buffer

    db.commit()
    for s in created:
        db.refresh(s)
    return created


def list_available_slots(db: Session, doctor_id: int, day_start_utc, day_end_utc,
                          appointment_type: str = "default") -> list[Slot]:
    """Patient-facing view: only ever returns AVAILABLE slots. HELD and
    BOOKED slots are invisible to patients by construction — there is no
    separate 'filter out booked slots' step to forget."""
    return list(
        db.scalars(
            select(Slot)
            .where(
                Slot.doctor_id == doctor_id,
                Slot.appointment_type == appointment_type,
                Slot.status == SlotStatus.AVAILABLE,
                Slot.start_utc >= day_start_utc,
                Slot.start_utc < day_end_utc,
            )
            .order_by(Slot.start_utc)
        )
    )


def update_availability_window(db: Session, window_id: int, new_start_utc, new_end_utc,
                                actor: str = "doctor") -> dict:
    """
    Handles a doctor shrinking, extending, or shifting a window *after*
    some of its slots may already be booked.

    Policy (see README "Retroactive availability changes" for rationale):
      - Slots that fall outside the new window and are still AVAILABLE or
        HELD-but-expired: deleted outright (nobody holds a claim on them).
      - Slots that fall outside the new window but are BOOKED: never
        auto-cancelled. A confirmed appointment is a commitment to the
        patient; the system will not silently cancel it just because the
        doctor changed their hours. Instead these are flagged as
        'orphaned_bookings' for the doctor/admin to explicitly resolve
        (cancel-with-notice, or honor the appointment as an exception).
      - The window's own start/end are updated, and generate_slots_for_window
        is re-run to backfill any newly-added time.
    """
    window = db.get(AvailabilityWindow, window_id)
    if not window:
        raise NotFoundError(f"AvailabilityWindow {window_id} not found")

    outside = list(
        db.scalars(
            select(Slot).where(
                Slot.availability_window_id == window.id,
                (Slot.start_utc < new_start_utc) | (Slot.start_utc >= new_end_utc),
            )
        )
    )

    removed_slot_ids, orphaned_bookings = [], []
    for slot in outside:
        if slot.status == SlotStatus.BOOKED:
            orphaned_bookings.append(slot.id)
            continue
        slot.status = SlotStatus.WITHDRAWN
        removed_slot_ids.append(slot.id)

    window.start_utc = new_start_utc
    window.end_utc = new_end_utc
    db.commit()

    record(db, "AvailabilityWindow", window.id, "UPDATED", actor,
           f"new_range=({new_start_utc},{new_end_utc}) withdrawn={removed_slot_ids} "
           f"orphaned_bookings_on_slots={orphaned_bookings}")

    newly_created = generate_slots_for_window(db, window)

    return {
        "window_id": window.id,
        "withdrawn_slot_ids": removed_slot_ids,
        "orphaned_booked_slot_ids": orphaned_bookings,
        "newly_created_slot_ids": [s.id for s in newly_created],
    }


def deactivate_availability_window(db: Session, window_id: int, actor: str = "doctor") -> dict:
    """Doctor removes a window entirely. Same policy as above: unbooked
    slots are withdrawn, booked slots and their bookings are preserved
    untouched and surfaced for manual resolution."""
    window = db.get(AvailabilityWindow, window_id)
    if not window:
        raise NotFoundError(f"AvailabilityWindow {window_id} not found")

    window.is_active = False
    slots = list(db.scalars(select(Slot).where(Slot.availability_window_id == window.id)))

    withdrawn, orphaned = [], []
    for slot in slots:
        if slot.status == SlotStatus.BOOKED:
            orphaned.append(slot.id)
        elif slot.status in (SlotStatus.AVAILABLE, SlotStatus.HELD):
            slot.status = SlotStatus.WITHDRAWN
            withdrawn.append(slot.id)

    db.commit()
    record(db, "AvailabilityWindow", window.id, "DEACTIVATED", actor,
           f"withdrawn={withdrawn} orphaned_booked_slot_ids={orphaned}")
    return {"withdrawn_slot_ids": withdrawn, "orphaned_booked_slot_ids": orphaned}
