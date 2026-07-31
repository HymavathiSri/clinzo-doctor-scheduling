from datetime import datetime
from pydantic import BaseModel, Field


class DoctorCreate(BaseModel):
    name: str
    timezone: str = "UTC"


class DoctorOut(BaseModel):
    id: int
    name: str
    timezone: str
    model_config = {"from_attributes": True}


class AvailabilityWindowCreate(BaseModel):
    doctor_id: int
    start_utc: datetime
    end_utc: datetime
    slot_duration_minutes: int = Field(gt=0)
    buffer_minutes: int = Field(default=0, ge=0)
    appointment_type: str = "default"


class AvailabilityWindowUpdate(BaseModel):
    start_utc: datetime
    end_utc: datetime


class SlotOut(BaseModel):
    id: int
    doctor_id: int
    start_utc: datetime
    end_utc: datetime
    status: str
    appointment_type: str
    model_config = {"from_attributes": True}


class HoldRequest(BaseModel):
    patient_id: str
    hold_seconds: int = 300


class ConfirmRequest(BaseModel):
    patient_id: str


class BookRequest(BaseModel):
    patient_id: str


class CancelRequest(BaseModel):
    actor: str = "patient"


class RescheduleRequest(BaseModel):
    patient_id: str
    new_slot_id: int


class BookingOut(BaseModel):
    id: int
    slot_id: int
    patient_id: str
    status: str
    created_at: datetime
    model_config = {"from_attributes": True}
