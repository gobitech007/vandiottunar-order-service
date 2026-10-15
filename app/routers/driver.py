from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, driver_only
from app.db import get_session
from app.models import Booking, BookingStatus
from app.schemas import DriverBookingOut, LocationBatch, TripCodeIn
from app.services import trips

router = APIRouter(prefix="/driver/bookings", tags=["driver"])


async def _mine(session: AsyncSession, booking_id: str, caller: Caller) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.driver_user_id != caller.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return booking


@router.get("", response_model=list[DriverBookingOut])
async def my_assignments(caller: Caller = Depends(driver_only), session: AsyncSession = Depends(get_session)):
    """Bookings assigned to this driver, with the customer's contact details."""
    rows = await session.scalars(
        select(Booking)
        .where(
            Booking.driver_user_id == caller.user_id,
            Booking.status.in_([BookingStatus.assigned, BookingStatus.in_progress, BookingStatus.completed]),
        )
        .order_by(Booking.start_at.desc())
        .limit(100)
    )
    return rows.all()


@router.post("/{booking_id}/start", response_model=DriverBookingOut)
async def start_trip(
    booking_id: str, body: TripCodeIn, caller: Caller = Depends(driver_only), session: AsyncSession = Depends(get_session)
):
    """Start the trip with the 4-digit start code the customer reads out."""
    return await trips.start(session, await _mine(session, booking_id, caller), caller, body.code, body.lat, body.lng)


@router.post("/{booking_id}/end", response_model=DriverBookingOut)
async def end_trip(
    booking_id: str, body: TripCodeIn, caller: Caller = Depends(driver_only), session: AsyncSession = Depends(get_session)
):
    """Finish the trip with the customer's 4-digit end code."""
    return await trips.end(session, await _mine(session, booking_id, caller), caller, body.code, body.lat, body.lng)


@router.post("/{booking_id}/location", status_code=status.HTTP_204_NO_CONTENT)
async def share_location(
    booking_id: str, body: LocationBatch, caller: Caller = Depends(driver_only), session: AsyncSession = Depends(get_session)
):
    """GPS points from the driver's phone while the trip runs (one or a batch saved during a network drop)."""
    await trips.add_points(session, await _mine(session, booking_id, caller), body.points)
