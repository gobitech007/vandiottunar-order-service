import asyncio

import pytest

from tests.conftest import ADMIN, BUSINESS, CHENNAI, CUSTOMER, GANDHIPURAM, SERVICE, driver, future, token

pytestmark = pytest.mark.asyncio


async def book(client, headers=CUSTOMER, **kw):
    body = {"start_at": future(), **kw}
    return await client.post("/bookings", json=body, headers=headers)


async def test_customer_books_with_profile_defaults(client, directory):
    r = await book(client, end_at=future(days=2), notes="Airport drop")
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["status"] == "pending" and b["reference"].startswith("VO")
    assert b["car_number"] == "TN37AB1234" and b["pickup_pincode"] == "641002" and b["pickup_area"] == "RS Puram"
    # The browser sent 09:00 India time, stored as 03:30 UTC
    assert b["start_at"].endswith(("03:30:00Z", "03:30:00+00:00", "03:30:00"))

    mine = (await client.get("/bookings", headers=CUSTOMER)).json()
    assert [x["id"] for x in mine] == [b["id"]]


async def test_booking_validation(client, directory):
    assert (await book(client, start_at="2020-01-01T09:00")).status_code == 422
    r = await book(client, start_at=future(days=2), end_at=future(days=1))
    assert r.status_code == 422 and "after the start" in r.json()["detail"]
    assert (await book(client, start_at=future(days=200))).status_code == 422
    assert (await book(client, car_number="not a plate")).status_code == 422
    assert (await book(client, pickup_lat=11.0)).status_code == 422  # lat without lng
    r = await book(client, car_number="tn 37 ab 1234")
    assert r.json()["car_number"] == "TN37AB1234"


async def test_customer_without_car_number_must_give_one(client, directory):
    directory.users["cust-1"].car_number = None
    r = await book(client)
    assert r.status_code == 422 and "car number" in r.json()["detail"]
    assert (await book(client, car_number="22BH1234AA")).status_code == 201


async def test_roles_and_ownership(client, directory):
    b = (await book(client)).json()
    assert (await client.post("/bookings", json={"start_at": future()}, headers=token("d-1", "driver"))).status_code == 403
    assert (await client.get(f"/bookings/{b['id']}", headers=BUSINESS)).status_code == 404
    assert (await client.get("/admin/bookings", headers=CUSTOMER)).status_code == 403
    assert (await client.get("/bookings")).status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert (await client.get("/bookings", headers=bad)).status_code == 401


async def test_auto_assign_picks_nearest_eligible_driver(client, directory):
    directory.drivers = [
        driver("d-far", "Far Driver", at=CHENNAI, pincode="600001"),
        driver("d-near", "Near Driver", at=GANDHIPURAM, pincode="641012"),
        driver("d-nolmv", "Bike Only", four_wheeler=False, licence_classes=["MCWG"]),
        driver("d-jobs", "Jobs Only", work_preferences=["job"]),
    ]
    b = (await book(client, pickup_lat=11.0090, pickup_lng=76.9520)).json()

    cands = (await client.get(f"/admin/bookings/{b['id']}/candidates", headers=ADMIN)).json()
    by_name = {c["full_name"]: c for c in cands}
    assert cands[0]["full_name"] == "Near Driver" and cands[0]["eligible"]
    assert 1 < by_name["Near Driver"]["distance_km"] < 4
    assert by_name["Far Driver"]["within_service_area"] is False
    assert by_name["Bike Only"]["reasons"] == ["Licence has no four-wheeler class"]
    assert by_name["Jobs Only"]["reasons"] == ["Wants permanent jobs only"]

    r = (await client.post(f"/admin/bookings/{b['id']}/auto-assign", headers=ADMIN)).json()
    assert r["assigned"] and r["driver_name"] == "Near Driver"

    # Customer sees the driver; the driver sees the customer
    mine = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()
    assert mine["status"] == "assigned" and mine["driver_name"] == "Near Driver"
    jobs = (await client.get("/driver/bookings", headers=token("d-near", "driver"))).json()
    assert len(jobs) == 1
    assert jobs[0]["customer_name"] == "Priya R" and jobs[0]["customer_phone"] == "+919876600001"
    assert (await client.get("/driver/bookings", headers=token("d-far", "driver"))).json() == []


async def test_heavy_vehicle_needs_heavy_licence(client, directory):
    directory.drivers = [
        driver("d-car", "Car Driver"),
        driver("d-heavy", "Truck Driver", heavy_vehicle=True, licence_classes=["LMV", "HGMV"]),
    ]
    b = (await book(client, BUSINESS, booking_type="job", vehicle_class="heavy")).json()
    r = (await client.post(f"/admin/bookings/{b['id']}/auto-assign", headers=ADMIN)).json()
    assert r["driver_name"] == "Truck Driver"


