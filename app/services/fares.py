"""Driver fare: hourly rate, a day rate for a full working day, and a first-booking discount.

Rules (amounts in paise, from settings, copied onto each booking when it is made):
- Time is rounded up to whole hours (minimum 1 hour).
- Every full working day (DAILY_HOURS, 8 h) costs the day rate (₹1000).
- Hours beyond the full days cost the hourly rate (₹150/h), but never more than one more day rate:
  7 hours would be ₹1050, so it is charged as one day (₹1000).
- A new customer's first booking gets NEW_CUSTOMER_DISCOUNT_PCT (10%) off.

Only on-demand trips are priced here; permanent jobs are paid as agreed with the driver.
The estimate uses the booked start and end; once the trip is over, the fare uses the real start and end codes.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from app.models import Booking, BookingType
from app.timeutil import as_utc


@dataclass
class Fare:
    basis: str  # planned (booked times) | actual (start code to end code)
    hours: int
    days: int  # full working days charged at the day rate
    extra_hours: int  # hours charged at the hourly rate
    hourly_rate_paise: int
    daily_rate_paise: int
    daily_hours: int
    subtotal_paise: int
    discount_pct: int
    discount_paise: int
    total_paise: int


def billable_hours(start: datetime, end: datetime) -> int:
    minutes = (as_utc(end) - as_utc(start)).total_seconds() / 60
    return max(1, math.ceil(minutes / 60 - 1e-9))


def price(hours: int, hourly: int, daily: int, daily_hours: int, discount_pct: int, basis: str = "planned") -> Fare:
    days, extra = divmod(hours, daily_hours) if daily_hours > 0 else (0, hours)
    if daily and extra * hourly >= daily:  # leftover hours cost a day or more: charge one more day instead
        days, extra = days + 1, 0
    subtotal = days * daily + extra * hourly
    discount = round(subtotal * discount_pct / 100)
    return Fare(
        basis=basis,
        hours=hours,
        days=days,
        extra_hours=extra,
        hourly_rate_paise=hourly,
        daily_rate_paise=daily,
        daily_hours=daily_hours,
        subtotal_paise=subtotal,
        discount_pct=discount_pct,
        discount_paise=discount,
        total_paise=subtotal - discount,
    )


def booking_fare(b: Booking) -> Fare | None:
    """The fare for a booking: actual once the trip has ended, otherwise the estimate from the booked times.
    None when there is nothing to price yet (no end time) or it is a permanent job."""
    if b.booking_type != BookingType.on_demand or not b.rate_hourly_paise:
        return None
    if b.started_at and b.completed_at:
        start, end, basis = b.started_at, b.completed_at, "actual"
    elif b.end_at:
        start, end, basis = b.start_at, b.end_at, "planned"
    else:
        return None
    return price(
        billable_hours(start, end), b.rate_hourly_paise, b.rate_daily_paise, b.rate_daily_hours, b.discount_pct, basis
    )
