import asyncio
import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from crm_callback import send_crm_callback
from language import detect_language
from prompts import DEFAULT_SYSTEM_PROMPT, build_prompt

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-server")

app = FastAPI(title="OutboundAI", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

scheduler = AsyncIOScheduler()

UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
if os.path.isdir(UI_DIR):
    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")


# ── Pydantic models ───────────────────────────────────────────────────────────

class SingleCallRequest(BaseModel):
    phone_number: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None

class BatchCallRequest(BaseModel):
    contacts: list
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    call_delay_seconds: int = 3
    agent_profile_id: Optional[str] = None

class AppointmentCreate(BaseModel):
    name: str
    phone: str
    date: str
    time: str
    service: str

class CampaignCreate(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None

class AgentProfileCreate(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: list = []
    is_default: bool = False

class N8nWebhookRequest(BaseModel):
    name: str = "there"
    phone: str
    city: str = ""
    symptom: str = ""
    sheet_row: Optional[int] = None
    lead_id: Optional[str] = None       # Supabase leads.id when triggered from CRM
    source: Optional[str] = None
    retry_count: int = 0
    agent_profile_id: Optional[str] = None

class NotesUpdate(BaseModel):
    notes: str

class PromptSave(BaseModel):
    prompt: str


# ── LiveKit dispatch helper ───────────────────────────────────────────────────

async def dispatch_call(
    phone_number: str,
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "our service",
    system_prompt: Optional[str] = None,
    agent_profile_id: Optional[str] = None,
    enabled_tools: Optional[list] = None,
    extra_meta: Optional[dict] = None,
) -> dict:
    lk_url = os.getenv("LIVEKIT_URL", "")
    lk_key = os.getenv("LIVEKIT_API_KEY", "")
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
    trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")

    if not all([lk_url, lk_key, lk_secret, trunk_id]):
        raise ValueError("LiveKit credentials or OUTBOUND_TRUNK_ID not configured")

    room_name = f"outbound-{uuid.uuid4().hex[:12]}"

    meta = {
        "phone_number": phone_number,
        "lead_name": lead_name,
        "business_name": business_name,
        "service_type": service_type,
    }
    if system_prompt:
        meta["system_prompt"] = system_prompt
    if agent_profile_id:
        meta["agent_profile"] = agent_profile_id
    if enabled_tools:
        meta["enabled_tools"] = enabled_tools
    if extra_meta:
        meta.update(extra_meta)

    meta_str = json.dumps(meta)

    try:
        from livekit import api as lkapi
        from livekit.protocol.sip import CreateSIPParticipantRequest
    except ImportError as exc:
        raise ValueError(f"livekit-api package missing or outdated: {exc}")

    try:
        async with lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret) as lk:
            # Dial the phone number via SIP trunk
            sip_req = CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity=f"sip_{phone_number.replace('+', '')}",
                participant_name=lead_name,
                participant_metadata=meta_str,
            )
            await lk.sip.create_sip_participant(sip_req)
            logger.info("SIP call dispatched to %s in room %s", phone_number, room_name)

            # Give LiveKit a moment to register the room before dispatching the agent
            await asyncio.sleep(1)

            # Dispatch Priya agent to the room
            try:
                from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest
                await lk.agent_dispatch.create_dispatch(
                    CreateAgentDispatchRequest(
                        room=room_name,
                        agent_name="outbound-agent",
                        metadata=meta_str,
                    )
                )
                logger.info("Agent dispatched to room %s", room_name)
            except Exception as dispatch_exc:
                # Worker-mode agents pick up the room automatically — non-fatal
                logger.warning("Agent dispatch skipped: %s", dispatch_exc)

    except Exception as exc:
        logger.error("dispatch_call failed for %s: %s", phone_number, exc)
        raise ValueError(str(exc))

    await db.log_error("server", f"Call dispatched to {phone_number} in room {room_name}", "", "info")
    return {"room": room_name, "phone": phone_number, "status": "dispatched"}


# ── Campaign runner ───────────────────────────────────────────────────────────

async def run_campaign(campaign_id: str) -> None:
    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign.get("status") not in ("active", "scheduled"):
        return

    contacts = json.loads(campaign.get("contacts_json", "[]"))
    delay = int(campaign.get("call_delay_seconds", 3))
    system_prompt = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
    business_name = "our company"
    service_type = "our service"

    dispatched = 0
    failed = 0

    await db.log_error("server", f"Campaign '{campaign['name']}' started -- {len(contacts)} contacts", "", "info")

    for contact in contacts:
        phone = contact.get("phone") or contact.get("phone_number", "")
        lead_name = contact.get("name") or contact.get("lead_name", "there")
        if not phone:
            failed += 1
            continue
        try:
            await dispatch_call(
                phone_number=phone,
                lead_name=lead_name,
                business_name=business_name,
                service_type=service_type,
                system_prompt=system_prompt,
                agent_profile_id=agent_profile_id,
            )
            dispatched += 1
        except Exception as exc:
            logger.error("Campaign call failed for %s: %s", phone, exc)
            failed += 1
        if delay > 0:
            await asyncio.sleep(delay)

    await db.update_campaign_run_stats(campaign_id, dispatched, failed)
    await db.log_error("server", f"Campaign '{campaign['name']}' finished: {dispatched} dispatched, {failed} failed", "", "info")


def _schedule_campaign(campaign: dict) -> None:
    cid = campaign["id"]
    stype = campaign.get("schedule_type", "once")
    stime = campaign.get("schedule_time", "09:00")

    try:
        hour, minute = (int(x) for x in stime.split(":")[:2])
    except Exception:
        hour, minute = 9, 0

    job_id = f"campaign_{cid}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if stype == "once":
        scheduler.add_job(
            run_campaign, "date",
            run_date=datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0),
            args=[cid], id=job_id, replace_existing=True,
            misfire_grace_time=3600,
        )
    elif stype == "daily":
        scheduler.add_job(
            run_campaign, CronTrigger(hour=hour, minute=minute),
            args=[cid], id=job_id, replace_existing=True,
        )
    elif stype == "weekdays":
        scheduler.add_job(
            run_campaign, CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            args=[cid], id=job_id, replace_existing=True,
        )


# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    db.init_db()
    scheduler.start()
    # Load settings from Supabase into env so OUTBOUND_TRUNK_ID etc. are available
    try:
        adb = await db._adb()
        result = await adb.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value"):
                os.environ[row["key"]] = row["value"]
        logger.info("Loaded %d settings from Supabase", len(result.data or []))
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)
    # Schedule all active campaigns
    try:
        campaigns = await db.get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active":
                _schedule_campaign(c)
        logger.info("Scheduled %d active campaigns", sum(1 for c in campaigns if c.get("status") == "active"))
    except Exception as exc:
        logger.warning("Could not load campaign schedules: %s", exc)
    await db.log_error("server", "OutboundAI server started", "", "info")


@app.on_event("shutdown")
async def shutdown() -> None:
    scheduler.shutdown(wait=False)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    idx = os.path.join(UI_DIR, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return JSONResponse({"status": "OutboundAI running", "ui": "/ui/index.html"})


@app.get("/api/health")
async def health():
    config = {
        "livekit": bool(os.getenv("LIVEKIT_URL")),
        "gemini": bool(os.getenv("GOOGLE_API_KEY")),
        "supabase": bool(os.getenv("SUPABASE_URL")),
        "vobiz": bool(os.getenv("OUTBOUND_TRUNK_ID")),
        "twilio": bool(os.getenv("TWILIO_ACCOUNT_SID")),
        "calcom": bool(os.getenv("CALCOM_API_KEY")),
    }
    return {"status": "ok", "config": config, "timestamp": datetime.now().isoformat()}


@app.get("/api/sip/trunks")
async def list_sip_trunks():
    """Debug endpoint — lists all SIP trunks in your LiveKit project so you can find the correct OUTBOUND_TRUNK_ID."""
    lk_url = os.getenv("LIVEKIT_URL", "")
    lk_key = os.getenv("LIVEKIT_API_KEY", "")
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
    if not all([lk_url, lk_key, lk_secret]):
        raise HTTPException(status_code=500, detail="LiveKit credentials not configured")
    try:
        from livekit import api as lkapi
        from livekit.protocol.sip import ListSIPOutboundTrunkRequest
        async with lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret) as lk:
            resp = await lk.sip.list_sip_outbound_trunk(ListSIPOutboundTrunkRequest())
            trunks = [
                {"id": t.sid, "name": t.name, "address": t.address, "numbers": list(t.numbers)}
                for t in (resp.items or [])
            ]
        current = os.getenv("OUTBOUND_TRUNK_ID", "")
        return {"trunks": trunks, "current_env": current, "match": any(t["id"] == current for t in trunks)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/sip/create-trunk")
async def create_sip_trunk():
    """Create a new SIP outbound trunk using Vobiz credentials and auto-update OUTBOUND_TRUNK_ID."""
    lk_url = os.getenv("LIVEKIT_URL", "")
    lk_key = os.getenv("LIVEKIT_API_KEY", "")
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
    sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
    sip_user = os.getenv("VOBIZ_USERNAME", "")
    sip_password = os.getenv("VOBIZ_PASSWORD", "")
    outbound_num = os.getenv("VOBIZ_OUTBOUND_NUMBER", "")

    if not all([lk_url, lk_key, lk_secret, sip_domain]):
        raise HTTPException(status_code=500, detail="LiveKit or Vobiz credentials not configured")

    try:
        from livekit import api as lkapi
        from livekit.protocol.sip import (
            CreateSIPOutboundTrunkRequest,
            SIPOutboundTrunkInfo,
        )

        trunk_info = SIPOutboundTrunkInfo(
            name="Vobiz Outbound",
            address=sip_domain,
            numbers=[outbound_num] if outbound_num else [],
            auth_username=sip_user,
            auth_password=sip_password,
        )

        async with lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret) as lk:
            created = await lk.sip.create_sip_outbound_trunk(
                CreateSIPOutboundTrunkRequest(trunk=trunk_info)
            )
            new_id = created.sip_trunk_id
            logger.info("Created SIP outbound trunk: %s", new_id)

            # Auto-update the env and persist to DB
            os.environ["OUTBOUND_TRUNK_ID"] = new_id
            await db.save_settings({"OUTBOUND_TRUNK_ID": new_id})

            return {
                "success": True,
                "trunk_id": new_id,
                "name": created.name,
                "address": created.address,
                "numbers": list(created.numbers),
                "message": f"Trunk created and OUTBOUND_TRUNK_ID updated to {new_id}",
            }
    except Exception as exc:
        logger.error("Failed to create SIP trunk: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/call/single")
