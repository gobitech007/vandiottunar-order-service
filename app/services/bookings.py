import asyncio
import secrets
import zlib
from collections import defaultdict
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller
from app.core.config import get_settings
from app.core.users import DriverInfo, UserDirectory
from app.models import ACTIVE_STATUSES, BUSY_STATUSES, Booking, BookingEvent, BookingStatus, BookingType
from app.schemas import BookingCreate
from app.services import chat, matching
from app.timeutil import as_utc, local_to_utc, utcnow

_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I so it can be read over the phone


def new_reference() -> str:
    return "VO" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def event(session: AsyncSession, booking: Booking, actor: Caller | None, action: str, note: str | None = None) -> None:
    session.add(
        BookingEvent(
            booking_id=booking.id,
            actor_user_id=actor.user_id if actor else None,
            actor_role=actor.role if actor else "system",
            action=action,
            note=note,
        )
    )


async def create_booking(session: AsyncSession, caller: Caller, body: BookingCreate, directory: UserDirectory) -> Booking:
    s = get_settings()
    user = await directory.user(caller.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account is not active. Contact support.")

    now = utcnow()
    start = local_to_utc(body.start_at)
    end = local_to_utc(body.end_at) if body.end_at else None
    if start < now - timedelta(minutes=5):
        raise HTTPException(422, "The start date and time must be in the future")
    if start > now + timedelta(days=s.max_days_ahead):
        raise HTTPException(422, f"Bookings can be made up to {s.max_days_ahead} days ahead")
    if end is not None and end <= start:
        raise HTTPException(422, "The end date and time must be after the start")

    car_number = body.car_number or user.car_number
    if body.booking_type == BookingType.on_demand and caller.role == "customer" and not car_number:
        raise HTTPException(422, "Add your car number")
    pincode = body.pickup_pincode or user.home_pincode
    if not pincode:
        raise HTTPException(422, "Add the pickup pincode")

    for _ in range(5):  # retry on the (very unlikely) reference clash
        booking = Booking(
            reference=new_reference(),
            customer_user_id=caller.user_id,
            customer_role=caller.role,
            customer_name=user.full_name,
            customer_phone=user.phone,
            company_name=user.company_name,
            booking_type=body.booking_type,
            vehicle_class=body.vehicle_class,
            car_number=car_number,
            vehicle_note=body.vehicle_note,
            start_at=start,
            end_at=end,
            pickup_address=body.pickup_address,
            pickup_area=body.pickup_area or (user.home_area if not body.pickup_pincode else None),
            pickup_pincode=pincode,
            pickup_lat=body.pickup_lat,
            pickup_lng=body.pickup_lng,
            notes=body.notes,
            status=BookingStatus.pending,
        )
        session.add(booking)
        try:
            await session.flush()
            break
        except IntegrityError:
            await session.rollback()
    else:  # pragma: no cover
        raise HTTPException(500, "Could not create the booking, please try again")
    event(session, booking, caller, "created")
    await session.commit()
    return booking


async def busy_map(session: AsyncSession, driver_user_ids: list[str]) -> dict[str, list[Booking]]:
    """Bookings that keep each driver busy (assigned or on a trip)."""
    if not driver_user_ids:
        return {}
    rows = (
        await session.scalars(
            select(Booking).where(
                Booking.driver_user_id.in_(driver_user_ids), Booking.status.in_(BUSY_STATUSES)
            )
        )
    ).all()
    out: dict[str, list[Booking]] = {}
    for b in rows:
        out.setdefault(b.driver_user_id, []).append(b)
    return out


async def candidates(session: AsyncSession, booking: Booking, directory: UserDirectory) -> list[matching.Candidate]:
    drivers = await directory.approved_drivers()
    busy = await busy_map(session, [d.user_id for d in drivers])
    return matching.evaluate(booking, drivers, busy)


# One lock per driver inside this process; the PostgreSQL advisory lock below covers several processes
_driver_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _lock_driver(session: AsyncSession, driver_user_id: str) -> None:
    """Serialise assignments per driver across processes so two admins can't double-book (PostgreSQL)."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": zlib.crc32(driver_user_id.encode())})


async def assign(
    session: AsyncSession,
    booking: Booking,
    driver: DriverInfo,
    actor: Caller,
    mode: str,
) -> Booking:
    async with _driver_locks[driver.user_id]:
        return await _assign_locked(session, booking, driver, actor, mode)


async def _assign_locked(session: AsyncSession, booking: Booking, driver: DriverInfo, actor: Caller, mode: str) -> Booking:
    await session.refresh(booking)
    if booking.status != BookingStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This booking is already {booking.status.value}")
    await _lock_driver(session, driver.user_id)
    # Re-check with the lock held, against the latest bookings
    busy = await busy_map(session, [driver.user_id])
    check = matching.evaluate(booking, [driver], busy)[0]
    if not check.eligible:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"{driver.full_name or 'This driver'} can't take it: {'; '.join(check.reasons)}")

    now = utcnow()
    result = await session.execute(
        update(Booking)
        .where(Booking.id == booking.id, Booking.status == BookingStatus.pending)
        .values(
            status=BookingStatus.assigned,
            driver_user_id=driver.user_id,
            driver_id=driver.driver_id,
            driver_name=driver.full_name,
            driver_phone=driver.phone,
            assignment_mode=mode,
            assigned_at=now,
            assigned_by=actor.user_id,
            start_code=f"{secrets.randbelow(10_000):04d}",
            code_attempts=0,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Someone else just changed this booking. Refresh and try again.")
    event(session, booking, actor, "assigned", f"{mode}: {driver.full_name} ({driver.phone})")
    await session.commit()
    await session.refresh(booking)
    return booking


async def auto_assign(session: AsyncSession, booking: Booking, actor: Caller, directory: UserDirectory) -> tuple[Booking, str | None]:
    """Assign the best eligible driver. Returns (booking, reason-if-not-assigned)."""
    ranked = await candidates(session, booking, directory)
    if not ranked:
        return booking, "No approved drivers yet"
    for cand in [c for c in ranked if c.eligible][:5]:
        try:
            return await assign(session, booking, cand.driver, actor, "auto"), None
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
            # The driver became busy meanwhile (or the booking changed); reload and try the next one
            await session.refresh(booking)
            if booking.status != BookingStatus.pending:
                return booking, f"Booking is already {booking.status.value}"
    reasons = sorted({r for c in ranked for r in c.reasons})
    return booking, "No eligible driver: " + "; ".join(reasons[:3])


async def unassign(session: AsyncSession, booking: Booking, actor: Caller, note: str | None) -> Booking:
    if booking.status != BookingStatus.assigned:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only assigned bookings can be unassigned")
    previous = booking.driver_name
    booking.status = BookingStatus.pending
    booking.driver_user_id = booking.driver_id = booking.driver_name = booking.driver_phone = None
    booking.assignment_mode = None
    booking.assigned_at = None
    booking.assigned_by = None
    booking.start_code = None  # the next driver gets a new code
    booking.code_attempts = 0
    event(session, booking, actor, "unassigned", note or f"was {previous}")
    await chat.close_session(session, booking, "The driver for this booking has changed.")
    await session.commit()
    return booking


async def cancel(session: AsyncSession, booking: Booking, actor: Caller, by: str, reason: str | None) -> Booking:
    if booking.status not in ACTIVE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This booking is already {booking.status.value}")
    booking.status = BookingStatus.cancelled
    booking.cancelled_by = by
    booking.cancel_reason = reason
    event(session, booking, actor, "cancelled", reason)
    await chat.close_session(session, booking, "This booking was cancelled.")
    await session.commit()
    return booking


async def complete(session: AsyncSession, booking: Booking, actor: Caller) -> Booking:
    """Admin override: finish a booking without the end code (e.g. the driver's phone died)."""
    if booking.status not in BUSY_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only assigned or running bookings can be completed")
    if as_utc(booking.start_at) > utcnow() and booking.status == BookingStatus.assigned:
        raise HTTPException(status.HTTP_409_CONFLICT, "This booking hasn't started yet")
    booking.status = BookingStatus.completed
    booking.completed_at = utcnow()
    booking.start_code = booking.end_code = None
    event(session, booking, actor, "completed", "by admin, without the end code")
    await chat.close_session(session, booking, "The trip has ended.")
    await session.commit()
    return booking
