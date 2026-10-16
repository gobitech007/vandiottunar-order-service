"""Endpoints for other Vandi Ottunar services (payment-service). Callers send X-Service-Token."""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller
from app.core.config import get_settings
from app.db import get_session
from app.models import Booking, BookingStatus
from app.services.bookings import cancel, cancellation_fee, event


async def require_service(x_service_token: str | None = Header(default=None)) -> None:
    expected = get_settings().service_token
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Internal API is disabled (SERVICE_TOKEN not set)")
    if not x_service_token or not hmac.compare_digest(x_service_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid service token")


router = APIRouter(prefix="/internal/bookings", tags=["internal"], dependencies=[Depends(require_service)])


class InternalBooking(BaseModel):
    id: str
    reference: str
    customer_user_id: str
    customer_name: str | None
    customer_phone: str
    status: BookingStatus
    payment_status: str
    fee_paise: int
    cancel_fee_paise: int
    driver_name: str | None


class PaidIn(BaseModel):
    payment_id: str = Field(max_length=64)
    amount_paise: int = Field(ge=0)


def _out(b: Booking) -> InternalBooking:
    return InternalBooking(
        id=b.id,
        reference=b.reference,
        customer_user_id=b.customer_user_id,
        customer_name=b.customer_name,
        customer_phone=b.customer_phone,
        status=b.status,
        payment_status=b.payment_status,
        fee_paise=get_settings().confirmation_fee_paise,
        cancel_fee_paise=cancellation_fee(b),
        driver_name=b.driver_name,
    )


async def _get(session: AsyncSession, booking_id: str) -> Booking:
    b = await session.get(Booking, booking_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return b


@router.get("/{booking_id}", response_model=InternalBooking)
async def get_booking(booking_id: str, session: AsyncSession = Depends(get_session)):
    return _out(await _get(session, booking_id))


@router.post("/{booking_id}/paid", response_model=InternalBooking)
async def mark_paid(booking_id: str, body: PaidIn, session: AsyncSession = Depends(get_session)):
    """The payment-service confirms the booking fee was paid. Safe to call more than once."""
    from app.timeutil import utcnow

    b = await _get(session, booking_id)
    if b.payment_status in ("paid", "waived"):
        return _out(b)
    if body.amount_paise < get_settings().confirmation_fee_paise:
        raise HTTPException(422, "Amount is less than the booking fee")
    b.payment_status = "paid"
    b.payment_id = body.payment_id
    b.paid_at = utcnow()
    event(session, b, Caller(user_id="payment-service", role="system"), "paid", f"₹{body.amount_paise / 100:g} · {body.payment_id}")
    await session.commit()
    return _out(b)


@router.post("/{booking_id}/cancel-paid", response_model=InternalBooking)
async def cancel_paid(booking_id: str, body: PaidIn, session: AsyncSession = Depends(get_session)):
    """The customer paid the cancellation fee: cancel the booking. Safe to call more than once."""
    b = await _get(session, booking_id)
    if b.cancel_fee_status == "paid":
        return _out(b)
    if b.status not in (BookingStatus.pending, BookingStatus.assigned):
        # e.g. the trip started while they were paying: the admin refunds the fee
        raise HTTPException(status.HTTP_409_CONFLICT, f"Booking is {b.status.value}; refund the cancellation fee")
    b.cancel_fee_status = "paid"
    b.cancel_fee_paise = body.amount_paise
    b.cancel_payment_id = body.payment_id
    await cancel(
        session, b, Caller(user_id=b.customer_user_id, role="customer"), "customer",
        f"Cancellation fee ₹{body.amount_paise / 100:g} paid ({body.payment_id})",
    )
    return _out(b)
