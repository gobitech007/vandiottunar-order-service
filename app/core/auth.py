"""Login tokens are issued by the user-service; this service only checks them (same JWT_SECRET)."""

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Caller:
    user_id: str
    role: str  # driver | customer | employer | admin


def current_caller(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Caller:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in again", {"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    s = get_settings()
    try:
        payload = jwt.decode(creds.credentials, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.InvalidTokenError:
        raise unauthorized
    if payload.get("typ") != "access" or not payload.get("sub") or not payload.get("role"):
        raise unauthorized
    return Caller(user_id=payload["sub"], role=payload["role"])


def require_role(*roles: str):
    def checker(caller: Caller = Depends(current_caller)) -> Caller:
        if caller.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have access to this")
        return caller

    return checker


customer_only = require_role("customer", "employer")
driver_only = require_role("driver")
admin_only = require_role("admin")
