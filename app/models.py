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
    # Customer cancellation fee: none | paid (the payment-service cancelled the booking after payment)
    cancel_fee_status: Mapped[str] = mapped_column(String(8), default="none")
    cancel_fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    cancel_payment_id: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Booking confirmation payment (₹1 by default) — the driver's details stay hidden until it's paid
    payment_status: Mapped[str] = mapped_column(String(16), default="unpaid")  # unpaid | paid | waived
    payment_id: Mapped[str | None] = mapped_column(String(64))  # payment-service id
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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


class ChatSession(Base):
    """WhatsApp chat between a booking's customer and driver, relayed through the business number."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    booking_id: Mapped[str] = mapped_column(String(36), unique=True)
    pin: Mapped[str] = mapped_column(String(6), index=True)  # in the pre-filled message; picks the booking
    customer_wa: Mapped[str] = mapped_column(String(16))  # +91… registered numbers
    driver_wa: Mapped[str] = mapped_column(String(16))
    # Last message we received from each side: WhatsApp allows free-form replies for 24 hours after it
    customer_last_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    driver_last_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatRoute(Base):
    """Which chat a phone number is talking in right now (a driver may have several bookings)."""

    __tablename__ = "chat_routes"

    phone: Mapped[str] = mapped_column(String(16), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session_time", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(String(36))
    sender: Mapped[str] = mapped_column(String(16))  # customer | driver | system
    body: Mapped[str] = mapped_column(Text)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)  # inbound id, to ignore repeats
    delivery: Mapped[str] = mapped_column(String(16), default="sent")  # sent | template | failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
