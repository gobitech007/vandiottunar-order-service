from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, driver_only
from app.db import get_session
from app.models import Booking, BookingStatus
from app.schemas import DriverBookingOut
from app.services import bookings as svc

router = APIRouter(prefix="/driver/bookings", tags=["driver"])


@router.get("", response_model=list[DriverBookingOut])
async def my_assignments(caller: Caller = Depends(driver_only), session: AsyncSession = Depends(get_session)):
    """Bookings assigned to this driver, with the customer's contact details."""
    rows = await session.scalars(
        select(Booking)
        .where(
            Booking.driver_user_id == caller.user_id,
            Booking.status.in_([BookingStatus.assigned, BookingStatus.completed]),
        )
        .order_by(Booking.start_at.desc())
        .limit(100)
    )
    return rows.all()


@router.post("/{booking_id}/complete", response_model=DriverBookingOut)
async def complete(booking_id: str, caller: Caller = Depends(driver_only), session: AsyncSession = Depends(get_session)):
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.driver_user_id != caller.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return await svc.complete(session, booking, caller)
