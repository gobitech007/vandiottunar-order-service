import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from app.core import whatsapp
from tests.conftest import ADMIN, CUSTOMER, SERVICE, driver, pay, token

pytestmark = pytest.mark.asyncio
DRIVER = token("d-1", "driver")
CUSTOMER_PHONE = "+919876600001"


async def assigned(client, directory) -> dict:
    directory.drivers = [driver("d-1", "Murugan", phone="+919811122233")]
    start = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    b = (await client.post("/bookings", json={"start_at": start}, headers=CUSTOMER)).json()
    assert (await client.post(f"/admin/bookings/{b['id']}/assign", json={"driver_user_id": "d-1"}, headers=ADMIN)).status_code == 200
    return b


async def test_driver_hidden_until_paid(client, directory):
    b = await assigned(client, directory)
    view = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()
    assert view["driver_name"] == "Murugan" and "driver_phone" not in view
    assert view["payment_status"] == "unpaid" and view["fee_paise"] == 100
    assert view["start_code"] is None and view["chat_url"] is None
    # The driver can't start an unconfirmed booking and has no chat link yet
    mine = (await client.get("/driver/bookings", headers=DRIVER)).json()[0]
    assert mine["payment_status"] == "unpaid" and mine["chat_url"] is None
    r = await client.post(f"/driver/bookings/{b['id']}/start", json={"code": "1234"}, headers=DRIVER)
    assert r.status_code == 409 and "confirmed" in r.text

    # Internal API needs the service token and the full fee
    assert (await client.post(f"/internal/bookings/{b['id']}/paid", json={"payment_id": "x", "amount_paise": 100})).status_code == 401
    assert (await client.post(f"/internal/bookings/{b['id']}/paid", json={"payment_id": "x", "amount_paise": 50}, headers=SERVICE)).status_code == 422
    info = (await client.get(f"/internal/bookings/{b['id']}", headers=SERVICE)).json()
    assert info["customer_user_id"] == "cust-1" and info["status"] == "assigned"

    await pay(client, b["id"])
    await pay(client, b["id"])  # repeated callbacks are fine
    view = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()
    assert view["payment_status"] == "paid" and len(view["start_code"]) == 4
    assert view["chat_url"].startswith("http://test/chat/") and "driver_phone" not in view
    admin = (await client.get(f"/admin/bookings/{b['id']}", headers=ADMIN)).json()
    assert admin["payment_id"] == "pay_test" and admin["driver_phone"] == "+919811122233"


async def test_chat_link_and_relay(client, directory):
    b = await assigned(client, directory)
    await pay(client, b["id"])
    url = (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()["chat_url"]
    token_part = url.rsplit("/", 1)[1]
    assert b["id"] not in token_part  # encrypted, not just signed

    r = await client.get(f"/chat/{token_part}", follow_redirects=False)
    assert r.status_code == 302
    target = urlparse(r.headers["location"])
    assert target.netloc == "wa.me" and target.path == "/919000011111"
    text = unquote(parse_qs(target.query)["text"][0])
    pin = text.split("Chat PIN: ")[1][:6]
    assert b["reference"] in text and pin.isdigit()
    assert (await client.get("/chat/garbage", follow_redirects=False)).status_code == 404

    whatsapp.OUTBOX.clear()
    # A stranger sending the PIN gets nothing
    out = (await client.post("/whatsapp/dev/inbound", json={"from_phone": "+919000000000", "text": text})).json()
    assert len(out) == 1 and "isn't active for your number" in out[0]["text"]

    # Customer sends the pre-filled message: confirmed, driver is told (template: driver hasn't written yet)
    out = (await client.post("/whatsapp/dev/inbound", json={"from_phone": CUSTOMER_PHONE, "text": text})).json()
    to = {m["to"]: m for m in out}
    assert "connected with your driver Murugan" in to[CUSTOMER_PHONE]["text"]
    assert to["+919811122233"]["template"] is True

    out = (await client.post("/whatsapp/dev/inbound", json={"from_phone": CUSTOMER_PHONE, "text": "I'm at the gate"})).json()
    assert out == [{"to": "+919811122233", "text": f"[{b['reference']}] Customer Priya R: I'm at the gate", "template": True}]

    # Driver replies (inside their window now): customer gets plain text, numbers never appear
    out = (await client.post("/whatsapp/dev/inbound", json={"from_phone": "+919811122233", "text": "Coming in 5 min"})).json()
    assert out[0]["to"] == CUSTOMER_PHONE and out[0]["template"] is False
    assert "Driver Murugan: Coming in 5 min" in out[0]["text"] and "98111" not in out[0]["text"]
    out = (await client.post("/whatsapp/dev/inbound", json={"from_phone": "+919811122233", "text": "Reached"})).json()
    assert out[0]["template"] is False

    msgs = (await client.get(f"/admin/bookings/{b['id']}/chat", headers=ADMIN)).json()
    assert [m["sender"] for m in msgs] == ["customer", "customer", "driver", "driver"]

    # Cancelling closes the chat; link and PIN stop working
    await client.post(f"/bookings/{b['id']}/cancel", json={}, headers=CUSTOMER)
    assert (await client.get(f"/chat/{token_part}", follow_redirects=False)).status_code == 410
    out = (await client.post("/whatsapp/dev/inbound", json={"from_phone": CUSTOMER_PHONE, "text": "hello?"})).json()
    assert "open the WhatsApp link" in out[0]["text"]


async def test_webhook_signature_and_verify(client, directory):
    r = await client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify", "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"
    assert (await client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"})).status_code == 403

    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"from": "919000000000", "id": "wamid.1", "type": "text", "text": {"body": "hi"}}]}}]}]}).encode()
    assert (await client.post("/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"})).status_code == 401
    sig = "sha256=" + hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()
    whatsapp.OUTBOX.clear()
    r = await client.post("/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200 and len(whatsapp.OUTBOX) == 1  # unknown sender gets the help reply


async def test_admin_can_waive(client, directory):
    b = await assigned(client, directory)
    r = await client.post(f"/admin/bookings/{b['id']}/waive-payment", headers=ADMIN)
    assert r.status_code == 200 and r.json()["payment_status"] == "waived"
    assert (await client.get(f"/bookings/{b['id']}", headers=CUSTOMER)).json()["chat_url"]
