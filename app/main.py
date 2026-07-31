from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .database import get_db, engine, Base
from . import models, schemas
from .services import slot_service, booking_service
from .exceptions import (
    SchedulerError, NotFoundError, SlotUnavailableError,
    HoldExpiredError, HoldOwnershipError, InvalidWindowError,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Clinzo Doctor Slot Scheduling API")

_ERROR_STATUS = {
    NotFoundError: 404,
    SlotUnavailableError: 409,
    HoldExpiredError: 409,
    HoldOwnershipError: 403,
    InvalidWindowError: 422,
}


@app.exception_handler(SchedulerError)
def scheduler_error_handler(request, exc: SchedulerError):
    from fastapi.responses import JSONResponse
    status_code = _ERROR_STATUS.get(type(exc), 400)
    return JSONResponse(status_code=status_code, content={"detail": str(exc)})


# ---------- Doctors & availability ----------

@app.post("/doctors", response_model=schemas.DoctorOut)
def create_doctor(payload: schemas.DoctorCreate, db: Session = Depends(get_db)):
    doctor = models.Doctor(name=payload.name, timezone=payload.timezone)
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    return doctor


@app.post("/availability", response_model=list[schemas.SlotOut])
def create_availability_window(payload: schemas.AvailabilityWindowCreate, db: Session = Depends(get_db)):
    window = models.AvailabilityWindow(**payload.model_dump())
    db.add(window)
    db.commit()
    db.refresh(window)
    slots = slot_service.generate_slots_for_window(db, window)
    return slots


@app.patch("/availability/{window_id}")
def update_availability_window(window_id: int, payload: schemas.AvailabilityWindowUpdate,
                                db: Session = Depends(get_db)):
    return slot_service.update_availability_window(
        db, window_id, payload.start_utc, payload.end_utc, actor="doctor"
    )


@app.delete("/availability/{window_id}")
def remove_availability_window(window_id: int, db: Session = Depends(get_db)):
    return slot_service.deactivate_availability_window(db, window_id, actor="doctor")


# ---------- Slot discovery (patient-facing) ----------

@app.get("/doctors/{doctor_id}/slots", response_model=list[schemas.SlotOut])
def list_slots(
    doctor_id: int,
    date: str = Query(..., description="YYYY-MM-DD, interpreted in `tz`"),
    tz: str = Query("UTC", description="IANA timezone name for interpreting `date`"),
    appointment_type: str = "default",
    db: Session = Depends(get_db),
):
    try:
        zone = ZoneInfo(tz)
        local_day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=zone)
    except Exception as e:
        raise HTTPException(422, f"Invalid date/timezone: {e}")

    day_start_utc = local_day_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    day_end_utc = (local_day_start + timedelta(days=1)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    return slot_service.list_available_slots(db, doctor_id, day_start_utc, day_end_utc, appointment_type)


# ---------- Booking (two-phase: hold -> confirm) ----------

@app.post("/slots/{slot_id}/hold", response_model=schemas.SlotOut)
def hold_slot(slot_id: int, payload: schemas.HoldRequest, db: Session = Depends(get_db)):
    return booking_service.hold_slot(db, slot_id, payload.patient_id, payload.hold_seconds)


@app.post("/slots/{slot_id}/release-hold", response_model=schemas.SlotOut)
def release_hold(slot_id: int, payload: schemas.ConfirmRequest, db: Session = Depends(get_db)):
    return booking_service.release_hold(db, slot_id, payload.patient_id)


@app.post("/slots/{slot_id}/confirm", response_model=schemas.BookingOut)
def confirm_booking(slot_id: int, payload: schemas.ConfirmRequest, db: Session = Depends(get_db)):
    return booking_service.confirm_booking(db, slot_id, payload.patient_id)


# ---------- Booking (single-step) ----------

@app.post("/slots/{slot_id}/book", response_model=schemas.BookingOut)
def book_slot(slot_id: int, payload: schemas.BookRequest, db: Session = Depends(get_db)):
    return booking_service.book_slot(db, slot_id, payload.patient_id)


@app.post("/bookings/{booking_id}/cancel", response_model=schemas.BookingOut)
def cancel_booking(booking_id: int, payload: schemas.CancelRequest, db: Session = Depends(get_db)):
    return booking_service.cancel_booking(db, booking_id, payload.actor)


@app.post("/bookings/{booking_id}/reschedule", response_model=schemas.BookingOut)
def reschedule_booking(booking_id: int, payload: schemas.RescheduleRequest, db: Session = Depends(get_db)):
    return booking_service.reschedule_booking(db, booking_id, payload.new_slot_id, payload.patient_id)


@app.post("/admin/sweep-expired-holds")
def sweep_expired_holds(db: Session = Depends(get_db)):
    return {"released": booking_service.sweep_expired_holds(db)}
