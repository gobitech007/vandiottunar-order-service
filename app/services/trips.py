"""Trips: start and end codes, and the driver's live location in between.

The customer sees a 4-digit start code once a driver is assigned and reads it to the driver when the trip
begins; the driver types it in to start. An end code then appears for the customer, and the driver types
that to finish. Location points are only accepted between the two codes.
"""

import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller
from app.core.config import get_settings
from app.models import Booking, BookingLocation, BookingStatus
from app.schemas import LocationPoint, TrackOut, TrackPoint
from app.services import chat
from app.services.bookings import event
from app.services.matching import haversine_km
from app.timeutil import as_utc, utcnow

MAX_PATH_POINTS = 500
GOOD_ACCURACY_M = 100  # readings less precise than this are shown but don't count towards distance
MIN_STEP_M = 15  # ignore GPS jitter while standing still
MAX_SPEED_KMH = 200  # a jump faster than this is a bad reading


def new_code() -> str:
    return f"{secrets.randbelow(10_000):04d}"


def ensure_start_code(booking: Booking) -> bool:
    """Bookings assigned before codes existed get one when the customer next looks. True if changed."""
    if booking.status == BookingStatus.assigned and not booking.start_code:
        booking.start_code = new_code()
        booking.code_attempts = 0
        return True
    return False


async def _check_code(session: AsyncSession, booking: Booking, expected: str | None, given: str, which: str) -> None:
    max_attempts = get_settings().code_max_attempts
    if expected and hmac.compare_digest(expected, given.strip()):
        booking.code_attempts = 0
        return
    booking.code_attempts = (booking.code_attempts or 0) + 1
    if booking.code_attempts >= max_attempts:
        # Too many guesses: replace the code. The customer's screen shows the new one.
        if which == "start":
            booking.start_code = new_code()
        else:
            booking.end_code = new_code()
        booking.code_attempts = 0
        event(session, booking, None, f"{which}_code_reset", f"{max_attempts} wrong codes")
        await session.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many wrong codes. The customer's {which} code has been changed: ask them for the new one.",
        )
    left = max_attempts - booking.code_attempts
    await session.commit()
    raise HTTPException(422, f"Wrong code. Ask the customer for their {which} code ({left} tries left).")


def _note_position(booking: Booking, lat: float | None, lng: float | None) -> str | None:
    return f"at {lat:.5f},{lng:.5f}" if lat is not None and lng is not None else None


async def start(session: AsyncSession, booking: Booking, caller: Caller, code: str, lat: float | None, lng: float | None) -> Booking:
    if booking.status != BookingStatus.assigned:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This booking is {booking.status.value}, it can't be started")
    if not chat.is_confirmed(booking):
        raise HTTPException(status.HTTP_409_CONFLICT, "The customer hasn't confirmed this booking yet (₹1 payment). You'll see it here when they do.")
    earliest = as_utc(booking.start_at) - timedelta(minutes=get_settings().start_early_minutes)
    if utcnow() < earliest:
        raise HTTPException(status.HTTP_409_CONFLICT, "It's too early to start this trip")
    ensure_start_code(booking)
    await _check_code(session, booking, booking.start_code, code, "start")
    now = utcnow()
    booking.status = BookingStatus.in_progress
    booking.started_at = now
    booking.start_code = None
    booking.end_code = new_code()
    booking.distance_m = 0.0
    if lat is not None and lng is not None:
        _record(session, booking, LocationPoint(lat=lat, lng=lng, recorded_at=now))
    event(session, booking, caller, "started", _note_position(booking, lat, lng))
    await session.commit()
    return booking


async def end(session: AsyncSession, booking: Booking, caller: Caller, code: str, lat: float | None, lng: float | None) -> Booking:
    if booking.status != BookingStatus.in_progress:
        raise HTTPException(status.HTTP_409_CONFLICT, "This trip hasn't started")
    await _check_code(session, booking, booking.end_code, code, "end")
    now = utcnow()
    if lat is not None and lng is not None:
        _record(session, booking, LocationPoint(lat=lat, lng=lng, recorded_at=now))
    booking.status = BookingStatus.completed
    booking.completed_at = now
    booking.end_code = None
    event(session, booking, caller, "completed", _note_position(booking, lat, lng))
    await chat.close_session(session, booking, "The trip has ended. Thank you for riding with Vandi Ottunar.")
    await session.commit()
    return booking


