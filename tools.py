import asyncio
import logging
import os
import time
from typing import Optional

from livekit import agents, api
from livekit.agents import llm

from db import (
    log_call, log_error, log_order,
    get_calls_by_phone, get_orders_by_phone,
    add_contact_memory, get_contact_memory, compress_contact_memory,
)

logger = logging.getLogger("sales-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


class SalesTools(llm.ToolContext):

    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_name: Optional[str] = None,
        sheet_row: Optional[int] = None,
        city: Optional[str] = None,
        symptom: Optional[str] = None,
        language: str = "hinglish",
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.sheet_row = sheet_row
        self.city = city
        self.symptom = symptom
        self.language = language
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list) -> list:
        all_methods = [
            self.book_order, self.request_callback, self.end_call,
            self.transfer_to_human, self.lookup_contact, self.remember_details,
        ]
        if not enabled:
            return all_methods
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[n] for n in enabled if n in name_map]

    @llm.function_tool
    async def book_order(
        self,
        variant: str,
        amount: str,
        full_address: str,
        pincode: str,
        landmark: str = "",
        alt_phone: str = "",
        notes: str = "",
    ) -> str:
        """
        Book a COD order for the 3A Piles Kit after customer confirms.
        Call ONLY after customer verbally confirms they want to order AND you have collected full address.
        variant: '15-day kit' or '30-day kit'
        amount: '1500' or '2500'
        full_address: complete house/street/area address
        pincode: 6-digit pincode
        landmark: nearby landmark (optional)
        alt_phone: alternate contact number (optional)
        notes: any special delivery instructions (optional)
        """
        from datetime import datetime
        order_id = f"AI{datetime.now().strftime('%d%m%y%H%M%S')}"

        try:
            await log_order(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name,
                city=self.city,
                variant=variant,
                amount=amount,
                full_address=full_address,
                pincode=pincode,
                landmark=landmark,
                alt_phone=alt_phone,
                language=self.language,
                notes=notes,
                order_id=order_id,
            )
        except Exception as exc:
            logger.error("Failed to log order: %s", exc)

        # Update Google Sheet row with order details
        if self.sheet_row:
            try:
                from sheets import update_order_details
                await update_order_details(
                    row=self.sheet_row,
                    status="ordered",
                    language=self.language,
                    product="3A Piles Kit",
                    variant=variant,
                    amount=amount,
                    address=full_address,
                    pincode=pincode,
                    landmark=landmark,
                    alt_phone=alt_phone,
                    wa_sent="Pending",
                    notes=notes,
                )
            except Exception as exc:
                logger.error("Sheet update failed: %s", exc)

        # Send WhatsApp confirmation
        wa_sent = False
        if self.phone_number:
            try:
                from whatsapp import send_order_confirmation
                wa_sent = await send_order_confirmation(
                    phone=self.phone_number,
                    name=self.lead_name or "Customer",
                    variant=variant,
                    amount=amount,
                    address=full_address,
                    city=self.city or "",
                    pincode=pincode,
                )
                if self.sheet_row and wa_sent:
                    from sheets import update_order_details
                    await update_order_details(
                        row=self.sheet_row,
                        status="ordered",
                        wa_sent="Yes",
                    )
            except Exception as exc:
                logger.error("WhatsApp confirmation failed: %s", exc)

        outcome = f"ordered_{variant.replace(' ', '_').replace('-', '_').lower()}"
        await _log(f"Order booked: {self.phone_number} — {variant} ₹{amount}")

        return (
            f"Order confirmed! Order ID: {order_id}. "
            f"{variant} — ₹{amount} COD. "
            f"Delivery to {full_address[:40]}... in 3-5 working days. "
            f"WhatsApp confirmation {'sent' if wa_sent else 'will be sent shortly'}."
        )

    @llm.function_tool
    async def request_callback(self, reason: str = "") -> str:
        """
        Log a callback request when customer wants to speak with a human agent.
        Call this whenever the customer says they want a human, says 'real person',
        or clearly does not want to talk to an AI.
        reason: brief note on why they want a callback
        """
        from datetime import datetime

        # Notify team via WhatsApp
        try:
            from whatsapp import send_team_callback_alert
            await send_team_callback_alert(
                phone=self.phone_number or "unknown",
                name=self.lead_name or "Lead",
                city=self.city or "Unknown",
                note=reason or "Customer wants human agent callback",
                called_at=datetime.now().strftime("%I:%M %p IST"),
            )
        except Exception as exc:
            logger.error("Team alert failed: %s", exc)

        # Update sheet
        if self.sheet_row:
            try:
                from sheets import update_row_status
                await update_row_status(self.sheet_row, "callback_requested")
            except Exception as exc:
                logger.error("Sheet status update failed: %s", exc)

        await _log(f"Callback requested: {self.phone_number} — {reason}")
        return "Callback request noted. Our team will call you back shortly."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """
        End the call and log the outcome. ALWAYS call this before the call ends.
        outcome options:
          ordered_15day_kit | ordered_30day_kit | not_interested | callback_requested |
          no_answer | wrong_number | existing_refill | voicemail
        reason: brief description of what happened
        """
        duration = int(time.time() - self._call_start_time)

        # Map order outcomes for sheet status
        sheet_status_map = {
            "ordered_15day_kit": "ordered",
            "ordered_30day_kit": "ordered",
            "not_interested": "not_interested",
            "callback_requested": "callback_requested",
            "no_answer": "no_answer",
            "wrong_number": "invalid",
            "existing_refill": "ordered",
            "voicemail": "no_answer",
        }
        sheet_status = sheet_status_map.get(outcome, outcome)

        try:
            await log_call(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name,
                outcome=outcome,
                reason=reason,
                duration_seconds=duration,
            )
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)

        # Update sheet status if not already set to 'ordered'
        if self.sheet_row and outcome not in ("ordered_15day_kit", "ordered_30day_kit", "existing_refill"):
            try:
                from sheets import update_row_status
                await update_row_status(self.sheet_row, sheet_status)
            except Exception as exc:
                logger.error("Sheet status update failed: %s", exc)

        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call ended."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Transfer the call to a human agent via SIP REFER.
        Use when customer is angry, has a very complex query, or requests human.
        reason: why you are transferring
        """
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            await self.request_callback(reason=reason)
            return "Transfer unavailable. Callback request logged for our team."

        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"

        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity:
            return "Transfer failed: could not identify caller."

        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination,
                    play_dialtone=False,
                )
            )
            return "Transferring you to a human agent now. Please hold."
        except Exception:
            await self.request_callback(reason=reason)
            return "Transfer failed. Callback request logged for our team."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """
        Look up a contact's full history. Call at the START of every call.
        phone: lead's phone number with country code
        Returns call history, past orders, and remembered details.
        """
        try:
            calls = await get_calls_by_phone(phone)
            orders = await get_orders_by_phone(phone)
            memories = await get_contact_memory(phone)
            if not calls and not orders and not memories:
                return f"No history for {phone}. First-time contact."
            lines = [f"Contact history for {phone}:"]
            if memories:
                lines.append(f"\nREMEMBERED ({len(memories)} notes):")
                for m in memories[:5]:
                    lines.append(f"  - {m['insight']}")
            if orders:
                lines.append(f"\nPAST ORDERS ({len(orders)}):")
                for o in orders[:3]:
                    ts = (o.get("created_at") or "")[:10]
                    lines.append(f"  - {ts} — {o.get('variant')} ₹{o.get('amount')} [{o.get('status')}]")
            if calls:
                lines.append(f"\nCALL HISTORY ({len(calls)} calls):")
                for c in calls[:5]:
                    ts = (c.get("timestamp") or "")[:16]
                    lines.append(f"  - {ts} — {c.get('outcome','?')}: {c.get('reason','')}")
            return "\n".join(lines)
        except Exception:
            return "Unable to retrieve contact history."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Store a key insight about this lead for future calls.
        Use whenever you learn: symptoms, family situation, budget concerns, preferred time, occupation.
        insight: the detail to remember (keep it concise)
        """
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Noted: {insight}"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            import google.generativeai as genai
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key:
                return
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = f"Compress these notes about a sales lead into 3-5 concise bullets. Keep all key facts.\n\n{bullet_list}"
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
            if response.text.strip():
                await compress_contact_memory(self.phone_number, response.text.strip())
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)


# Keep AppointmentTools as alias for backward compatibility during migration
AppointmentTools = SalesTools
