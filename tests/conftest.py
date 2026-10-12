import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="vandi-orders-test-")
os.environ.update(
    {
        "ENVIRONMENT": "development",
        # Set TEST_DATABASE_URL to run against PostgreSQL; defaults to a throwaway SQLite file
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_tmp}/test.db"),
        "JWT_SECRET": "test-secret-that-is-long-enough-for-hs256-0123456789",
        "AUTO_ASSIGN_ON_CREATE": "false",
    }
)

from datetime import datetime, timedelta, timezone  # noqa: E402

import jwt  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.users import DriverInfo, UserDirectory, UserInfo, get_directory  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

# RS Puram and Gandhipuram are ~2 km apart in Coimbatore; Chennai is ~400 km away
RS_PURAM = (11.0090, 76.9520)
GANDHIPURAM = (11.0183, 76.9725)
CHENNAI = (13.0827, 80.2707)


def token(user_id: str, role: str) -> dict:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "role": role, "typ": "access", "iat": now, "exp": now + timedelta(hours=1)}
    t = jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {t}"}


def driver(user_id: str, name: str, at=RS_PURAM, pincode="641002", **kw) -> DriverInfo:
    defaults = dict(
        user_id=user_id,
        driver_id=f"p-{user_id}",
        full_name=name,
        phone=f"+9198765{abs(hash(user_id)) % 100000:05d}",
        status="approved",
        home_pincode=pincode,
        home_area="Area",
        home_lat=at[0],
        home_lng=at[1],
        service_radius_km=10,
        two_wheeler=True,
        four_wheeler=True,
        heavy_vehicle=False,
        work_preferences=["on_demand", "job"],
        licence_classes=["LMV", "MCWG"],
        vehicle_types=["car_manual"],
        experience_years=5,
    )
    defaults.update(kw)
    return DriverInfo(**defaults)


class FakeDirectory(UserDirectory):
    def __init__(self):
        self.drivers: list[DriverInfo] = []
        self.users: dict[str, UserInfo] = {}

    async def approved_drivers(self) -> list[DriverInfo]:
        return [d for d in self.drivers if d.status == "approved"]

    async def user(self, user_id: str) -> UserInfo | None:
        return self.users.get(user_id)


@pytest.fixture
def directory():
    fake = FakeDirectory()
    fake.users["cust-1"] = UserInfo(
        user_id="cust-1",
        role="customer",
        phone="+919876600001",
        is_active=True,
        full_name="Priya R",
        home_pincode="641002",
        home_area="RS Puram",
        car_number="TN37AB1234",
    )
    fake.users["biz-1"] = UserInfo(
        user_id="biz-1",
        role="employer",
        phone="+919876600002",
        is_active=True,
        full_name="Arun S",
        company_name="Kovai Logistics",
        home_pincode="641018",
        home_area="Gandhipuram",
    )
    app.dependency_overrides[get_directory] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_directory, None)


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


CUSTOMER = token("cust-1", "customer")
BUSINESS = token("biz-1", "employer")
ADMIN = token("admin-1", "admin")


def future(days: int = 1, hour: int = 9) -> str:
    """A local (India time) date-time string, as a browser form would send it."""
    d = (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.strftime("%Y-%m-%dT%H:%M")
