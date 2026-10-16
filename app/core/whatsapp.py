"""Sending WhatsApp messages from the business number.

console: messages are logged and kept in memory (development, tests, the /whatsapp/dev endpoints).
cloud:   Meta WhatsApp Cloud API. Free-form text is only allowed within 24 hours of the person's last
         message to us; otherwise an approved template is used.
"""

import hashlib
import hmac
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.core.config import get_settings

logger = logging.getLogger("vandi.whatsapp")


@dataclass
class Outgoing:
    to: str  # +91…
    text: str
    template: bool
    at: datetime = field(default_factory=lambda: datetime.now())


# Recent messages in console mode, newest last
OUTBOX: deque[Outgoing] = deque(maxlen=200)


def wa_digits(phone: str) -> str:
    """+919876543210 -> 919876543210 (WhatsApp's format)"""
    return "".join(ch for ch in phone if ch.isdigit())


def from_wa(digits: str) -> str:
    """919876543210 -> +919876543210 (our format)"""
    return "+" + "".join(ch for ch in digits if ch.isdigit())


def valid_signature(raw_body: bytes, header: str | None) -> bool:
    secret = get_settings().whatsapp_app_secret
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def send(to: str, text: str, *, in_window: bool, template_params: list[str] | None = None) -> str:
    """Send to a +91… number. Returns 'sent', 'template' or 'failed'."""
    s = get_settings()
    use_template = not in_window and template_params is not None
    if s.whatsapp_provider != "cloud":
        OUTBOX.append(Outgoing(to=to, text=text, template=use_template))
        logger.warning("WHATSAPP (console) to %s%s: %s", to, " [template]" if use_template else "", text)
        return "template" if use_template else "sent"

    if use_template:
        payload = {
            "messaging_product": "whatsapp",
            "to": wa_digits(to),
            "type": "template",
            "template": {
                "name": s.whatsapp_relay_template,
                "language": {"code": s.whatsapp_template_language},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": p[:1000]} for p in template_params]}
                ],
            },
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": wa_digits(to), "type": "text", "text": {"body": text[:4096]}}
    url = f"https://graph.facebook.com/{s.whatsapp_graph_version}/{s.whatsapp_phone_number_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {s.whatsapp_access_token}"})
        if r.status_code >= 300:
            logger.error("WhatsApp send to %s failed: %s %s", to, r.status_code, r.text[:300])
            return "failed"
    except httpx.HTTPError:
        logger.exception("WhatsApp send to %s failed", to)
        return "failed"
    return "template" if use_template else "sent"
