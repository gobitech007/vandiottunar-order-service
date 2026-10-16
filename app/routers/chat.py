import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import whatsapp
from app.core.config import get_settings
from app.db import get_session
from app.services import chat

logger = logging.getLogger("vandi.whatsapp")
router = APIRouter(tags=["chat"])


@router.get("/chat/{token}", include_in_schema=False)
async def open_chat(token: str, session: AsyncSession = Depends(get_session)):
    """Encrypted link from a booking -> WhatsApp with the business number and the chat PIN."""
    return RedirectResponse(await chat.link_target(session, token), status_code=status.HTTP_302_FOUND)


@router.get("/whatsapp/webhook", include_in_schema=False)
async def verify_webhook(
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
):
    """Meta calls this once when you add the webhook URL in the WhatsApp app settings."""
    expected = get_settings().whatsapp_verify_token
    if mode == "subscribe" and expected and token == expected:
        return PlainTextResponse(challenge)
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed")


@router.post("/whatsapp/webhook", include_in_schema=False)
async def receive(request: Request, session: AsyncSession = Depends(get_session)):
    raw = await request.body()
    if not whatsapp.valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature")
    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                text = msg.get("text", {}).get("body") if msg.get("type") == "text" else None
                if msg.get("type") == "button":
                    text = msg.get("button", {}).get("text")
                try:
                    await chat.handle_inbound(session, msg.get("from", ""), text, msg.get("id"))
                except Exception:  # never make Meta retry forever because of one bad message
                    logger.exception("Could not handle WhatsApp message %s", msg.get("id"))
                    await session.rollback()
    return {"ok": True}


# ---------- Development helpers (console provider only) ----------

class DevInbound(BaseModel):
    from_phone: str = Field(description="+91… number the message comes from")
    text: str


def _dev_only() -> None:
    s = get_settings()
    if s.is_production or s.whatsapp_provider != "console":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


@router.post("/whatsapp/dev/inbound", tags=["chat (development)"])
async def dev_inbound(body: DevInbound, session: AsyncSession = Depends(get_session)):
    """Pretend a WhatsApp message arrived from this number. Returns what the relay sent out."""
    _dev_only()
    before = len(whatsapp.OUTBOX)
    await chat.handle_inbound(session, whatsapp.wa_digits(body.from_phone), body.text, None)
    sent = list(whatsapp.OUTBOX)[before:] if len(whatsapp.OUTBOX) >= before else list(whatsapp.OUTBOX)
    return [{"to": m.to, "text": m.text, "template": m.template} for m in sent]


@router.get("/whatsapp/dev/outbox", tags=["chat (development)"])
async def dev_outbox():
    """Messages the relay 'sent' (console provider), newest last."""
    _dev_only()
    return [{"to": m.to, "text": m.text, "template": m.template, "at": m.at.isoformat()} for m in whatsapp.OUTBOX]
