from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, admin_only
from app.core.users import UserDirectory, get_directory
from app.db import get_session
from app.models import Booking, BookingStatus
from app.schemas import (
    AdminBookingOut,
    AssignIn,
    AutoAssignResult,
    BookingList,
    CancelIn,
    CandidateOut,
    SummaryOut,
)
from app.services import bookings as svc
from app.services.matching import Candidate

router = APIRouter(prefix="/admin/bookings", tags=["admin"])


async def _booking(session: AsyncSession, booking_id: str) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return booking


def _candidate_out(c: Candidate) -> CandidateOut:
    d = c.driver
    return CandidateOut(
        driver_user_id=d.user_id,
        driver_id=d.driver_id,
        full_name=d.full_name,
        phone=d.phone,
        home_area=d.home_area,
        home_pincode=d.home_pincode,
        two_wheeler=d.two_wheeler,
        four_wheeler=d.four_wheeler,
        heavy_vehicle=d.heavy_vehicle,
        licence_classes=d.licence_classes,
        experience_years=d.experience_years,
        distance_km=c.distance_km,
        within_service_area=c.within_service_area,
        pincode_match=c.pincode_match,
        upcoming=c.upcoming,
        eligible=c.eligible,
        reasons=c.reasons,
    )


@router.get("", response_model=BookingList)
async def list_bookings(
    status_filter: BookingStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=60),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: Caller = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Booking)
    if status_filter:
        stmt = stmt.where(Booking.status == status_filter)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Booking.reference.ilike(like),
                Booking.customer_name.ilike(like),
                Booking.customer_phone.ilike(like),
                Booking.company_name.ilike(like),
                Booking.car_number.ilike(like),
                Booking.pickup_pincode.ilike(like),
                Booking.driver_name.ilike(like),
            )
        )
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    # Waiting bookings: soonest first. Everything else: newest first.
    order = Booking.start_at.asc() if status_filter == BookingStatus.pending else Booking.start_at.desc()
    rows = (await session.scalars(stmt.order_by(order).limit(limit).offset(offset))).all()
    return BookingList(items=[AdminBookingOut.model_validate(b) for b in rows], total=total or 0)


@router.get("/summary", response_model=SummaryOut)
async def summary(_: Caller = Depends(admin_only), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Booking.status, func.count()).group_by(Booking.status))).all()
    counts = {(s.value if hasattr(s, "value") else s): n for s, n in rows}
    return SummaryOut(by_status={s.value: counts.get(s.value, 0) for s in BookingStatus})


@router.post("/auto-assign", response_model=list[AutoAssignResult])
async def auto_assign_all(
    limit: int = Query(default=50, ge=1, le=200),
    caller: Caller = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
    directory: UserDirectory = Depends(get_directory),
):
    """Try to give every waiting booking a driver, soonest booking first."""
    pending = (
        await session.scalars(
            select(Booking).where(Booking.status == BookingStatus.pending).order_by(Booking.start_at.asc()).limit(limit)
        )
    ).all()
    results = []
    for booking in pending:
        booking, reason = await svc.auto_assign(session, booking, caller, directory)
        results.append(
            AutoAssignResult(
                booking_id=booking.id,
                reference=booking.reference,
                assigned=booking.status == BookingStatus.assigned,
                driver_name=booking.driver_name,
                reason=reason,
            )
        )
    return results


@router.get("/{booking_id}", response_model=AdminBookingOut)
async def get_one(booking_id: str, _: Caller = Depends(admin_only), session: AsyncSession = Depends(get_session)):
    return await _booking(session, booking_id)


@router.get("/{booking_id}/candidates", response_model=list[CandidateOut])
async def candidates(
    booking_id: str,
    _: Caller = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
    directory: UserDirectory = Depends(get_directory),
):
    """Every approved driver, best fit first, with the reasons any of them can't take this booking."""
    booking = await _booking(session, booking_id)
    return [_candidate_out(c) for c in await svc.candidates(session, booking, directory)]


@router.post("/{booking_id}/assign", response_model=AdminBookingOut)
async def assign(
    booking_id: str,
    body: AssignIn,
    caller: Caller = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
    directory: UserDirectory = Depends(get_directory),
):
    booking = await _booking(session, booking_id)
    driver = next((d for d in await directory.approved_drivers() if d.user_id == body.driver_user_id), None)
    if driver is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Driver not found or not approved")
    return await svc.assign(session, booking, driver, caller, "manual")


@router.post("/{booking_id}/auto-assign", response_model=AutoAssignResult)
async def auto_assign_one(
    booking_id: str,
    caller: Caller = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
    directory: UserDirectory = Depends(get_directory),
):
    booking = await _booking(session, booking_id)
    if booking.status != BookingStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This booking is already {booking.status.value}")
    booking, reason = await svc.auto_assign(session, booking, caller, directory)
    return AutoAssignResult(
        booking_id=booking.id,
        reference=booking.reference,
        assigned=booking.status == BookingStatus.assigned,
        driver_name=booking.driver_name,
        reason=reason,
    )


@router.post("/{booking_id}/unassign", response_model=AdminBookingOut)
async def unassign(
    booking_id: str, body: CancelIn, caller: Caller = Depends(admin_only), session: AsyncSession = Depends(get_session)
):
    return await svc.unassign(session, await _booking(session, booking_id), caller, body.reason)


@router.post("/{booking_id}/cancel", response_model=AdminBookingOut)
async def cancel(
    booking_id: str, body: CancelIn, caller: Caller = Depends(admin_only), session: AsyncSession = Depends(get_session)
):
    return await svc.cancel(session, await _booking(session, booking_id), caller, "admin", body.reason)


@router.post("/{booking_id}/complete", response_model=AdminBookingOut)
async def complete(booking_id: str, caller: Caller = Depends(admin_only), session: AsyncSession = Depends(get_session)):
    return await svc.complete(session, await _booking(session, booking_id), caller)