async def call_single(req: SingleCallRequest):
    try:
        result = await dispatch_call(
            phone_number=req.phone_number,
            lead_name=req.lead_name,
            business_name=req.business_name,
            service_type=req.service_type,
            system_prompt=req.system_prompt,
            agent_profile_id=req.agent_profile_id,
        )
        return {"success": True, **result}
    except Exception as exc:
        await db.log_error("server", f"Single call failed: {exc}", str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/call/batch")
async def call_batch(req: BatchCallRequest):
    results = []
    for contact in req.contacts:
        phone = contact.get("phone") or contact.get("phone_number", "")
        lead_name = contact.get("name") or contact.get("lead_name", "there")
        try:
            r = await dispatch_call(
                phone_number=phone,
                lead_name=lead_name,
                business_name=req.business_name,
                service_type=req.service_type,
                system_prompt=req.system_prompt,
                agent_profile_id=req.agent_profile_id,
            )
            results.append({"phone": phone, "status": "dispatched", **r})
        except Exception as exc:
            results.append({"phone": phone, "status": "failed", "error": str(exc)})
        if req.call_delay_seconds > 0:
            await asyncio.sleep(req.call_delay_seconds)
    return {"results": results, "total": len(results)}


@app.post("/api/call/csv")
async def call_csv(
    file: UploadFile = File(...),
    business_name: str = "our company",
    service_type: str = "our service",
    call_delay_seconds: int = 3,
):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    contacts = list(reader)
    results = []
    for row in contacts:
        phone = row.get("phone") or row.get("Phone") or row.get("phone_number", "")
        lead_name = row.get("name") or row.get("Name") or row.get("lead_name", "there")
        if not phone:
            results.append({"phone": "", "status": "skipped", "error": "no phone"})
            continue
        try:
            r = await dispatch_call(
                phone_number=phone.strip(),
                lead_name=lead_name.strip(),
                business_name=business_name,
                service_type=service_type,
            )
            results.append({"phone": phone, "status": "dispatched"})
        except Exception as exc:
            results.append({"phone": phone, "status": "failed", "error": str(exc)})
        if call_delay_seconds > 0:
            await asyncio.sleep(call_delay_seconds)
    return {"results": results, "total": len(results)}


@app.get("/api/calls")
async def get_calls(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    calls = await db.get_all_calls(page=page, limit=limit)
    return {"calls": calls, "page": page, "limit": limit}


@app.get("/api/calls/{call_id}/notes")
async def get_call_notes(call_id: str):
    calls = await db.get_all_calls(limit=1000)
    for c in calls:
        if c["id"] == call_id:
            return {"notes": c.get("notes", "")}
    raise HTTPException(status_code=404, detail="Call not found")


@app.put("/api/calls/{call_id}/notes")
async def update_call_notes(call_id: str, body: NotesUpdate):
    ok = await db.update_call_notes(call_id, body.notes)
    if not ok:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"success": True}


@app.get("/api/appointments")
async def get_appointments(date: Optional[str] = None):
    appts = await db.get_all_appointments(date_filter=date)
    return {"appointments": appts}


@app.post("/api/appointments")
async def create_appointment(req: AppointmentCreate):
    booking_id = await db.insert_appointment(req.name, req.phone, req.date, req.time, req.service)
    return {"success": True, "booking_id": booking_id}


@app.delete("/api/appointments/{appointment_id}")
async def cancel_appointment(appointment_id: str):
    ok = await db.cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Appointment not found or already cancelled")
    return {"success": True}


@app.get("/api/stats")
async def get_stats():
    return await db.get_stats()


@app.get("/api/contacts")
async def get_contacts():
    contacts = await db.get_contacts()
    return {"contacts": contacts}


@app.get("/api/contacts/{phone}/memory")
async def get_contact_memory(phone: str):
    memories = await db.get_contact_memory(phone)
    calls = await db.get_calls_by_phone(phone)
    appointments = await db.get_appointments_by_phone(phone)
    return {"phone": phone, "memories": memories, "calls": calls, "appointments": appointments}


@app.get("/api/campaigns")
async def get_campaigns():
    campaigns = await db.get_all_campaigns()
    return {"campaigns": campaigns}


@app.post("/api/campaigns")
async def create_campaign(req: CampaignCreate):
    contacts_json = json.dumps(req.contacts)
    prompt = req.system_prompt or build_prompt(
        business_name=req.business_name,
        service_type=req.service_type,
    )
    campaign_id = await db.create_campaign(
        name=req.name,
        contacts_json=contacts_json,
        schedule_type=req.schedule_type,
        schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds,
        system_prompt=prompt,
        agent_profile_id=req.agent_profile_id,
    )
    campaign = await db.get_campaign(campaign_id)
    if campaign:
        _schedule_campaign(campaign)
    return {"success": True, "campaign_id": campaign_id}


@app.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@app.put("/api/campaigns/{campaign_id}/status")
async def update_campaign_status(campaign_id: str, body: dict):
    status = body.get("status", "")
    if status not in ("active", "paused", "cancelled", "completed"):
        raise HTTPException(status_code=400, detail="Invalid status")
    ok = await db.update_campaign_status(campaign_id, status)
    if not ok:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if status in ("paused", "cancelled", "completed"):
        job_id = f"campaign_{campaign_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
    elif status == "active":
        campaign = await db.get_campaign(campaign_id)
        if campaign:
            _schedule_campaign(campaign)
    return {"success": True}


@app.delete("/api/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str):
    job_id = f"campaign_{campaign_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    ok = await db.delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"success": True}


@app.post("/api/campaigns/{campaign_id}/run")
async def run_campaign_now(campaign_id: str):
    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    asyncio.create_task(run_campaign(campaign_id))
    return {"success": True, "message": "Campaign run started"}


@app.get("/api/settings")
async def get_settings():
    settings = await db.get_all_settings()
    return {"settings": settings}


@app.post("/api/settings")
async def save_settings(body: dict):
    await db.save_settings(body)
    # Reload into env
    for k, v in body.items():
        if v:
            os.environ[k] = str(v)
    return {"success": True}


@app.get("/api/logs")
async def get_logs(
    level: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
):
    logs = await db.get_logs(level=level, source=source, limit=limit)
    return {"logs": logs}


@app.delete("/api/logs")
async def clear_logs():
    await db.clear_errors()
    return {"success": True}


@app.get("/api/agent-profiles")
async def get_agent_profiles():
    profiles = await db.get_all_agent_profiles()
    return {"profiles": profiles}


@app.post("/api/agent-profiles")
async def create_agent_profile(req: AgentProfileCreate):
    import json as _json
    profile_id = await db.create_agent_profile(
        name=req.name,
        voice=req.voice,
        model=req.model,
        system_prompt=req.system_prompt,
        enabled_tools=_json.dumps(req.enabled_tools),
        is_default=req.is_default,
    )
    return {"success": True, "profile_id": profile_id}


@app.put("/api/agent-profiles/{profile_id}")
async def update_agent_profile(profile_id: str, body: dict):
    ok = await db.update_agent_profile(profile_id, body)
    if not ok:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"success": True}


@app.delete("/api/agent-profiles/{profile_id}")
async def delete_agent_profile(profile_id: str):
    ok = await db.delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"success": True}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def set_default_profile(profile_id: str):
    await db.set_default_agent_profile(profile_id)
    return {"success": True}


