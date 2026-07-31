from datetime import datetime, timedelta

from app.models import Doctor, AvailabilityWindow, Slot
from app.services.slot_service import generate_slots_for_window
from app.exceptions import InvalidWindowError
import pytest


def make_doctor(db):
    doc = Doctor(name="Dr. Rao", timezone="UTC")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def test_basic_slot_generation_no_buffer(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 11, 0),
        slot_duration_minutes=15,
        buffer_minutes=0,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = generate_slots_for_window(db, window)

    assert len(slots) == 4
    starts = [s.start_utc for s in slots]
    assert starts == [
        datetime(2026, 8, 3, 10, 0),
        datetime(2026, 8, 3, 10, 15),
        datetime(2026, 8, 3, 10, 30),
        datetime(2026, 8, 3, 10, 45),
    ]
    # last slot ends exactly at window end, no overrun
    assert slots[-1].end_utc == datetime(2026, 8, 3, 11, 0)


def test_slot_generation_with_buffer(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 11, 0),
        slot_duration_minutes=15,
        buffer_minutes=5,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = generate_slots_for_window(db, window)

    # 15 min slot + 5 min buffer = 20 min cadence -> fits 3 full slots in 60 min
    # (10:00-10:15, 10:20-10:35, 10:40-10:55); a 4th would need buffer+slot
    # room that doesn't exist before 11:00.
    assert len(slots) == 3
    starts = [s.start_utc for s in slots]
    assert starts == [
        datetime(2026, 8, 3, 10, 0),
        datetime(2026, 8, 3, 10, 20),
        datetime(2026, 8, 3, 10, 40),
    ]
    for s in slots:
        assert s.end_utc - s.start_utc == timedelta(minutes=15)


def test_window_not_evenly_divisible_no_partial_slots(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 10, 50),  # 50 min window, 15 min slots
        slot_duration_minutes=15,
        buffer_minutes=0,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = generate_slots_for_window(db, window)
    # 50 / 15 = 3 full slots (45 min), remaining 5 min dropped, never a
    # partial/short slot
    assert len(slots) == 3
    assert slots[-1].end_utc == datetime(2026, 8, 3, 10, 45)


def test_generation_is_idempotent(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 11, 0),
        slot_duration_minutes=15,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    first = generate_slots_for_window(db, window)
    second = generate_slots_for_window(db, window)  # e.g. retried request

    assert len(first) == 4
    assert len(second) == 0  # nothing new created, no duplicates

    total_in_db = db.query(Slot).filter(Slot.doctor_id == doctor.id).count()
    assert total_in_db == 4


def test_invalid_window_rejected(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 11, 0),
        end_utc=datetime(2026, 8, 3, 10, 0),  # end before start
        slot_duration_minutes=15,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    with pytest.raises(InvalidWindowError):
        generate_slots_for_window(db, window)
