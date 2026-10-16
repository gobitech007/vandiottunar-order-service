# Vandi Ottunar — Order Service

FastAPI service for **customer bookings** and **driver assignment**:

- Customers (car owners and businesses) say **when** they need a driver (start date and time, optional end date), their **car number** and where to pick up.
- Admins assign a driver **manually** (from a ranked list) or **automatically** (one booking or all waiting bookings at once).
- Drivers see their assigned customers; customers see their assigned driver.

It has its own database (`vandi_orders`) and reads drivers and customers from the [user-service](https://github.com/gobitech007/vandiottunar-user-service) through its `/internal` API. It doesn't issue logins: it trusts the user-service's tokens, so both services must share the same `JWT_SECRET`.

```
browser / app ──login──▶ user-service (8000) ──token──┐
       │                                              │ same JWT_SECRET
       └────── bookings, assignments ──▶ order-service (8001)
                                              │ X-Service-Token (SERVICE_TOKEN)
                                              └──▶ user-service /internal/drivers, /internal/users/{id}
```

## How a driver is chosen

A driver is **eligible** for a booking when:

1. They are **approved** in the user-service.
2. Their **licence covers the vehicle**: a two-wheeler needs MCWG/MCWOG, a car needs LMV (or a heavy class), and a heavy vehicle needs HMV/HGMV/HPMV/HTV and similar. Classes are read from the licence photo and checked by an admin.
3. They want **this kind of work**: trips (`on_demand`) or a permanent job (`job`).
4. They are **free**: no other assigned booking overlaps. A trip with no end time blocks `DEFAULT_TRIP_HOURS` (12 h). A permanent job with no end date blocks them from then on.

Eligible drivers are ranked by:
- being inside their service radius,
- distance from the pickup point (when both sides have GPS),
- pincode closeness (same pincode, then same first 4 digits, then same first 3),
- fewest upcoming bookings, so work is spread out.

**Auto-assign** gives the booking to the top eligible driver. **Manual assign** lets the admin pick anyone eligible; the same rules are re-checked before saving. Assignments lock per driver, so two admins can't double-book the same person at the same time.

## Fare

Trips (on-demand bookings) are priced at **₹150 per hour** and **₹1000 per 8-hour day** (`HOURLY_RATE_PAISE`, `DAILY_RATE_PAISE`, `DAILY_HOURS`):

- Time is rounded up to whole hours, minimum 1 hour.
- Each full 8 hours costs ₹1000; hours beyond that cost ₹150 each, but never more than one more day (7 hours = ₹1000, not ₹1050).
- Examples: 4 h = ₹600 · 8 h = ₹1000 · 10 h = ₹1300 · 16 h = ₹2000.
- A new customer's **first booking** gets **10% off** (`NEW_CUSTOMER_DISCOUNT_PCT`). "New" means no earlier booking that wasn't cancelled.

Rates and the discount are copied onto the booking when it is made, so later price changes don't change it. Bookings return `rates` and `fare`: an estimate from the booked start and end (`basis: planned`), then the real fare from the start code to the end code (`basis: actual`). A trip with no end time has `rates` only. Permanent jobs aren't priced here.

## Run locally (Windows / PowerShell)

1. In pgAdmin, create an empty database **`vandi_orders`** on the same PostgreSQL server as `vandi_users`.
2. Set up the service:
   ```powershell
   cd vandiottunar-order-service
   copy .env.example .env      # set DATABASE_URL password, and the SAME JWT_SECRET + SERVICE_TOKEN as the user-service
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements-dev.txt
   python -m alembic upgrade head
   python -m uvicorn app.main:app --reload --port 8001
   ```
3. Make sure the user-service's `.env` has the same `SERVICE_TOKEN`, then restart it.

API docs: http://localhost:8001/docs

## API

| Method | Path | Who | What |
|---|---|---|---|
| POST | `/bookings` | customer / employer | Create a booking: `start_at`, optional `end_at`, `car_number` (defaults to profile), `booking_type` (`on_demand` / `job`), `vehicle_class` (`two_wheeler` / `four_wheeler` / `heavy`), pickup address, pincode and optional GPS |
| GET | `/bookings` | customer / employer | My bookings, with the assigned driver's name and phone |
| POST | `/bookings/{id}/cancel` | customer / employer | Cancel |
| GET | `/driver/bookings` | driver | My assigned customers: name, phone, company, car number, pickup, dates |
| POST | `/driver/bookings/{id}/complete` | driver | Mark a started booking done |
| GET | `/admin/bookings?status=&q=` | admin | List / search |
| GET | `/admin/bookings/summary` | admin | Counts by status |
| GET | `/admin/bookings/{id}/candidates` | admin | All approved drivers ranked, with reasons when one can't take it |
| POST | `/admin/bookings/{id}/assign` | admin | Manual assign `{driver_user_id}` |
| POST | `/admin/bookings/{id}/auto-assign` | admin | Auto-assign this booking |
| POST | `/admin/bookings/auto-assign` | admin | Auto-assign every waiting booking, soonest first |
| POST | `/admin/bookings/{id}/unassign` | admin | Take the driver off |
| POST | `/admin/bookings/{id}/cancel` / `complete` | admin | Cancel / complete |

Times without an offset (as browsers send them from a date-time field) are read as India time (`TIMEZONE`).

## Tests

```bash
pytest                                              # SQLite
TEST_DATABASE_URL=postgresql+asyncpg://... pytest   # PostgreSQL
```

The tests swap the user-service for an in-memory directory, so they run on their own.

## Notes

- Customer and driver contact details are **copied onto the booking** when it is made or assigned, so dashboards work even if the user-service is briefly down. A later name or phone change shows on new bookings.
- Tokens are checked by signature only. A driver or customer blocked in the user-service can still reach this service until their token expires, but booking creation re-checks that the account is active.

## Trips: start code, end code, live location

1. When a driver is assigned, the customer's booking shows a **4-digit start code**. The driver can't see it.
2. When the driver arrives, the customer reads it out and the driver enters it: `POST /driver/bookings/{id}/start` (`{"code", "lat", "lng"}`). The booking becomes `in_progress`; the driver can start up to `START_EARLY_MINUTES` before the booked time.
3. The customer now sees an **end code**. The driver's phone sends GPS points (`POST /driver/bookings/{id}/location`, batches of up to 200 so points saved without signal arrive later). Points are only accepted between the two codes.
4. The customer (`GET /bookings/{id}/track`) and admin (`GET /admin/bookings/{id}/track`) see the latest position, the route and the distance driven (GPS jitter and impossible jumps are ignored).
5. At the end, the driver enters the end code: `POST /driver/bookings/{id}/end`. Sharing stops.

After `CODE_MAX_ATTEMPTS` wrong codes the code is replaced (the customer's screen shows the new one). An admin can finish a running trip without the code (`POST /admin/bookings/{id}/complete`) if the driver's phone dies. Run `python -m app.purge_locations` daily (cron / Task Scheduler) to delete GPS points older than `LOCATION_KEEP_DAYS`.

Run `python -m alembic upgrade head` after pulling.

## ₹1 confirmation and WhatsApp chat

- After a driver is assigned, the customer sees only the driver's **name**. They pay ₹1 through the payment-service, which calls `POST /internal/bookings/{id}/paid`. Then the customer's booking shows the **start code** and a **WhatsApp chat link**; the driver can start the trip. Admins can *confirm without payment* (`POST /admin/bookings/{id}/waive-payment`). `REQUIRE_PAYMENT=false` switches the gate off.
- The chat link (`/chat/<encrypted token>`) opens WhatsApp with your business number and a pre-filled message holding the booking reference and a 6-digit **chat PIN**. Nobody sees the other person's number.

### WhatsApp relay

Development (`WHATSAPP_PROVIDER=console`): outgoing messages are printed in this service's log. Pretend a message arrived:

```powershell
Invoke-RestMethod -Method Post http://localhost:8001/whatsapp/dev/inbound -ContentType application/json -Body '{"from_phone": "+919876543210", "text": "hi"}'
```

Going live (`WHATSAPP_PROVIDER=cloud`), Meta WhatsApp Cloud API:
1. Meta Business account → verify the business → WhatsApp → add your business number → note the **Phone number ID**; create a **system user** permanent token with `whatsapp_business_messaging`.
2. App settings → *Basic*: copy the **App secret**. WhatsApp → *Configuration*: webhook URL `https://<order-service domain>/whatsapp/webhook`, verify token = any secret you choose; subscribe to **messages**.
3. WhatsApp Manager → *Message templates*: create a Utility template named `booking_chat_message`, language English, body with 3 variables, e.g. `Booking {{1}}: {{2}} says: {{3}} (reply here to answer)`. Wait for approval.
4. Fill `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_BUSINESS_NUMBER`, `PUBLIC_URL` (HTTPS) and restart.

Why the template: WhatsApp only allows free-form messages within 24 hours of the person's last message to you. The first message to a driver (or a reply after a quiet day) goes out as the template.
