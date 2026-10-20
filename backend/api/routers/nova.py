"""NOVA Voice API: background speech jobs, options and device status.

Jobs run as asyncio tasks inside the backend, independent of the window that
started them. See ``services/nova_jobs.py`` for the control model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import DATA_DIR
from services import nova_jobs as nj

logger = logging.getLogger("omnivoice.nova")
router = APIRouter()


class JobRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    engine: str = "fast"
    style: str = "natural"
    gender: str = "female"
    age: str = "young adult"
    voice: Optional[str] = None
    speed: float = Field(1.0, ge=0.6, le=1.6)
    pitch: float = Field(0.0, ge=-6.0, le=6.0)
    depth: int = Field(0, ge=-2, le=2)
    language: str = "ar"
    quality: str = "balanced"
    wav_bits: Literal[16, 24, 32] = 16


def prepare_text(text: str, language: str) -> str:
    """Normalise before synthesis: Unicode form, numbers to words, no tatweel."""
    import unicodedata
    out = unicodedata.normalize("NFC", text).replace("ـ", "")
    try:
        from services.text_normalization import normalize_text
        out = normalize_text(out, language)
    except Exception:  # noqa: BLE001 — normalisation is an improvement, not a requirement
        logger.warning("nova: text normalisation skipped", exc_info=True)
    return out.strip()


async def _finalize(audio, sr, job: nj.NovaJob) -> dict:
    from api.routers.generation import _apply_effect_chain, _finalize_generation
    audio = await asyncio.to_thread(_apply_effect_chain, audio, sr, "broadcast")
    s = job.settings
    _, meta = await _finalize_generation(
        audio, sr, text=s.text, history_mode="nova", ref_audio_path=None,
        language=s.language, instruct=nj.resolve_instruct(s) if s.engine == "quality" else "",
        resolved_profile_id=None, used_seed=None, start_time=time.time() - job.elapsed(),
        wav_bits=s.wav_bits,
    )
    return {"id": meta["id"], "filename": meta["filename"], "duration": meta["duration"]}


@router.get("/nova/options")
def nova_options():
    return {
        "engines": {k: {"label": v["label"], "model": v["model"], "note": v["note"]} for k, v in nj.ENGINES.items()},
        "quality_levels": nj.QUALITY_LEVELS,
        "styles": {k: {"label": v["label"]} for k, v in nj.STYLES.items()},
        "ages": nj.AGES,
        "voices": nj.FAST_VOICES,
    }


@router.post("/nova/jobs")
async def nova_create(req: JobRequest):
    if req.engine not in nj.ENGINES:
        raise HTTPException(400, "محرك غير معروف")
    if req.quality not in nj.QUALITY_LEVELS:
        raise HTTPException(400, "مستوى جودة غير معروف")
    if req.style not in nj.STYLES:
        raise HTTPException(400, "أسلوب إلقاء غير معروف")
    text = prepare_text(req.text, req.language)
    chunks = nj.split_for_job(text, nj.ENGINES[req.engine]["chunk_chars"])
    if req.engine in ("quality", "expressive"):
        chunks = nj.shorten_first(chunks)
    if not chunks:
        raise HTTPException(400, "النص فارغ")
    settings = nj.JobSettings(**{**req.model_dump(), "text": text})
    if nj.MANAGER.active() is not None:
        raise HTTPException(409, "هناك عملية توليد قيد التنفيذ")
    # Validate BEFORE registering the job: a job registered and then rejected
    # would stay "queued" forever and block every later request.
    from services.tts_backend import ensure_engine_licence, get_backend_class
    cls = get_backend_class(nj.ENGINES[req.engine]["engine_id"])
    ensure_engine_licence(cls)   # raises the structured 403 the licence dialog listens for
    ok, msg = cls.is_available()
    if not ok:
        raise HTTPException(400, f"المحرك غير جاهز بعد: {msg}")
    try:
        job = nj.MANAGER.create(settings, chunks)
    except RuntimeError:
        raise HTTPException(409, "هناك عملية توليد قيد التنفيذ")
    try:
        # Hand back RAM held by any other resident engine (matters on 8 GB machines).
        from services.engine_memory import evict_other_tts_engines
        await evict_other_tts_engines(nj.ENGINES[req.engine]["engine_id"])
        from services.tts_backend import get_engine_instance_for
        job._task = asyncio.create_task(job.run(get_backend=get_engine_instance_for, finalize=_finalize))
    except Exception as exc:
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"[:400]
        raise
    return job.progress()


def _job(job_id: str) -> nj.NovaJob:
    job = nj.MANAGER.get(job_id)
    if job is None:
        raise HTTPException(404, "العملية غير موجودة")
    return job


@router.get("/nova/jobs/active")
def nova_active():
    job = nj.MANAGER.active()
    return job.progress() if job else {"state": "idle"}


@router.get("/nova/jobs/{job_id}")
def nova_status(job_id: str):
    return _job(job_id).progress()


@router.post("/nova/jobs/{job_id}/pause")
async def nova_pause(job_id: str):
    job = _job(job_id)
    job.pause()
    return job.progress()


@router.post("/nova/jobs/{job_id}/resume")
async def nova_resume(job_id: str):
    job = _job(job_id)
    job.resume()
    return job.progress()


@router.post("/nova/jobs/{job_id}/cancel")
async def nova_cancel(job_id: str):
    job = _job(job_id)
    job.cancel()
    return job.progress()


_psutil_primed = False


@router.get("/nova/system")
def nova_system():
    """Device indicators shown while rendering."""
    import psutil
    global _psutil_primed
    if not _psutil_primed:
        psutil.cpu_percent(interval=None)
        _psutil_primed = True
    vm = psutil.virtual_memory()
    app_rss = 0
    try:
        me = psutil.Process()
        procs = [me] + me.children(recursive=True)
        for p in procs:
            try:
                app_rss += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:  # noqa: BLE001
        pass
    disk = shutil.disk_usage(DATA_DIR if os.path.isdir(DATA_DIR) else os.path.expanduser("~"))
    battery = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = {"percent": round(b.percent), "plugged": bool(b.power_plugged)}
    except Exception:  # noqa: BLE001
        pass
    temp = None
    try:
        temps = psutil.sensors_temperatures()  # not available on Windows
        for entries in temps.values():
            if entries:
                temp = round(max(e.current for e in entries), 1)
                break
    except Exception:  # noqa: BLE001
        pass
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_percent": vm.percent,
        "ram_used_gb": round((vm.total - vm.available) / 2**30, 2),
        "ram_total_gb": round(vm.total / 2**30, 2),
        "app_ram_gb": round(app_rss / 2**30, 2),
        "disk_free_gb": round(disk.free / 2**30, 1),
        "battery": battery,
        "temperature_c": temp,
    }


# ── Engine status panel ────────────────────────────────────────────────────
# Weight repositories per engine, used only to say whether the model is already
# on disk (a model that is not cached is fetched on first use).
_WEIGHTS = {"fast": "Supertone/supertonic-3", "quality": "k2-fsa/OmniVoice", "expressive": "openbmb/VoxCPM2"}
_weights_cache: dict[str, tuple[float, Optional[bool]]] = {}


def _weights_cached(key: str) -> Optional[bool]:
    now = time.monotonic()
    hit = _weights_cache.get(key)
    if hit and now - hit[0] < 30:
        return hit[1]
    value: Optional[bool]
    try:
        from api.routers.setup.models import is_cached
        value = bool(is_cached(_WEIGHTS[key]))
    except Exception:  # noqa: BLE001
        value = None
    _weights_cache[key] = (now, value)
    return value


# What the project's own research says about each engine in Arabic, and the RAM
# it realistically needs on a CPU-only machine (estimates, shown as such).
_ENGINE_INFO = {
    "fast": {
        "badge": "fastest", "min_ram_gb": 4,
        "arabic_note": "العربية مدعومة رسميًّا (31 لغة). نموذج صغير (نحو 99 مليون معامل) وسريع جدًّا على المعالج. لا توجد أرقام منشورة للعربية وحدها، فالحكم بالأذن.",
    },
    "quality": {
        "badge": "best_arabic", "min_ram_gb": 8,
        "arabic_note": "الأقوى توثيقًا في العربية: خطأ نطق 1.39% في اختبار متعدد اللغات (ElevenLabs: 1.67%)، و1.92% على FLEURS للعربية الفصحى. يتحكم بالجنس والعمر والنبرة.",
    },
    "expressive": {
        "badge": "studio", "min_ram_gb": 16,
        "arabic_note": "يدعم العربية بين 30 لغة بجودة استوديو 48 كيلوهرتز، ومتوسط الخطأ المنشور 1.68% دون تفصيل للعربية. نموذج بحجم 2 مليار معامل: ثقيل على ذاكرة 8 غيغابايت.",
    },
}


def _fit(total_gb: float, need_gb: float) -> str:
    # Judge by the nominal size: a "8 GB" laptop reports ~7.7 GB usable.
    nominal = round(total_gb)
    if nominal >= need_gb + 2:
        return "ok"
    if nominal >= need_gb:
        return "tight"
    return "heavy"


def _engine_row(key: str, meta: dict) -> dict:
    from services import tts_backend
    eid = meta["engine_id"]
    row = {
        "key": key, "engine_id": eid, "label": meta["label"], "model": meta["model"], "note": meta["note"],
        "available": False, "reason": "", "loaded": False,
        "installable": False, "installed": None, "install": None,
        "weights_cached": _weights_cached(key),
    }
    extra = _ENGINE_INFO.get(key, {})
    row["badge"] = extra.get("badge")
    row["arabic_note"] = extra.get("arabic_note", "")
    row["min_ram_gb"] = extra.get("min_ram_gb")
    try:
        import psutil
        total = psutil.virtual_memory().total / 2**30
        row["system_ram_gb"] = round(total, 1)
        row["fit"] = _fit(total, extra["min_ram_gb"]) if extra.get("min_ram_gb") else None
    except Exception:  # noqa: BLE001
        row["system_ram_gb"] = None
        row["fit"] = None
    try:
        cls = tts_backend.get_backend_class(eid)
        ok, msg = cls.is_available()
        row["available"] = bool(ok)
        if not ok:
            row["reason"] = tts_backend._mask_hf_tokens(str(msg))[:220]
        row["loaded"] = cls in tts_backend._ENGINE_INSTANCES
    except Exception as exc:  # noqa: BLE001
        row["reason"] = f"{type(exc).__name__}: {exc}"[:220]
    try:
        from services import sidecar_install
        if sidecar_install.get_spec(eid) is not None:
            row["installable"] = True
            st = sidecar_install.get_status(eid)
            row["installed"] = bool(st.get("installed"))
            job = st.get("job")
            if job:
                row["install"] = {
                    "state": job.get("state"),
                    "error": (str(job.get("error"))[:200] if job.get("error") else None),
                    "weights_progress": job.get("weights_progress"),
                    "steps": [{"id": x.get("id"), "state": x.get("state")} for x in job.get("steps", [])],
                }
    except Exception:  # noqa: BLE001
        pass
    return row


@router.get("/nova/engines")
async def nova_engines():
    """Which engines can render right now (green) and which cannot (red)."""
    rows = await asyncio.wait_for(
        asyncio.gather(*[asyncio.to_thread(_engine_row, k, v) for k, v in nj.ENGINES.items()]),
        timeout=25,
    )
    return {"engines": rows, "busy": nj.MANAGER.active() is not None}
