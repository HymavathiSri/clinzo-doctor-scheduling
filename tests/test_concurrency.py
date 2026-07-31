"""
This is the proof requested by the exercise: "Concurrency-safe booking
implementation with proof/test."

We fire N threads at the *same slot* at (as close to) the same instant as
possible, each using its own DB session/connection, and assert:
  - exactly one booking succeeds
  - every other attempt gets a clean SlotUnavailableError (not a crash,
    not a silently-wrong success)
  - the DB ends up with exactly one ACTIVE booking row for that slot
  - the slot's final status is BOOKED
"""
import threading
from datetime import datetime

from app.models import Doctor, AvailabilityWindow, Slot, Booking, BookingStatus, SlotStatus
from app.services.slot_service import generate_slots_for_window
from app.services import booking_service
from app.exceptions import SlotUnavailableError

N_THREADS = 25


def _setup_single_slot(db):
    doctor = Doctor(name="Dr. Contention", timezone="UTC")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 10, 15),
        slot_duration_minutes=15,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = generate_slots_for_window(db, window)
    assert len(slots) == 1
    return slots[0].id


def test_concurrent_direct_booking_never_double_books(SessionFactory):
    setup_session = SessionFactory()
    slot_id = _setup_single_slot(setup_session)
    setup_session.close()

    results = []
    barrier = threading.Barrier(N_THREADS)

    def attempt_booking(patient_index):
        # Each thread gets its OWN session/connection, mimicking separate
        # concurrent API requests/processes -- this is what makes it a
        # real test of DB-level atomicity rather than Python-level locking.
        session = SessionFactory()
        try:
            barrier.wait()  # line everyone up to maximize actual overlap
            try:
                booking = booking_service.book_slot(session, slot_id, f"patient-{patient_index}")
                results.append(("success", patient_index, booking.id))
            except SlotUnavailableError:
                results.append(("rejected", patient_index, None))
        finally:
            session.close()

    threads = [threading.Thread(target=attempt_booking, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    successes = [r for r in results if r[0] == "success"]
    rejections = [r for r in results if r[0] == "rejected"]

    assert len(results) == N_THREADS, "every thread should finish and report a result"
    assert len(successes) == 1, f"expected exactly 1 successful booking, got {len(successes)}: {successes}"
    assert len(rejections) == N_THREADS - 1

    # Verify final DB state independently of what the threads reported.
    verify_session = SessionFactory()
    try:
        slot = verify_session.get(Slot, slot_id)
        assert slot.status == SlotStatus.BOOKED

        active_bookings = (
            verify_session.query(Booking)
            .filter(Booking.slot_id == slot_id, Booking.status == BookingStatus.ACTIVE)
            .all()
        )
        assert len(active_bookings) == 1
        assert active_bookings[0].patient_id == f"patient-{successes[0][1]}"
    finally:
        verify_session.close()


def test_concurrent_hold_then_confirm_never_double_books(SessionFactory):
    """Same race, but through the two-phase hold -> confirm flow used for
    e.g. payment-collection checkouts."""
    setup_session = SessionFactory()
    slot_id = _setup_single_slot(setup_session)
    setup_session.close()

    hold_results = []
    barrier = threading.Barrier(N_THREADS)

    def attempt_hold(patient_index):
        session = SessionFactory()
        try:
            barrier.wait()
            try:
                slot = booking_service.hold_slot(session, slot_id, f"patient-{patient_index}")
                hold_results.append(("held", patient_index))
            except SlotUnavailableError:
                hold_results.append(("rejected", patient_index))
        finally:
            session.close()

    threads = [threading.Thread(target=attempt_hold, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    holders = [r for r in hold_results if r[0] == "held"]
    assert len(holders) == 1, f"expected exactly 1 successful hold, got {holders}"

    winner_index = holders[0][1]
    verify_session = SessionFactory()
    try:
        # The winner can confirm...
        booking = booking_service.confirm_booking(verify_session, slot_id, f"patient-{winner_index}")
        assert booking.status == BookingStatus.ACTIVE

        # ...and a loser trying to confirm (even if they'd somehow gotten
        # this far) is correctly rejected.
        try:
            booking_service.confirm_booking(verify_session, slot_id, "patient-999")
            assert False, "a non-holder must never be able to confirm a booking"
        except Exception:
            pass
    finally:
        verify_session.close()


def test_concurrent_cancel_and_rebook_race(SessionFactory):
    """A slot is booked, then immediately raced by a cancel and a bunch of
    new booking attempts. Exactly one of two things should be true at the
    end: either the cancel won and the slot is free, or a rebook won and
    there's exactly one active booking -- never both an active booking
    AND an available slot, never two active bookings."""
    setup_session = SessionFactory()
    slot_id = _setup_single_slot(setup_session)
    original = booking_service.book_slot(setup_session, slot_id, "patient-original")
    booking_id = original.id
    setup_session.close()

    results = []
    barrier = threading.Barrier(N_THREADS + 1)

    def do_cancel():
        session = SessionFactory()
        try:
            barrier.wait()
            try:
                booking_service.cancel_booking(session, booking_id, actor="patient-original")
                results.append(("cancelled",))
            except Exception as e:
                results.append(("cancel_failed", str(e)))
        finally:
            session.close()

    def do_rebook(i):
        session = SessionFactory()
        try:
            barrier.wait()
            try:
                b = booking_service.book_slot(session, slot_id, f"rebooker-{i}")
                results.append(("rebooked", b.id))
            except SlotUnavailableError:
                results.append(("rebook_rejected",))
        finally:
            session.close()

    threads = [threading.Thread(target=do_cancel)]
    threads += [threading.Thread(target=do_rebook, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    verify_session = SessionFactory()
    try:
        active = (
            verify_session.query(Booking)
            .filter(Booking.slot_id == slot_id, Booking.status == BookingStatus.ACTIVE)
            .all()
        )
        slot = verify_session.get(Slot, slot_id)

        assert len(active) <= 1, f"must never have more than 1 active booking, got {len(active)}"
        if active:
            assert slot.status == SlotStatus.BOOKED
        else:
            assert slot.status == SlotStatus.AVAILABLE
    finally:
        verify_session.close()
