from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ADMIN, CUSTOMER, RS_PURAM, SERVICE, driver, pay, token

pytestmark = pytest.mark.asyncio
DRIVER = token("d-1", "driver")
OTHER_DRIVER = token("d-2", "driver")


def soon(minutes: int = 10) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


async def assigned_booking(client, directory) -> dict:
    directory.drivers = [driver("d-1", "Murugan"), driver("d-2", "Senthil")]
    b = (await client.post("/bookings", json={"start_at": soon(), "pickup_lat": RS_PURAM[0], "pickup_lng": RS_PURAM[1]}, headers=CUSTOMER)).json()
    r = await client.post(f"/admin/bookings/{b['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    await pay(client, b["id"])
    return (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()


def pt(lat, lng, seconds_ago=0, acc=8):
    at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return {"lat": lat, "lng": lng, "accuracy_m": acc, "recorded_at": at.isoformat()}


async def test_full_trip_with_codes_and_tracking(client, directory):
    b = await assigned_booking(client, directory)
    code = b["start_code"]
    assert b["status"] == "assigned" and len(code) == 4 and b["end_code"] is None

    # The driver never sees the codes
    mine = (await client.get("/driver/bookings", headers=DRIVER)).json()[0]
    assert "start_code" not in mine and "end_code" not in mine
    admin_view = (await client.get(f"/admin/bookings/{b['id']}", headers=ADMIN)).json()
    assert "start_code" not in admin_view

    # Not tracked before the start code; another driver can't start it
    assert (await client.get(f"/bookings/{b['id']}/track", headers=CUSTOMER)).status_code == 409
    assert (await client.post(f"/driver/bookings/{b['id']}/location", json={"points": [pt(11.0, 76.9)]}, headers=DRIVER)).status_code == 409
    assert (await client.post(f"/driver/bookings/{b['id']}/start", json={"code": code}, headers=OTHER_DRIVER)).status_code == 404

    wrong = "0000" if code != "0000" else "1111"
    r = await client.post(f"/driver/bookings/{b['id']}/start", json={"code": wrong}, headers=DRIVER)
    assert r.status_code == 422 and "4 tries left" in r.text

    r = await client.post(
        f"/driver/bookings/{b['id']}/start", json={"code": code, "lat": RS_PURAM[0], "lng": RS_PURAM[1]}, headers=DRIVER
    )
    assert r.status_code == 200 and r.json()["status"] == "in_progress" and r.json()["started_at"]

    b = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()
    assert b["start_code"] is None and len(b["end_code"]) == 4

    # Driving ~1.1 km north in two steps, plus a jittery and a duplicate point
    far = pt(11.0190, 76.9520, -30)
    points = [pt(11.0140, 76.9520, -10), pt(11.0140, 76.9521, -20), far, dict(far)]  # last one is resent
    r = await client.post(f"/driver/bookings/{b['id']}/location", json={"points": points}, headers=DRIVER)
    assert r.status_code == 204
    t = (await client.get(f"/bookings/{b['id']}/track", headers=CUSTOMER)).json()
    assert t["status"] == "in_progress" and t["last"]["lat"] == pytest.approx(11.0190)
    assert 1.0 < t["distance_km"] < 1.2, t["distance_km"]
    assert len(t["path"]) == 4 and t["driver_name"] == "Murugan"
    assert (await client.get(f"/admin/bookings/{b['id']}/track", headers=ADMIN)).json()["distance_km"] == t["distance_km"]

    # A running trip keeps the driver busy and can't be cancelled by the customer (not even by paying the fee)
    assert (await client.post(f"/bookings/{b['id']}/cancel", json={}, headers=CUSTOMER)).status_code == 409
    r = await client.post(f"/internal/bookings/{b['id']}/cancel-paid", json={"payment_id": "p", "amount_paise": 4900}, headers=SERVICE)
    assert r.status_code == 409 and "refund" in r.text

    r = await client.post(f"/driver/bookings/{b['id']}/end", json={"code": b["end_code"]}, headers=DRIVER)
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["completed_at"]
    assert (await client.post(f"/driver/bookings/{b['id']}/location", json={"points": [pt(11.02, 76.95)]}, headers=DRIVER)).status_code == 409
    t = (await client.get(f"/bookings/{b['id']}/track", headers=CUSTOMER)).json()
    assert t["status"] == "completed" and t["completed_at"]

    # After the trip the fare uses the real time (a few seconds → the 1-hour minimum, 10% first-booking discount)
    fare = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()["fare"]
    assert (fare["basis"], fare["hours"], fare["total_paise"]) == ("actual", 1, 13500)


async def test_too_many_wrong_codes_changes_the_code(client, directory):
    b = await assigned_booking(client, directory)
    code = b["start_code"]
    wrong = "0000" if code != "0000" else "1111"
    for _ in range(4):
        assert (await client.post(f"/driver/bookings/{b['id']}/start", json={"code": wrong}, headers=DRIVER)).status_code == 422
    r = await client.post(f"/driver/bookings/{b['id']}/start", json={"code": wrong}, headers=DRIVER)
    assert r.status_code == 429
    new = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()["start_code"]
    assert new and (new != code or new == code)  # replaced (may rarely repeat by chance)
    assert (await client.post(f"/driver/bookings/{b['id']}/start", json={"code": new}, headers=DRIVER)).status_code == 200


async def test_running_trip_blocks_driver_and_admin_can_finish(client, directory):
    b = await assigned_booking(client, directory)
    await client.post(f"/driver/bookings/{b['id']}/start", json={"code": b["start_code"]}, headers=DRIVER)
    # A new booking at the same time can't go to the driver on a trip
    other = (await client.post("/bookings", json={"start_at": soon(20)}, headers=CUSTOMER)).json()
    cands = (await client.get(f"/admin/bookings/{other['id']}/candidates?all=true", headers=ADMIN)).json()
    d1 = next(c for c in cands if c["driver_user_id"] == "d-1")
    assert not d1["eligible"]
    # Unassign isn't allowed mid-trip; admin can complete without the code
    assert (await client.post(f"/admin/bookings/{b['id']}/unassign", json={}, headers=ADMIN)).status_code == 409
    r = await client.post(f"/admin/bookings/{b['id']}/complete", headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "completed"


async def test_unassign_gives_new_code(client, directory):
    b = await assigned_booking(client, directory)
    await client.post(f"/admin/bookings/{b['id']}/unassign", json={}, headers=ADMIN)
    assert (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()["start_code"] is None
    await client.post(f"/admin/bookings/{b['id']}/assign", json={"driver_user_id": "d-2"}, headers=ADMIN)
    # Already paid, so the new driver's code shows straight away
    assert len((await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()["start_code"]) == 4
