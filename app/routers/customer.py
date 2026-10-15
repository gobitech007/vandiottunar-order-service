from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, customer_only
from app.core.config import get_settings
from app.core.users import UserDirectory, get_directory
from app.db import get_session
from app.models import Booking
from app.schemas import BookingCreate, CancelIn, CustomerBookingOut, TrackOut
from app.services import bookings as svc
from app.services import trips

router = APIRouter(prefix="/bookings", tags=["customer bookings"])


async def _own_booking(session: AsyncSession, booking_id: str, caller: Caller) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.customer_user_id != caller.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return booking


@router.post("", response_model=CustomerBookingOut, status_code=status.HTTP_201_CREATED)
async def create(
    body: BookingCreate,
    caller: Caller = Depends(customer_only),
    session: AsyncSession = Depends(get_session),
    directory: UserDirectory = Depends(get_directory),
):
    booking = await svc.create_booking(session, caller, body, directory)
    if get_settings().auto_assign_on_create:
        booking, _ = await svc.auto_assign(session, booking, Caller(user_id="system", role="system"), directory)
    return booking


@router.get("", response_model=list[CustomerBookingOut])
async def my_bookings(caller: Caller = Depends(customer_only), session: AsyncSession = Depends(get_session)):
    rows = (
        await session.scalars(
            select(Booking).where(Booking.customer_user_id == caller.user_id).order_by(Booking.start_at.desc()).limit(100)
        )
    ).all()
    if any([trips.ensure_start_code(b) for b in rows]):
        await session.commit()
    return rows


@router.get("/{booking_id}", response_model=CustomerBookingOut)
async def get_one(booking_id: str, caller: Caller = Depends(customer_only), session: AsyncSession = Depends(get_session)):
    booking = await _own_booking(session, booking_id, caller)
    if trips.ensure_start_code(booking):
        await session.commit()
    return booking


@router.get("/{booking_id}/track", response_model=TrackOut)
async def track(booking_id: str, caller: Caller = Depends(customer_only), session: AsyncSession = Depends(get_session)):
    """Where the driver is now, and the route so far. Available from the start code to the end code."""
    return await trips.track(session, await _own_booking(session, booking_id, caller))


@router.post("/{booking_id}/cancel", response_model=CustomerBookingOut)
async def cancel(
    booking_id: str,
    body: CancelIn,
    caller: Caller = Depends(customer_only),
    session: AsyncSession = Depends(get_session),
):
    booking = await _own_booking(session, booking_id, caller)
    return await svc.cancel(session, booking, caller, "customer", body.reason)
