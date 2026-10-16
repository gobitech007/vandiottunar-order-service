"""Reads drivers and customers from the user-service's /internal API."""

import logging
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException, status

from app.core.config import get_settings

logger = logging.getLogger("vandi.users")


@dataclass
class DriverInfo:
    user_id: str
    driver_id: str
    full_name: str | None
    phone: str
    status: str
    home_pincode: str | None
    home_area: str | None
    home_lat: float | None
    home_lng: float | None
    service_radius_km: int
    two_wheeler: bool
    four_wheeler: bool
    heavy_vehicle: bool
    work_preferences: list[str] = field(default_factory=list)
    licence_classes: list[str] = field(default_factory=list)
    vehicle_types: list[str] = field(default_factory=list)
    experience_years: int | None = None


@dataclass
class UserInfo:
    user_id: str
    role: str
    phone: str
    is_active: bool
    full_name: str | None = None
    company_name: str | None = None
    home_pincode: str | None = None
    home_area: str | None = None
    car_number: str | None = None
    policy_accepted: bool = True  # customers must accept the booking & cancellation policy


class UserDirectory:
    """Interface so tests can swap in an in-memory directory."""

    async def approved_drivers(self) -> list[DriverInfo]:  # pragma: no cover - interface
        raise NotImplementedError

    async def user(self, user_id: str) -> UserInfo | None:  # pragma: no cover - interface
        raise NotImplementedError


class HttpUserDirectory(UserDirectory):
    def _client(self) -> httpx.AsyncClient:
        s = get_settings()
        return httpx.AsyncClient(
            base_url=s.user_service_url.rstrip("/"),
            headers={"X-Service-Token": s.service_token},
            timeout=s.user_service_timeout_s,
        )

    async def _get(self, path: str, **params):
        try:
            async with self._client() as client:
                resp = await client.get(path, params=params or None)
        except httpx.HTTPError:
            logger.exception("User-service unreachable at %s", get_settings().user_service_url)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Driver and customer data is unavailable right now")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.error("User-service %s returned %s: %s", path, resp.status_code, resp.text[:300])
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Driver and customer data is unavailable right now")
        return resp.json()

    async def approved_drivers(self) -> list[DriverInfo]:
        rows = await self._get("/internal/drivers", status="approved") or []
        keys = DriverInfo.__dataclass_fields__.keys()
        return [DriverInfo(**{k: v for k, v in row.items() if k in keys}) for row in rows]

    async def user(self, user_id: str) -> UserInfo | None:
        row = await self._get(f"/internal/users/{user_id}")
        if row is None:
            return None
        keys = UserInfo.__dataclass_fields__.keys()
        return UserInfo(**{k: v for k, v in row.items() if k in keys})


_directory: UserDirectory = HttpUserDirectory()


def get_directory() -> UserDirectory:
    return _directory
