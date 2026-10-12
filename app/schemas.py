import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import BookingStatus, BookingType, VehicleClass


def normalize_car_number(v: str) -> str:
    cleaned = re.sub(r"[\s\-.]", "", v).upper()
    if not (re.match(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{1,4}$", cleaned) or re.match(r"^\d{2}BH\d{4}[A-Z]{1,2}$", cleaned)):
        raise ValueError("Car number should look like TN37AB1234")
    return cleaned


class BookingCreate(BaseModel):
    booking_type: BookingType = BookingType.on_demand
    vehicle_class: VehicleClass = VehicleClass.four_wheeler
    # Date and time the driver is needed. Without an offset it is read as India time.
    start_at: datetime
    end_at: datetime | None = None
    car_number: str | None = Field(default=None, max_length=16)  # defaults to the car number on the profile
    vehicle_note: str | None = Field(default=None, max_length=120)
    pickup_address: str | None = Field(default=None, max_length=300)
    pickup_area: str | None = Field(default=None, max_length=120)
    pickup_pincode: str | None = Field(default=None, pattern=r"^[1-9]\d{5}$")  # defaults to the profile pincode
    pickup_lat: float | None = Field(default=None, ge=-90, le=90)
    pickup_lng: float | None = Field(default=None, ge=-180, le=180)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("car_number")
    @classmethod
    def _car(cls, v: str | None) -> str | None:
        return normalize_car_number(v) if v else None

    @model_validator(mode="after")
    def _coords_together(self):
        if (self.pickup_lat is None) != (self.pickup_lng is None):
            raise ValueError("Send both pickup_lat and pickup_lng, or neither")
        return self


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


class AssignIn(BaseModel):
    driver_user_id: str = Field(min_length=1, max_length=36)


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    reference: str
    booking_type: BookingType
    vehicle_class: VehicleClass
    car_number: str | None
    vehicle_note: str | None
    start_at: datetime
    end_at: datetime | None
    pickup_address: str | None
    pickup_area: str | None
    pickup_pincode: str
    pickup_lat: float | None
    pickup_lng: float | None
    notes: str | None
    status: BookingStatus
    driver_name: str | None
    driver_phone: str | None
    assigned_at: datetime | None
    cancel_reason: str | None
    created_at: datetime


class DriverBookingOut(BookingOut):
    """What a driver sees: the customer they are assigned to."""

    customer_name: str | None
    customer_phone: str
    company_name: str | None


class AdminBookingOut(DriverBookingOut):
    customer_user_id: str
    customer_role: str
    driver_user_id: str | None
    driver_id: str | None
    assignment_mode: str | None
    cancelled_by: str | None
    completed_at: datetime | None


class BookingList(BaseModel):
    items: list[AdminBookingOut]
    total: int


class CandidateOut(BaseModel):
    driver_user_id: str
    driver_id: str
    full_name: str | None
    phone: str
    home_area: str | None
    home_pincode: str | None
    two_wheeler: bool
    four_wheeler: bool
    heavy_vehicle: bool
    licence_classes: list[str]
    experience_years: int | None
    distance_km: float | None
    within_service_area: bool | None
    pincode_match: int
    upcoming: int
    eligible: bool
    reasons: list[str]


class AutoAssignResult(BaseModel):
    booking_id: str
    reference: str
    assigned: bool
    driver_name: str | None = None
    reason: str | None = None


class SummaryOut(BaseModel):
    by_status: dict[str, int]
