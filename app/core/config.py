from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Vandi Ottunar Order Service"
    environment: str = "development"  # development | production
    auto_create_tables: bool = False  # dev convenience; use Alembic in real deployments

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/vandi_orders"

    # Must be the SAME secret as the user-service: this service trusts its login tokens
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"

    # How this service reads drivers and customers from the user-service
    user_service_url: str = "http://localhost:8000"
    service_token: str = ""  # same value as SERVICE_TOKEN in the user-service
    user_service_timeout_s: float = 5.0

    # Booking rules
    timezone: str = "Asia/Kolkata"
    default_trip_hours: int = 12  # an on-demand booking without an end time blocks the driver this long
    max_days_ahead: int = 90
    auto_assign_on_create: bool = False  # try to assign a driver as soon as a booking is created

    cors_origins: str = "http://localhost:5173"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def validate_for_runtime(self) -> None:
        if self.is_production:
            if self.jwt_secret in ("", "change-me") or len(self.jwt_secret) < 32:
                raise RuntimeError("JWT_SECRET must match the user-service's long random secret in production")
            if len(self.service_token) < 32:
                raise RuntimeError("SERVICE_TOKEN must be set to the user-service's long random value in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