def _record(session: AsyncSession, booking: Booking, p: LocationPoint) -> None:
    """Store one point and add to the trip distance when it's a plausible move."""
    at = as_utc(p.recorded_at) if p.recorded_at else utcnow()
    good = p.accuracy_m is None or p.accuracy_m <= GOOD_ACCURACY_M
    ref_at = booking.distance_ref_at or booking.last_location_at
    if good and booking.last_lat is not None and ref_at is not None:
        step_km = haversine_km(booking.last_lat, booking.last_lng, p.lat, p.lng)
        hours = max((at - as_utc(ref_at)).total_seconds(), 1) / 3600
        if step_km * 1000 >= MIN_STEP_M and step_km / hours <= MAX_SPEED_KMH:
            booking.distance_m = (booking.distance_m or 0) + step_km * 1000
        elif step_km * 1000 < MIN_STEP_M:
            good = False  # standing still: keep the earlier point as the reference
    session.add(
        BookingLocation(
            booking_id=booking.id,
            lat=p.lat,
            lng=p.lng,
            accuracy_m=p.accuracy_m,
            speed_mps=p.speed_mps,
            heading=p.heading,
            recorded_at=at,
        )
    )
    if good or booking.last_lat is None:
        booking.last_lat, booking.last_lng = p.lat, p.lng
        booking.distance_ref_at = at
    booking.last_accuracy_m = p.accuracy_m
    booking.last_location_at = at


async def add_points(session: AsyncSession, booking: Booking, points: list[LocationPoint]) -> Booking:
    if booking.status != BookingStatus.in_progress:
        raise HTTPException(status.HTTP_409_CONFLICT, "Location is only shared while the trip is running")
    now = utcnow()
    started = as_utc(booking.started_at) if booking.started_at else now
    for p in sorted(points, key=lambda x: x.recorded_at or now):
        at = as_utc(p.recorded_at) if p.recorded_at else now
        if at < started - timedelta(minutes=1) or at > now + timedelta(minutes=2):
            continue  # from before the trip or a phone clock far in the future
        if booking.last_location_at and at <= as_utc(booking.last_location_at):
            continue  # duplicate or out of order (resent after a network drop)
        _record(session, booking, p)
    await session.commit()
    return booking


async def track(session: AsyncSession, booking: Booking) -> TrackOut:
    if booking.status not in (BookingStatus.in_progress, BookingStatus.completed):
        raise HTTPException(status.HTTP_409_CONFLICT, "Tracking starts when the driver enters the start code")
    rows = (
        await session.execute(
            select(BookingLocation.lat, BookingLocation.lng, BookingLocation.recorded_at)
            .where(BookingLocation.booking_id == booking.id)
            .order_by(BookingLocation.recorded_at)
        )
    ).all()
    step = max(1, -(-len(rows) // MAX_PATH_POINTS))  # thin long trips, always keeping the latest point
    picked = rows[::step]
    if rows and picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    last = None
    if booking.last_location_at is not None:
        last = TrackPoint(
            lat=rows[-1].lat if rows else booking.last_lat,
            lng=rows[-1].lng if rows else booking.last_lng,
            recorded_at=as_utc(rows[-1].recorded_at) if rows else as_utc(booking.last_location_at),
            accuracy_m=booking.last_accuracy_m,
        )
    return TrackOut(
        booking_id=booking.id,
        status=booking.status,
        started_at=booking.started_at,
        completed_at=booking.completed_at,
        distance_km=round((booking.distance_m or 0) / 1000, 2),
        last=last,
        path=[(r.lat, r.lng) for r in picked],
        pickup_lat=booking.pickup_lat,
        pickup_lng=booking.pickup_lng,
        driver_name=booking.driver_name,
    )


def stale_seconds(booking: Booking, now: datetime | None = None) -> int | None:
    if booking.last_location_at is None:
        return None
    return int(((now or utcnow()) - as_utc(booking.last_location_at)).total_seconds())
