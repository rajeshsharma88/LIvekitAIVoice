import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv

# Patch SSL before any network import
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools
from language import detect_language
from tools import SalesTools

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value"):
                os.environ[row["key"]] = row["value"]
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# Import Google plugin paths
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


def _build_session(tools: list, system_prompt: str) -> AgentSession:
    """Build AgentSession with Gemini Live or pipeline fallback."""
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        try:
            from google.genai import types as _gt
            _realtime_input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=2000,
                    prefix_padding_ms=200,
                ),
            )
            _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
            _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
            logger.info("Silence-prevention config applied")
        except Exception as _cfg_err:
            logger.warning("Could not build silence-prevention config: %s", _cfg_err)
            _realtime_input_cfg = None
            _session_resumption_cfg = None
            _ctx_compression_cfg = None

        realtime_kwargs: dict = dict(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        if _realtime_input_cfg is not None:
            realtime_kwargs["realtime_input_config"]      = _realtime_input_cfg
            realtime_kwargs["session_resumption"]         = _session_resumption_cfg
            realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

        return AgentSession(llm=RealtimeClass(**realtime_kwargs), tools=tools)

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: pipeline (Deepgram STT + Gemini LLM + Google TTS)")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    llm_inst = _google_llm(model=gemini_model)
    return AgentSession(stt=stt, llm=llm_inst, tts=tts, tools=tools)


def _extract_phone(ctx: agents.JobContext) -> Optional[str]:
    """Extract caller phone from SIP participant attributes."""
    for p in ctx.room.remote_participants.values():
        attrs = p.attributes or {}
        for key in ("sip.callFrom", "sip.phoneNumber", "phoneNumber", "phone"):
            val = attrs.get(key, "")
            if val:
                return val
        if p.identity and p.identity.startswith("sip_"):
            return p.identity[4:]
    return None


def _extract_metadata(ctx: agents.JobContext) -> dict:
    """Parse job metadata JSON for lead_name, phone, system_prompt, etc."""
    try:
        raw = ctx.job.metadata or ""
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _load_language_prompt(language: str, lead_name: str, symptom: str) -> str:
    """Load the correct language prompt and substitute lead-specific variables."""
    try:
        if language == "marathi":
            from prompts_mr import MARATHI_SYSTEM_PROMPT as raw
        elif language == "bengali":
            from prompts_bn import BENGALI_SYSTEM_PROMPT as raw
        else:
            from prompts_hi import HINGLISH_SYSTEM_PROMPT as raw
    except ImportError:
        from prompts import DEFAULT_SYSTEM_PROMPT as raw

    return raw.replace("{lead_name}", lead_name).replace("{symptom}", symptom or "piles")


async def entrypoint(ctx: agents.JobContext) -> None:
    """Main entrypoint for every inbound/outbound call."""
    await ctx.connect()
    await _log("info", f"Agent connected to room: {ctx.room.name}")

    meta = _extract_metadata(ctx)
    phone = meta.get("phone_number") or _extract_phone(ctx) or "unknown"
    lead_name = meta.get("lead_name", "there")
    city = meta.get("city", "")
    symptom = meta.get("symptom", "")
    sheet_row = meta.get("sheet_row")
    retry_count = int(meta.get("retry_count", 0))
    source = meta.get("source", "")

    # Language: use pre-detected value from n8n meta, or detect from city
    language = meta.get("language") or detect_language(city)

    # System prompt: use language-specific prompt (passed via meta or built from language)
    system_prompt = meta.get("system_prompt") or _load_language_prompt(language, lead_name, symptom)

    # Agent profile overrides (voice/model)
    agent_profile = meta.get("agent_profile")
    if agent_profile:
        try:
            from db import get_agent_profile
            profile = await get_agent_profile(agent_profile)
            if profile:
                if profile.get("voice"):
                    os.environ["GEMINI_TTS_VOICE"] = profile["voice"]
                if profile.get("model"):
                    os.environ["GEMINI_MODEL"] = profile["model"]
        except Exception as exc:
            logger.warning("Could not load agent profile: %s", exc)

    enabled_tools = meta.get("enabled_tools") or await get_enabled_tools()
    tool_ctx = SalesTools(
        ctx,
        phone_number=phone,
        lead_name=lead_name,
        sheet_row=sheet_row,
        city=city,
        symptom=symptom,
        language=language,
        source=source,
    )
    tools = tool_ctx.build_tool_list(enabled_tools)

    await _log("info", f"Call: {lead_name} ({phone}) lang={language} city={city} retry={retry_count}")

    session = _build_session(tools=tools, system_prompt=system_prompt)

    input_opts = RoomInputOptions(
        noise_cancellation=noise_cancellation.BVC(),
    )

    try:
        await session.start(
            agent=Agent(instructions=system_prompt),
            room=ctx.room,
            room_input_options=input_opts,
        )
        # Wait until the room disconnects
        disconnect_event = asyncio.Event()

        @ctx.room.on("disconnected")
        def _on_disconnect(*args):
            disconnect_event.set()

        # If already disconnected, don't wait forever
        if ctx.room.connection_state != rtc.ConnectionState.CONN_CONNECTED:
            disconnect_event.set()

        await disconnect_event.wait()
    except Exception as exc:
        await _log("error", f"Session error for {phone}: {exc}", str(exc))
        raise
    finally:
        await _log("info", f"Session ended for {phone}")


if __name__ == "__main__":
    load_db_settings_to_env()
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-agent",
            api_key=os.getenv("LIVEKIT_API_KEY", ""),
            api_secret=os.getenv("LIVEKIT_API_SECRET", ""),
            ws_url=os.getenv("LIVEKIT_URL", ""),
        )
    )
