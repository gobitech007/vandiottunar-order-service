"""Delete trip GPS points and relayed chat messages older than LOCATION_KEEP_DAYS (default 90). Run daily:

    python -m app.purge_locations
"""

import asyncio
from datetime import timedelta

from sqlalchemy import delete

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import BookingLocation, ChatMessage
from app.timeutil import utcnow


async def main() -> int:
    cutoff = utcnow() - timedelta(days=get_settings().location_keep_days)
    async with SessionLocal() as session:
        result = await session.execute(delete(BookingLocation).where(BookingLocation.recorded_at < cutoff))
        chats = await session.execute(delete(ChatMessage).where(ChatMessage.created_at < cutoff))
        await session.commit()
    print(f"Deleted {result.rowcount} location points and {chats.rowcount} chat messages older than {cutoff:%Y-%m-%d}")
    return result.rowcount


if __name__ == "__main__":
    asyncio.run(main())
