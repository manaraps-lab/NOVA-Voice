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
from typing import Optional

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
    )
    return {"id": meta["id"], "filename": meta["filename"], "duration": meta["duration"]}


@router.get("/nova/options")
def nova_options():
    return {
        "engines": {k: {"label": v["label"]} for k, v in nj.ENGINES.items()},
        "styles": {k: {"label": v["label"]} for k, v in nj.STYLES.items()},
        "ages": nj.AGES,
        "voices": nj.FAST_VOICES,
    }


@router.post("/nova/jobs")
async def nova_create(req: JobRequest):
    if req.engine not in nj.ENGINES:
        raise HTTPException(400, "محرك غير معروف")
    if req.style not in nj.STYLES:
        raise HTTPException(400, "أسلوب إلقاء غير معروف")
    text = prepare_text(req.text, req.language)
    chunks = nj.split_for_job(text, nj.ENGINES[req.engine]["chunk_chars"])
    if not chunks:
        raise HTTPException(400, "النص فارغ")
    settings = nj.JobSettings(**{**req.model_dump(), "text": text})
    try:
        job = nj.MANAGER.create(settings, chunks)
    except RuntimeError:
        raise HTTPException(409, "هناك عملية توليد قيد التنفيذ")
    from services.tts_backend import ensure_engine_licence, get_backend_class
    cls = get_backend_class(nj.ENGINES[req.engine]["engine_id"])
    ensure_engine_licence(cls)   # raises the structured 403 the licence dialog listens for
    ok, msg = cls.is_available()
    if not ok:
        raise HTTPException(400, f"المحرك غير جاهز بعد: {msg}")
    from services.tts_backend import get_engine_instance_for
    job._task = asyncio.create_task(job.run(get_backend=get_engine_instance_for, finalize=_finalize))
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
def nova_pause(job_id: str):
    job = _job(job_id)
    job.pause()
    return job.progress()


@router.post("/nova/jobs/{job_id}/resume")
def nova_resume(job_id: str):
    job = _job(job_id)
    job.resume()
    return job.progress()


@router.post("/nova/jobs/{job_id}/cancel")
def nova_cancel(job_id: str):
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