@app.get("/api/prompt")
async def get_prompt():
    custom = await db.get_setting("CUSTOM_SYSTEM_PROMPT", "")
    return {"prompt": custom or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(custom)}


@app.post("/api/prompt")
async def save_prompt(body: PromptSave):
    await db.set_setting("CUSTOM_SYSTEM_PROMPT", body.prompt)
    return {"success": True}


# ── n8n Webhook — triggered by n8n when a new lead arrives ───────────────────

@app.post("/api/call/webhook")
async def call_webhook(req: N8nWebhookRequest):
    """
    Called by n8n when a new lead row appears in Google Sheet (Status=new)
    or when retrying a no_answer lead. Detects language from city, builds
    language-specific metadata, and dispatches the outbound call.
    """
    language = detect_language(req.city)

    # Load language-specific system prompt
    try:
        if language == "marathi":
            from prompts_mr import MARATHI_SYSTEM_PROMPT as lang_prompt
        elif language == "bengali":
            from prompts_bn import BENGALI_SYSTEM_PROMPT as lang_prompt
        else:
            from prompts_hi import HINGLISH_SYSTEM_PROMPT as lang_prompt
    except ImportError:
        lang_prompt = None

    meta_extra = {
        "language": language,
        "city": req.city,
        "symptom": req.symptom,
        "sheet_row": req.sheet_row,
        "lead_id": req.lead_id,
        "retry_count": req.retry_count,
        "source": req.source or "",
        "business_name": "Aarogya India",
        "service_type": "3A Piles Kit",
    }

    try:
        result = await dispatch_call(
            phone_number=req.phone,
            lead_name=req.name,
            business_name="Aarogya India",
            service_type="3A Piles Kit",
            system_prompt=lang_prompt,
            agent_profile_id=req.agent_profile_id,
            extra_meta=meta_extra,
        )
        # Mark CRM lead as "ai_calling" so the dashboard shows live status
        if req.lead_id:
            await db.update_crm_lead_ai_status(req.lead_id, "ai_calling")

        # Notify Lovable CRM that the call has been dispatched
        await send_crm_callback(
            phone=req.phone,
            outcome="other",
            lead_name=req.name,
            notes="AI call dispatched",
            raw={"source": req.source or "webhook", "status": "ai_calling"},
        )

        await db.log_error(
            "webhook",
            f"Call dispatched: {req.phone} ({language}) lead_id={req.lead_id} row={req.sheet_row}",
            "",
            "info",
        )
        return {"success": True, "language": language, **result}
    except Exception as exc:
        await db.log_error("webhook", f"Call failed for {req.phone}: {exc}", str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


# ── Orders endpoints ──────────────────────────────────────────────────────────

@app.get("/api/orders")
async def get_orders(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    orders = await db.get_all_orders(page=page, limit=limit)
    return {"orders": orders, "page": page, "limit": limit}


@app.get("/api/orders/stats")
async def get_order_stats():
    return await db.get_order_stats()


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    order = await db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.put("/api/orders/{order_id}/status")
async def update_order_status(order_id: str, body: dict):
    status = body.get("status", "")
    if status not in ("pending", "confirmed", "dispatched", "delivered", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    ok = await db.update_order_status(order_id, status)
    if not ok:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"success": True}
