"""Customer <-> driver chat over WhatsApp, relayed through the business number.

1. After paying, the customer gets an encrypted chat link (/chat/<token>). It opens WhatsApp with the
   business number and a pre-filled message holding the booking reference and a 6-digit chat PIN.
2. Messages from the customer's or driver's registered number are forwarded to the other person,
   labelled with the booking reference. Neither sees the other's phone number.
3. The PIN tells us which booking a message is about (a driver can have several). The chat closes when
   the trip ends, is cancelled, or the driver is removed.
"""

import base64
import hashlib
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import whatsapp
from app.core.config import get_settings
from app.models import Booking, BookingStatus, ChatMessage, ChatRoute, ChatSession
from app.timeutil import as_utc, utcnow

OPEN_STATUSES = (BookingStatus.assigned, BookingStatus.in_progress)
LINK_DAYS = 30
WINDOW = timedelta(hours=24)
_PIN_RE = re.compile(r"\bPIN\D{0,3}(\d{6})\b", re.I)


# ---------- Encrypted links ----------

def _fernet() -> Fernet:
    # Derived from JWT_SECRET so there's one less secret to manage; rotating JWT_SECRET expires links
    key = hashlib.sha256(("vandi-chat-link:" + get_settings().jwt_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def chat_url(booking: Booking, role: str) -> str | None:
    """Encrypted link for the customer or driver, once the booking is confirmed and still running."""
    if booking.status not in OPEN_STATUSES or not is_confirmed(booking):
        return None
    token = _fernet().encrypt(json.dumps({"b": booking.id, "r": role}).encode()).decode()
    return f"{get_settings().public_url.rstrip('/')}/chat/{token}"


def read_token(token: str) -> tuple[str, str]:
    try:
        data = json.loads(_fernet().decrypt(token.encode(), ttl=LINK_DAYS * 86400))
        return data["b"], data["r"]
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This chat link is not valid")


def is_confirmed(booking: Booking) -> bool:
    return booking.payment_status in ("paid", "waived") or not get_settings().require_payment


# ---------- Sessions ----------

async def _new_pin(session: AsyncSession) -> str:
    for _ in range(20):
        pin = f"{secrets.randbelow(900_000) + 100_000}"
        taken = await session.scalar(select(ChatSession.id).where(ChatSession.pin == pin, ChatSession.closed_at.is_(None)))
        if not taken:
            return pin
    raise HTTPException(503, "Please try again")


async def open_session(session: AsyncSession, booking: Booking) -> ChatSession:
    chat = await session.scalar(select(ChatSession).where(ChatSession.booking_id == booking.id))
    if chat and chat.closed_at is None and chat.driver_wa == booking.driver_phone:
        return chat
    if chat:  # closed earlier, or the driver changed: start over with a new PIN
        await session.delete(chat)
        await session.flush()
    chat = ChatSession(
        booking_id=booking.id,
        pin=await _new_pin(session),
        customer_wa=booking.customer_phone,
        driver_wa=booking.driver_phone,
    )
    session.add(chat)
    await session.flush()
    return chat


async def close_session(session: AsyncSession, booking: Booking, reason: str) -> None:
    """Called when a booking ends; both sides are told the chat is over. Caller commits."""
    chat = await session.scalar(select(ChatSession).where(ChatSession.booking_id == booking.id, ChatSession.closed_at.is_(None)))
    if chat is None:
        return
    chat.closed_at = utcnow()
    note = f"[{booking.reference}] {reason} This chat is now closed."
    for phone, last in ((chat.customer_wa, chat.customer_last_in), (chat.driver_wa, chat.driver_last_in)):
        if last and utcnow() - as_utc(last) < WINDOW:  # only people who used the chat, inside the free window
            await whatsapp.send(phone, note, in_window=True)


async def link_target(session: AsyncSession, token: str) -> str:
    """Where /chat/<token> sends the browser: WhatsApp with the business number and the PIN message."""
    booking_id, role = read_token(token)
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.status not in OPEN_STATUSES or not is_confirmed(booking):
        raise HTTPException(status.HTTP_410_GONE, "This booking's chat has ended")
    chat = await open_session(session, booking)
    await session.commit()
    other = "driver" if role == "customer" else "customer"
    text = (
        f"Vandi Ottunar booking {booking.reference}\n"
        f"Chat PIN: {chat.pin}\n"
        f"(Send this message to start chatting with your {other}. Your number is not shared.)"
    )
    return f"https://wa.me/{whatsapp.wa_digits(get_settings().whatsapp_business_number)}?text={quote(text)}"


# ---------- Incoming messages ----------

async def _reply(phone: str, text: str) -> None:
    await whatsapp.send(phone, text, in_window=True)  # they just wrote to us, so we're inside the window


async def _session_for(session: AsyncSession, phone: str, text: str) -> tuple[ChatSession | None, bool]:
    """(chat, chose_by_pin). The PIN picks the chat; otherwise the last one used, or the only open one."""
    m = _PIN_RE.search(text) or re.fullmatch(r"\s*(\d{6})\s*", text)
    if m:
        chat = await session.scalar(select(ChatSession).where(ChatSession.pin == m.group(1), ChatSession.closed_at.is_(None)))
        if chat is None or phone not in (chat.customer_wa, chat.driver_wa):
            return None, True
        return chat, True
    route = await session.get(ChatRoute, phone)
    if route:
        chat = await session.get(ChatSession, route.session_id)
        if chat and chat.closed_at is None:
            return chat, False
    open_chats = (
        await session.scalars(
            select(ChatSession).where(
                ChatSession.closed_at.is_(None), (ChatSession.customer_wa == phone) | (ChatSession.driver_wa == phone)
            )
        )
    ).all()
    return (open_chats[0] if len(open_chats) == 1 else None), False


async def handle_inbound(session: AsyncSession, wa_from: str, text: str | None, wa_message_id: str | None) -> None:
    phone = whatsapp.from_wa(wa_from)
    if wa_message_id and await session.scalar(select(ChatMessage.id).where(ChatMessage.wa_message_id == wa_message_id)):
        return  # WhatsApp retries webhooks; we've handled this one
    if text is None:
        await _reply(phone, "Sorry, only text messages can be passed on. Please type your message.")
        return

    chat, by_pin = await _session_for(session, phone, text)
    if chat is None:
        if by_pin:
            await _reply(phone, "That chat PIN isn't active for your number. Open the chat link again from your Vandi Ottunar booking.")
        else:
            await _reply(
                phone,
                "Hello from Vandi Ottunar. To chat about a booking, open the WhatsApp link on your booking "
                "(it sends your chat PIN). For anything else, call us.",
            )
        return

    booking = await session.get(Booking, chat.booking_id)
    is_customer = phone == chat.customer_wa
    me, other = ("customer", "driver") if is_customer else ("driver", "customer")
    now = utcnow()
    if is_customer:
        chat.customer_last_in = now
    else:
        chat.driver_last_in = now
    route = await session.get(ChatRoute, phone)
    if route:
        route.session_id = chat.id
        route.updated_at = now
    else:
        session.add(ChatRoute(phone=phone, session_id=chat.id))

    if by_pin:
        # The pre-filled opening message: confirm, and let the other side know
        session.add(ChatMessage(session_id=chat.id, sender=me, body=text[:2000], wa_message_id=wa_message_id, delivery="sent"))
        other_name = (booking.driver_name if is_customer else booking.customer_name) or other
        await _reply(
            phone,
            f"[{booking.reference}] You're connected with your {other} {other_name}. Type your messages here and we'll "
            f"pass them on. Phone numbers stay private.",
        )
        intro = f"[{booking.reference}] Your {me} {(booking.customer_name if is_customer else booking.driver_name) or ''} opened the chat. Reply here to message them."
        await _forward(chat, booking, to_customer=not is_customer, text=intro, sender_label="Vandi Ottunar")
        await session.commit()
        return

    label = f"{'Customer' if is_customer else 'Driver'} {(booking.customer_name if is_customer else booking.driver_name) or ''}".strip()
    delivery = await _forward(chat, booking, to_customer=not is_customer, text=f"[{booking.reference}] {label}: {text}", sender_label=label, raw=text)
    session.add(ChatMessage(session_id=chat.id, sender=me, body=text[:4000], wa_message_id=wa_message_id, delivery=delivery))
    if delivery == "failed":
        await _reply(phone, "We couldn't deliver that message right now. Please try again in a minute or call support.")
    await session.commit()


async def _forward(chat: ChatSession, booking: Booking, *, to_customer: bool, text: str, sender_label: str, raw: str | None = None) -> str:
    to = chat.customer_wa if to_customer else chat.driver_wa
    last = chat.customer_last_in if to_customer else chat.driver_last_in
    in_window = last is not None and utcnow() - as_utc(last) < WINDOW
    return await whatsapp.send(to, text, in_window=in_window, template_params=[booking.reference, sender_label, raw or text])


async def transcript(session: AsyncSession, booking_id: str) -> list[ChatMessage]:
    chat = await session.scalar(select(ChatSession).where(ChatSession.booking_id == booking_id))
    if chat is None:
        return []
    return list(
        (await session.scalars(select(ChatMessage).where(ChatMessage.session_id == chat.id).order_by(ChatMessage.created_at))).all()
    )
