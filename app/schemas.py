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


class TripCodeIn(BaseModel):
    code: str = Field(pattern=r"^\s*\d{4}\s*$")
    # Where the driver is when starting/ending (optional, recorded on the trip)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class LocationPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100_000)
    speed_mps: float | None = Field(default=None, ge=0, le=150)
    heading: float | None = Field(default=None, ge=0, le=360)
    recorded_at: datetime | None = None  # when the phone took the reading (sent in batches after a network drop)


class LocationBatch(BaseModel):
    points: list[LocationPoint] = Field(min_length=1, max_length=200)


class TrackPoint(BaseModel):
    lat: float
    lng: float
    recorded_at: datetime
    accuracy_m: float | None = None


class TrackOut(BaseModel):
    booking_id: str
    status: BookingStatus
    started_at: datetime | None
    completed_at: datetime | None
    distance_km: float
    last: TrackPoint | None
    path: list[tuple[float, float]]
    pickup_lat: float | None
    pickup_lng: float | None
    driver_name: str | None


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
    assigned_at: datetime | None
    payment_status: str = "unpaid"  # unpaid | paid | waived
    paid_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    distance_m: float = 0.0
    last_location_at: datetime | None = None
    cancel_reason: str | None
    created_at: datetime


class CustomerBookingOut(BookingOut):
    """What the customer sees. Never the driver's phone: they chat through the WhatsApp relay link,
    which (like the start code) appears only after the booking fee is paid."""

    start_code: str | None = None
    end_code: str | None = None
    fee_paise: int = 0
    chat_url: str | None = None
    # What cancelling now costs (0 = free). Above 0, cancel by paying it through the payment-service
    cancellation_fee_paise: int = 0
    # After a paid cancellation: status "paid" and the amount paid
    cancel_fee_status: str = "none"
    cancel_fee_paise: int = 0


class PolicyOut(BaseModel):
    confirmation_fee_paise: int
    cancellation_fee_paise: int
    cancellation_fee_applies: str  # assigned | always | never


class DriverBookingOut(BookingOut):
    """What a driver sees: the customer they are assigned to."""

    customer_name: str | None
    customer_phone: str
    company_name: str | None
    chat_url: str | None = None


class AdminBookingOut(DriverBookingOut):
    customer_user_id: str
    customer_role: str
    driver_user_id: str | None
    driver_id: str | None
    driver_phone: str | None
    payment_id: str | None = None
    assignment_mode: str | None
    cancelled_by: str | None
    cancel_fee_status: str = "none"
    cancel_fee_paise: int = 0
    cancel_payment_id: str | None = None


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


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sender: str
    body: str
    delivery: str
    created_at: datetime
