"""Which drivers can take a booking, and who is the best fit.

A driver is eligible when they are approved, their licence covers the vehicle, they want this kind of
work, and they have no other active booking at an overlapping time. Eligible drivers are ranked by
distance (when both sides have coordinates), then by how close their pincode is, then by how few
upcoming bookings they already have — so work is spread out.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.users import DriverInfo
from app.models import Booking, BookingType, VehicleClass
from app.timeutil import as_utc

FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def window(b: Booking) -> tuple[datetime, datetime]:
    """The time a booking blocks its driver. A permanent job with no end blocks indefinitely."""
    start = as_utc(b.start_at)
    if b.end_at is not None:
        return start, as_utc(b.end_at)
    if b.booking_type == BookingType.job:
        return start, FAR_FUTURE
    return start, start + timedelta(hours=get_settings().default_trip_hours)


def overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def licence_allows(d: DriverInfo, vc: VehicleClass) -> bool:
    return {
        VehicleClass.two_wheeler: d.two_wheeler,
        VehicleClass.four_wheeler: d.four_wheeler,
        VehicleClass.heavy: d.heavy_vehicle,
    }[vc]


@dataclass
class Candidate:
    driver: DriverInfo
    eligible: bool
    reasons: list[str]  # why not eligible (empty when eligible)
    distance_km: float | None
    pincode_match: int  # 3 = same pincode, 2 = same first 4 digits, 1 = same district prefix (3), 0 = none
    within_service_area: bool | None
    upcoming: int  # active bookings the driver already has

    @property
    def sort_key(self):
        dist = self.distance_km if self.distance_km is not None else 10_000.0
        # Prefer: inside service radius, nearest, best pincode match, least busy
        return (
            not self.eligible,
            self.within_service_area is False,
            dist,
            -self.pincode_match,
            self.upcoming,
            (self.driver.full_name or ""),
        )


def _pincode_match(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 0
    if a == b:
        return 3
    if a[:4] == b[:4]:
        return 2
    if a[:3] == b[:3]:
        return 1
    return 0


def evaluate(booking: Booking, drivers: list[DriverInfo], driver_bookings: dict[str, list[Booking]]) -> list[Candidate]:
    want = window(booking)
    out: list[Candidate] = []
    for d in drivers:
        reasons: list[str] = []
        if d.status != "approved":
            reasons.append("Not approved")
        if not licence_allows(d, booking.vehicle_class):
            reasons.append(
                {
                    VehicleClass.two_wheeler: "Licence has no two-wheeler class",
                    VehicleClass.four_wheeler: "Licence has no four-wheeler class",
                    VehicleClass.heavy: "Licence has no heavy-vehicle class",
                }[booking.vehicle_class]
            )
        if d.work_preferences and booking.booking_type.value not in d.work_preferences:
            reasons.append("Wants permanent jobs only" if booking.booking_type == BookingType.on_demand else "Wants trips only")
        busy = [
            b for b in driver_bookings.get(d.user_id, []) if b.id != booking.id and overlaps(window(b), want)
        ]
        if busy:
            reasons.append(f"Busy with booking {busy[0].reference}")

        distance = None
        within = None
        if None not in (booking.pickup_lat, booking.pickup_lng, d.home_lat, d.home_lng):
            distance = round(haversine_km(booking.pickup_lat, booking.pickup_lng, d.home_lat, d.home_lng), 1)
            within = distance <= d.service_radius_km
        out.append(
            Candidate(
                driver=d,
                eligible=not reasons,
                reasons=reasons,
                distance_km=distance,
                pincode_match=_pincode_match(booking.pickup_pincode, d.home_pincode),
                within_service_area=within,
                upcoming=len(driver_bookings.get(d.user_id, [])),
            )
        )
    return sorted(out, key=lambda c: c.sort_key)


def best(candidates: list[Candidate]) -> Candidate | None:
    return next((c for c in candidates if c.eligible), None)
