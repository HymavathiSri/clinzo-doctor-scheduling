from datetime import datetime, timedelta

import pytest

from app.models import Doctor, AvailabilityWindow, Slot, SlotStatus, Booking, BookingStatus
from app.services.slot_service import generate_slots_for_window, update_availability_window, \
    deactivate_availability_window
from app.services import booking_service
from app.exceptions import SlotUnavailableError, HoldExpiredError, HoldOwnershipError


def setup_window(db, start, end, duration=15, buffer=0):
    doctor = Doctor(name="Dr. Flow", timezone="UTC")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    window = AvailabilityWindow(
        doctor_id=doctor.id, start_utc=start, end_utc=end,
        slot_duration_minutes=duration, buffer_minutes=buffer,
    )
    db.add(window)
    db.commit()
    db.refresh(window)
    slots = generate_slots_for_window(db, window)
    return doctor, window, slots


def test_booking_removes_slot_from_available_list(db):
    from app.services.slot_service import list_available_slots
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 30))
    slot = slots[0]

    booking_service.book_slot(db, slot.id, "patient-1")

    remaining = list_available_slots(db, doctor.id, datetime(2026, 8, 3, 0, 0), datetime(2026, 8, 4, 0, 0))
    assert slot.id not in [s.id for s in remaining]


def test_cancellation_frees_the_slot_immediately(db):
    from app.services.slot_service import list_available_slots
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 15))
    slot = slots[0]

    booking = booking_service.book_slot(db, slot.id, "patient-1")
    booking_service.cancel_booking(db, booking.id, actor="patient-1")

    remaining = list_available_slots(db, doctor.id, datetime(2026, 8, 3, 0, 0), datetime(2026, 8, 4, 0, 0))
    assert slot.id in [s.id for s in remaining]

    refreshed = db.get(Slot, slot.id)
    assert refreshed.status == SlotStatus.AVAILABLE


def test_hold_expires_and_becomes_bookable_again(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 15))
    slot = slots[0]

    booking_service.hold_slot(db, slot.id, "patient-1", hold_seconds=-1)  # already expired

    # A second patient should be able to hold it since the first hold is expired
    booking_service.hold_slot(db, slot.id, "patient-2")
    refreshed = db.get(Slot, slot.id)
    assert refreshed.held_by == "patient-2"


def test_confirm_fails_if_hold_expired(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 15))
    slot = slots[0]

    booking_service.hold_slot(db, slot.id, "patient-1", hold_seconds=-1)

    with pytest.raises(HoldExpiredError):
        booking_service.confirm_booking(db, slot.id, "patient-1")


def test_confirm_fails_for_non_holder(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 15))
    slot = slots[0]

    booking_service.hold_slot(db, slot.id, "patient-1")

    with pytest.raises(HoldOwnershipError):
        booking_service.confirm_booking(db, slot.id, "patient-2")


def test_reschedule_preserves_appointment_if_new_slot_taken(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 45))
    slot_a, slot_b = slots[0], slots[1]

    original_booking = booking_service.book_slot(db, slot_a.id, "patient-1")
    # Someone else grabs slot_b first
    booking_service.book_slot(db, slot_b.id, "patient-2")

    with pytest.raises(SlotUnavailableError):
        booking_service.reschedule_booking(db, original_booking.id, slot_b.id, "patient-1")

    # Original booking must still be active/untouched
    refreshed = db.get(Booking, original_booking.id)
    assert refreshed.status == BookingStatus.ACTIVE
    assert db.get(Slot, slot_a.id).status == SlotStatus.BOOKED


def test_reschedule_success_moves_booking_and_frees_old_slot(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 45))
    slot_a, slot_b = slots[0], slots[1]

    original_booking = booking_service.book_slot(db, slot_a.id, "patient-1")
    new_booking = booking_service.reschedule_booking(db, original_booking.id, slot_b.id, "patient-1")

    assert new_booking.slot_id == slot_b.id
    assert new_booking.status == BookingStatus.ACTIVE

    old_booking = db.get(Booking, original_booking.id)
    assert old_booking.status == BookingStatus.RESCHEDULED
    assert old_booking.rescheduled_to_booking_id == new_booking.id

    assert db.get(Slot, slot_a.id).status == SlotStatus.AVAILABLE
    assert db.get(Slot, slot_b.id).status == SlotStatus.BOOKED


def test_retroactive_shrink_preserves_booked_slots_but_drops_unbooked(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 10, 0))
    # 4 slots: 9:00,9:15,9:30,9:45. Book the last one.
    late_slot = slots[-1]
    booking_service.book_slot(db, late_slot.id, "patient-1")

    # Doctor shrinks window to 9:00-9:30, which would normally drop the
    # 9:45 slot -- but it's booked, so it must survive untouched.
    result = update_availability_window(db, window.id, datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 9, 30))

    assert late_slot.id in result["orphaned_booked_slot_ids"]
    assert late_slot.id not in result["withdrawn_slot_ids"]

    refreshed = db.get(Slot, late_slot.id)
    assert refreshed.status == SlotStatus.BOOKED  # untouched

    active_booking = db.query(Booking).filter(
        Booking.slot_id == late_slot.id, Booking.status == BookingStatus.ACTIVE
    ).first()
    assert active_booking is not None  # patient's appointment preserved


def test_retroactive_shrink_withdraws_unbooked_slots_outside_new_range(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 10, 0))
    unbooked_late_slot = slots[-1]  # 9:45, never booked

    update_availability_window(db, window.id, datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 9, 30))

    refreshed = db.get(Slot, unbooked_late_slot.id)
    assert refreshed.status == SlotStatus.WITHDRAWN


def test_deactivating_window_preserves_booked_appointments(db):
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 9, 30))
    booked_slot, free_slot = slots[0], slots[1]
    booking_service.book_slot(db, booked_slot.id, "patient-1")

    result = deactivate_availability_window(db, window.id)

    assert booked_slot.id in result["orphaned_booked_slot_ids"]
    assert free_slot.id in result["withdrawn_slot_ids"]
    assert db.get(Slot, booked_slot.id).status == SlotStatus.BOOKED
    assert db.get(Slot, free_slot.id).status == SlotStatus.WITHDRAWN


def test_double_confirm_is_rejected(db):
    """Sequential (non-racy) sanity check that a slot can't be booked twice."""
    doctor, window, slots = setup_window(db, datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 15))
    slot = slots[0]

    booking_service.book_slot(db, slot.id, "patient-1")
    with pytest.raises(SlotUnavailableError):
        booking_service.book_slot(db, slot.id, "patient-2")
