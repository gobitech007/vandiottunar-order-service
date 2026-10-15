import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.timeutil import utcnow


def new_id() -> str:
    return str(uuid.uuid4())


def str_enum(e: type[enum.Enum]) -> Enum:
    return Enum(e, native_enum=False, values_callable=lambda x: [m.value for m in x], length=32)


class BookingType(str, enum.Enum):
    on_demand = "on_demand"  # hourly / daily / outstation trip
    job = "job"  # permanent / full-time driver


class VehicleClass(str, enum.Enum):
    two_wheeler = "two_wheeler"
    four_wheeler = "four_wheeler"
    heavy = "heavy"


class BookingStatus(str, enum.Enum):
    pending = "pending"  # waiting for a driver
    assigned = "assigned"
    in_progress = "in_progress"  # driver entered the customer's start code
    completed = "completed"
    cancelled = "cancelled"


ACTIVE_STATUSES = (BookingStatus.pending, BookingStatus.assigned)  # can still be cancelled
BUSY_STATUSES = (BookingStatus.assigned, BookingStatus.in_progress)  # driver is taken


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (Index("ix_bookings_driver_start", "driver_user_id", "start_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reference: Mapped[str] = mapped_column(String(12), unique=True, index=True)  # short code people can read out

    # Customer (copied at booking time so drivers see it without extra lookups)
    customer_user_id: Mapped[str] = mapped_column(String(36), index=True)
    customer_role: Mapped[str] = mapped_column(String(16))
    customer_name: Mapped[str | None] = mapped_column(String(120))
    customer_phone: Mapped[str] = mapped_column(String(16))
    company_name: Mapped[str | None] = mapped_column(String(160))

    booking_type: Mapped[BookingType] = mapped_column(str_enum(BookingType), default=BookingType.on_demand)
    vehicle_class: Mapped[VehicleClass] = mapped_column(str_enum(VehicleClass), default=VehicleClass.four_wheeler)
    car_number: Mapped[str | None] = mapped_column(String(16))
    vehicle_note: Mapped[str | None] = mapped_column(String(120))

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pickup_address: Mapped[str | None] = mapped_column(String(300))
    pickup_area: Mapped[str | None] = mapped_column(String(120))
    pickup_pincode: Mapped[str] = mapped_column(String(6), index=True)
    pickup_lat: Mapped[float | None] = mapped_column(Float)
    pickup_lng: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    status: Mapped[BookingStatus] = mapped_column(str_enum(BookingStatus), default=BookingStatus.pending, index=True)

    # Driver (copied at assignment time)
    driver_user_id: Mapped[str | None] = mapped_column(String(36))
    driver_id: Mapped[str | None] = mapped_column(String(36))
    driver_name: Mapped[str | None] = mapped_column(String(120))
    driver_phone: Mapped[str | None] = mapped_column(String(16))
    assignment_mode: Mapped[str | None] = mapped_column(String(8))  # manual | auto
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_by: Mapped[str | None] = mapped_column(String(36))

    cancel_reason: Mapped[str | None] = mapped_column(String(300))
    cancelled_by: Mapped[str | None] = mapped_column(String(16))  # customer | admin
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Trip: the customer reads a 4-digit code to the driver to start, and another to end
    start_code: Mapped[str | None] = mapped_column(String(4))
    end_code: Mapped[str | None] = mapped_column(String(4))
    code_attempts: Mapped[int] = mapped_column(Integer, default=0)  # wrong tries on the current code
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Live location while the trip runs (latest point; the whole path is in booking_locations)
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lng: Mapped[float | None] = mapped_column(Float)
    last_accuracy_m: Mapped[float | None] = mapped_column(Float)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Reference point for adding up distance (last_lat/lng) is the last real move, taken at this time
    distance_ref_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    distance_m: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BookingEvent(Base):
    """Audit trail: created, assigned, unassigned, cancelled, completed."""

    __tablename__ = "booking_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    booking_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(36))
    actor_role: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BookingLocation(Base):
    """A GPS point sent by the driver's phone between the start and end codes."""

    __tablename__ = "booking_locations"
    __table_args__ = (Index("ix_booking_locations_booking_time", "booking_id", "recorded_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    booking_id: Mapped[str] = mapped_column(String(36))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    speed_mps: Mapped[float | None] = mapped_column(Float)
    heading: Mapped[float | None] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the phone took the reading
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
