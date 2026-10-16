from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Vandi Ottunar Order Service"
    environment: str = "development"  # development | production
    auto_create_tables: bool = False  # dev convenience; use Alembic in real deployments

    database_url: str = "postgresql+asyncpg://postgres:admin@localhost:5432/vandi_orders"

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
    start_early_minutes: int = 120  # how long before the booked time the driver may start the trip
    code_max_attempts: int = 5  # wrong trip codes before the code is changed
    location_keep_days: int = 90  # GPS points older than this are deleted (python -m app.purge_locations)

    # Booking confirmation: the customer pays this (in paise) to see and chat with the driver
    confirmation_fee_paise: int = 100
    require_payment: bool = True
    # Customer cancellation fee (paise; 4900 = ₹49). When it applies:
    #   assigned = once a driver is assigned (cancelling while we look for a driver is free)
    #   always   = every customer cancellation;  never = no fee
    cancellation_fee_paise: int = 4900
    cancellation_fee_applies: str = "assigned"

    # Public address of this service (chat links point here; WhatsApp calls the webhook here)
    public_url: str = "http://localhost:8001"

    # WhatsApp relay. console = messages are logged (development); cloud = Meta WhatsApp Cloud API
    whatsapp_provider: str = "console"
    whatsapp_business_number: str = "919999999999"  # your WhatsApp Business number, digits only with country code
    whatsapp_phone_number_id: str = ""  # Cloud API: the number's ID in Meta's WhatsApp Manager
    whatsapp_access_token: str = ""  # Cloud API: permanent system-user token
    whatsapp_app_secret: str = ""  # Cloud API: app secret, checks the webhook's X-Hub-Signature-256
    whatsapp_verify_token: str = ""  # any secret string, also entered in Meta when setting up the webhook
    whatsapp_graph_version: str = "v21.0"
    # Approved template used when the other person hasn't written to us in the last 24 hours.
    # Body must have 3 variables: {{1}} booking ref, {{2}} sender ("Customer Priya"), {{3}} the message
    whatsapp_relay_template: str = "booking_chat_message"
    whatsapp_template_language: str = "en"

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
            if self.whatsapp_provider == "cloud" and not (
                self.whatsapp_phone_number_id and self.whatsapp_access_token and self.whatsapp_app_secret and self.whatsapp_verify_token
            ):
                raise RuntimeError("WHATSAPP_* settings are required when WHATSAPP_PROVIDER=cloud")
            if self.whatsapp_provider == "console":
                raise RuntimeError("WHATSAPP_PROVIDER=console is not allowed in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
