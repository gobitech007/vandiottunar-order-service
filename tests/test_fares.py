import pytest

from app.services.fares import price
from tests.conftest import ADMIN, CUSTOMER, driver, future




def _total(hours, pct=0):
    return price(hours, 15000, 100000, 8, pct).total_paise


def test_fare_rules():
    assert _total(1) == 15000  # ₹150
    assert _total(4) == 60000  # ₹600
    assert _total(6) == 90000  # ₹900
    assert _total(7) == 100000  # 7 h would be ₹1050: charged as a day, ₹1000
    assert _total(8) == 100000  # one day
    f = price(10, 15000, 100000, 8, 0)
    assert (f.days, f.extra_hours, f.total_paise) == (1, 2, 130000)  # ₹1000 + 2 × ₹150
    assert _total(16) == 200000  # two days
    assert _total(15) == 200000  # 1 day + 7 h → 2 days
    f = price(10, 15000, 100000, 8, 10)
    assert (f.subtotal_paise, f.discount_paise, f.total_paise) == (130000, 13000, 117000)


@pytest.mark.asyncio
async def test_first_booking_gets_discount_and_fare_shows(client, directory):
    start = future(days=1)  # 09:00 India time
    r = await client.post("/bookings", json={"start_at": start, "end_at": start.replace("T09:00", "T19:30")}, headers=CUSTOMER)
    first = r.json()
    assert first["rates"] == {"hourly_rate_paise": 15000, "daily_rate_paise": 100000, "daily_hours": 8, "discount_pct": 10}
    fare = first["fare"]
    # 10 h 30 min → 11 h: 1 day + 3 h = ₹1450, 10% off = ₹1305
    assert (fare["basis"], fare["hours"], fare["days"], fare["extra_hours"]) == ("planned", 11, 1, 3)
    assert (fare["subtotal_paise"], fare["discount_paise"], fare["total_paise"]) == (145000, 14500, 130500)

    # Second booking: no new-customer discount
    second = (await client.post("/bookings", json={"start_at": future(days=3)}, headers=CUSTOMER)).json()
    assert second["rates"]["discount_pct"] == 0
    assert second["fare"] is None  # no end time yet: rates only

    # A customer whose only booking was cancelled is still new
    await client.post(f"/bookings/{first['id']}/cancel", json={}, headers=CUSTOMER)
    await client.post(f"/bookings/{second['id']}/cancel", json={}, headers=CUSTOMER)
    third = (await client.post("/bookings", json={"start_at": future(days=4)}, headers=CUSTOMER)).json()
    assert third["rates"]["discount_pct"] == 10

    # Permanent jobs aren't priced by the hour
    job = (await client.post("/bookings", json={"start_at": future(days=5), "booking_type": "job"}, headers=CUSTOMER)).json()
    assert job["rates"] is None and job["fare"] is None

    # Fare is visible to the driver and admin too
    directory.drivers = [driver("d-1", "Ravi")]
    await client.post(f"/admin/bookings/{third['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN)
    admin = (await client.get(f"/admin/bookings?q={third['reference']}", headers=ADMIN)).json()["items"][0]
    assert admin["rates"]["discount_pct"] == 10


@pytest.mark.asyncio
async def test_policy_lists_rates(client):
    p = (await client.get("/bookings/policy")).json()
    assert (p["hourly_rate_paise"], p["daily_rate_paise"], p["daily_hours"], p["new_customer_discount_pct"]) == (15000, 100000, 8, 10)
