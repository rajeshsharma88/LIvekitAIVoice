"""
CRM Callback — POST call outcomes to the Lovable CRM.

Endpoint: https://aarogya-care-crm.lovable.app/api/public/agent-callback
Auth:     X-Agent-Secret header
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("crm-callback")

CRM_CALLBACK_URL = os.getenv(
    "CRM_CALLBACK_URL",
    "https://aarogya-care-crm.lovable.app/api/public/agent-callback",
)
CRM_AGENT_SECRET = os.getenv(
    "CRM_AGENT_SECRET",
    "c9bff9fd82d95421442683cb092e0e76a5977dedf9f6f19b",
)

# Map internal agent outcomes → CRM-compatible outcome strings
_OUTCOME_MAP = {
    "ordered_15day_kit": "ordered",
    "ordered_30day_kit": "ordered",
    "existing_refill":   "ordered",
    "not_interested":    "not_interested",
    "callback_requested": "callback_requested",
    "no_answer":         "no_answer",
    "wrong_number":      "other",
    "voicemail":         "no_answer",
}


async def send_crm_callback(
    phone: str,
    outcome: str,
    lead_name: Optional[str] = None,
    notes: Optional[str] = None,
    called_at: Optional[str] = None,
    raw: Optional[dict] = None,
) -> bool:
    """
    POST the call result to the Lovable CRM.

    Returns True on success, False on failure (never raises).
    """
    if not CRM_CALLBACK_URL or not CRM_AGENT_SECRET:
        logger.warning("CRM callback skipped — URL or secret not configured")
        return False

    # Normalise the outcome to what the CRM expects
    crm_outcome = _OUTCOME_MAP.get(outcome, "other")

    payload = {
        "phone": phone,
        "outcome": crm_outcome,
    }
    if lead_name:
        payload["lead_name"] = lead_name
    if notes:
        payload["notes"] = notes
    payload["called_at"] = called_at or datetime.utcnow().isoformat()
    if raw:
        payload["raw"] = raw

    headers = {
        "Content-Type": "application/json",
        "X-Agent-Secret": CRM_AGENT_SECRET,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(CRM_CALLBACK_URL, json=payload, headers=headers)
            if resp.status_code in (200, 201, 204):
                logger.info("CRM callback OK for %s → %s (%d)", phone, crm_outcome, resp.status_code)
                return True
            else:
                logger.error(
                    "CRM callback failed for %s: HTTP %d — %s",
                    phone, resp.status_code, resp.text[:300],
                )
                return False
    except Exception as exc:
        logger.error("CRM callback exception for %s: %s", phone, exc)
        return False
