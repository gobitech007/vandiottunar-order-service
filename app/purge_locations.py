"""Delete trip GPS points older than LOCATION_KEEP_DAYS (default 90). Run daily, e.g. from cron:

    python -m app.purge_locations
"""

import asyncio
from datetime import timedelta

from sqlalchemy import delete

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import BookingLocation
from app.timeutil import utcnow


async def main() -> int:
    cutoff = utcnow() - timedelta(days=get_settings().location_keep_days)
    async with SessionLocal() as session:
        result = await session.execute(delete(BookingLocation).where(BookingLocation.recorded_at < cutoff))
        await session.commit()
    print(f"Deleted {result.rowcount} location points older than {cutoff:%Y-%m-%d}")
    return result.rowcount


if __name__ == "__main__":
    asyncio.run(main())