async def test_busy_driver_is_skipped_and_manual_assign_is_checked(client, directory):
    directory.drivers = [driver("d-1", "Only Driver")]
    first = (await book(client, start_at=future(hour=9))).json()
    overlapping = (await book(client, start_at=future(hour=14))).json()  # within the default 12h trip
    later = (await book(client, start_at=future(days=3))).json()

    r = await client.post(f"/admin/bookings/{first['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["assignment_mode"] == "manual"

    r = await client.post(f"/admin/bookings/{overlapping['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN)
    assert r.status_code == 409 and "Busy with booking" in r.json()["detail"]

    results = (await client.post("/admin/bookings/auto-assign", headers=ADMIN)).json()
    outcome = {x["booking_id"]: x for x in results}
    assert outcome[overlapping["id"]]["assigned"] is False and "Busy" in outcome[overlapping["id"]]["reason"]
    assert outcome[later["id"]]["assigned"] is True

    # Unassigning frees the driver
    await client.post(f"/admin/bookings/{first['id']}/unassign", json={}, headers=ADMIN)
    r = await client.post(f"/admin/bookings/{overlapping['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN)
    assert r.status_code == 200


async def test_permanent_job_without_end_blocks_the_driver(client, directory):
    directory.drivers = [driver("d-1", "Only Driver")]
    job = (await book(client, BUSINESS, booking_type="job")).json()
    await client.post(f"/admin/bookings/{job['id']}/auto-assign", headers=ADMIN)
    trip = (await book(client, start_at=future(days=30))).json()
    r = (await client.post(f"/admin/bookings/{trip['id']}/auto-assign", headers=ADMIN)).json()
    assert r["assigned"] is False


async def test_parallel_assignment_cannot_double_book(client, directory):
    directory.drivers = [driver("d-1", "Only Driver")]
    a = (await book(client, start_at=future(hour=9))).json()
    b = (await book(client, start_at=future(hour=10))).json()
    results = await asyncio.gather(
        client.post(f"/admin/bookings/{a['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN),
        client.post(f"/admin/bookings/{b['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN),
    )
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_cancel_and_complete(client, directory):
    directory.drivers = [driver("d-1", "Only Driver")]
    b = (await book(client)).json()
    await client.post(f"/admin/bookings/{b['id']}/auto-assign", headers=ADMIN)
    # Can't start a trip that is a day away
    r = await client.post(f"/driver/bookings/{b['id']}/start", json={"code": "0000"}, headers=token("d-1", "driver"))
    assert r.status_code == 409
    # A driver is assigned, so cancelling costs ₹49: paid through the payment-service, which then cancels
    r = await client.post(f"/bookings/{b['id']}/cancel", json={"reason": "Plans changed"}, headers=CUSTOMER)
    assert r.status_code == 402 and "₹49" in r.text
    assert (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()["cancellation_fee_paise"] == 4900
    r = await client.post(f"/internal/bookings/{b['id']}/cancel-paid", json={"payment_id": "pay_c", "amount_paise": 4900}, headers=SERVICE)
    assert r.json()["status"] == "cancelled"
    view = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()
    assert view["status"] == "cancelled" and view["cancel_fee_status"] == "paid" and view["cancel_fee_paise"] == 4900
    assert view["cancellation_fee_paise"] == 0
    assert (await client.post(f"/bookings/{b['id']}/cancel", json={}, headers=CUSTOMER)).status_code == 409
    # Cancelled bookings no longer show on the driver's list or block them
    assert (await client.get("/driver/bookings", headers=token("d-1", "driver"))).json() == []


async def test_admin_list_and_summary(client, directory):
    await book(client)
    await book(client, BUSINESS, booking_type="job")
    data = (await client.get("/admin/bookings?status=pending&q=kovai", headers=ADMIN)).json()
    assert data["total"] == 1 and data["items"][0]["company_name"] == "Kovai Logistics"
    s = (await client.get("/admin/bookings/summary", headers=ADMIN)).json()
    assert s["by_status"] == {"pending": 2, "assigned": 0, "in_progress": 0, "completed": 0, "cancelled": 0}


async def test_unknown_or_inactive_customer(client, directory):
    directory.users["cust-1"].is_active = False
    assert (await book(client)).status_code == 403
    assert (await book(client, token("ghost", "customer"))).status_code == 403


async def test_cancellation_fee_rules(client, directory, monkeypatch):
    from app.core.config import get_settings

    # Free while we're still finding a driver
    b = (await book(client)).json()
    assert b["cancellation_fee_paise"] == 0
    assert (await client.post(f"/bookings/{b['id']}/cancel", json={}, headers=CUSTOMER)).json()["status"] == "cancelled"

    policy = (await client.get("/bookings/policy")).json()
    assert policy == {"confirmation_fee_paise": 100, "cancellation_fee_paise": 4900, "cancellation_fee_applies": "assigned"}

    # "always": even a pending booking costs the fee; "never": assigned is free
    monkeypatch.setattr(get_settings(), "cancellation_fee_applies", "always")
    b = (await book(client)).json()
    assert (await client.post(f"/bookings/{b['id']}/cancel", json={}, headers=CUSTOMER)).status_code == 402
    directory.drivers = [driver("d-1", "Murugan")]
    await client.post(f"/admin/bookings/{b['id']}/auto-assign", headers=ADMIN)
    monkeypatch.setattr(get_settings(), "cancellation_fee_applies", "never")
    assert (await client.post(f"/bookings/{b['id']}/cancel", json={}, headers=CUSTOMER)).status_code == 200

    # Admin cancellations never charge the customer
    monkeypatch.setattr(get_settings(), "cancellation_fee_applies", "always")
    b = (await book(client)).json()
    assert (await client.post(f"/admin/bookings/{b['id']}/cancel", json={"reason": "duplicate"}, headers=ADMIN)).status_code == 200


async def test_policy_must_be_accepted_to_book(client, directory):
    directory.users["cust-1"].policy_accepted = False
    r = await book(client)
    assert r.status_code == 422 and "policy" in r.text
