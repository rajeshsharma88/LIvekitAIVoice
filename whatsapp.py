"""AIsensy WhatsApp integration for Aarogya India."""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("whatsapp")

AISENSY_API_URL = "https://backend.aisensy.com/campaign/t1/api/v2"


def _api_key() -> str:
    return os.getenv("AISENSY_API_KEY", "")


def _from_number() -> str:
    return os.getenv("AISENSY_FROM_NUMBER", "")


def _team_number() -> str:
    return os.getenv("TEAM_WHATSAPP_NUMBER", "")


async def send_order_confirmation(
    phone: str,
    name: str,
    variant: str,
    amount: str,
    address: str,
    city: str = "",
    pincode: str = "",
) -> bool:
    """Send order confirmation WhatsApp to customer via AIsensy."""
    api_key = _api_key()
    if not api_key:
        logger.warning("AISENSY_API_KEY not set — WhatsApp skipped")
        return False

    # Normalize phone to E.164 without +
    phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    if not phone_clean.startswith("91") and len(phone_clean) == 10:
        phone_clean = "91" + phone_clean

    full_address = f"{address}, {city} - {pincode}".strip(", -")

    payload = {
        "apiKey": api_key,
        "campaignName": os.getenv("AISENSY_ORDER_TEMPLATE", "aarogya_order_confirmation"),
        "destination": phone_clean,
        "userName": "Aarogya India",
        "templateParams": [name, variant, amount, full_address],
        "source": "OutboundAI",
        "media": {},
        "buttons": [],
        "carouselCards": [],
        "location": {},
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(AISENSY_API_URL, json=payload)
        if resp.status_code in (200, 201):
            logger.info("Order confirmation sent to %s", phone)
            return True
        logger.warning("AIsensy responded %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.error("WhatsApp order confirmation failed: %s", exc)
        return False


async def send_team_callback_alert(
    phone: str,
    name: str,
    city: str,
    note: str = "Customer wants human agent callback",
    called_at: Optional[str] = None,
) -> bool:
    """Alert sales team on WhatsApp when a lead requests human callback."""
    api_key = _api_key()
    team_number = _team_number()
    if not api_key or not team_number:
        logger.warning("AISENSY_API_KEY or TEAM_WHATSAPP_NUMBER not set — team alert skipped")
        return False

    team_clean = team_number.replace("+", "").replace(" ", "")
    if not team_clean.startswith("91") and len(team_clean) == 10:
        team_clean = "91" + team_clean

    from datetime import datetime
    time_str = called_at or datetime.now().strftime("%I:%M %p IST")

    payload = {
        "apiKey": api_key,
        "campaignName": os.getenv("AISENSY_CALLBACK_TEMPLATE", "aarogya_callback_alert"),
        "destination": team_clean,
        "userName": "Aarogya India",
        "templateParams": [name, phone, city, time_str, note],
        "source": "OutboundAI",
        "media": {},
        "buttons": [],
        "carouselCards": [],
        "location": {},
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(AISENSY_API_URL, json=payload)
        if resp.status_code in (200, 201):
            logger.info("Team callback alert sent for %s", phone)
            return True
        logger.warning("AIsensy team alert %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.error("WhatsApp team alert failed: %s", exc)
        return False
