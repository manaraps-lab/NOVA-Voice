"""NOVA Voice: what this machine can run, and how fast it really does.

Pure helpers (no network, no model loading) so the rules can be tested with
made-up hardware. ``hardware_profile`` reads the machine once and caches it;
everything else takes the profile as an argument.

Sizes are estimates of what each engine needs on the machine that runs it:
system RAM when it runs on the CPU, video memory when it runs on a GPU. They
are shown to the user as estimates, never as measurements.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("omnivoice.nova")

# Needs per engine key (see nova_jobs.ENGINES): RAM for CPU runs, VRAM for GPU runs.
# ``vram`` None means the engine only runs on the CPU (Supertonic is ONNX-on-CPU).
ENGINE_NEEDS: dict[str, dict[str, Optional[float]]] = {
    "fast": {"ram": 4, "vram": None},
    "quality": {"ram": 8, "vram": 4},
    "expressive": {"ram": 16, "vram": 8},
    "compact": {"ram": 4, "vram": 2},
}

# On a GPU the larger models are both faster and better; the order differs per language.
# OmniVoice keeps first place for Arabic (best documented); Hebrew prefers VoxCPM2
# because OmniVoice saw very little Hebrew in training.
GPU_PREFERENCE: dict[str, list[str]] = {
    "ar": ["quality", "expressive", "fast", "compact"],
    "es": ["expressive", "quality", "fast", "compact"],
    "en": ["expressive", "quality", "fast", "compact"],
    "he": ["expressive", "quality", "compact"],
}

_RANK = {"ok": 2, "tight": 1, "heavy": 0}
_cache: dict[str, Any] = {"at": 0.0, "hw": None}


def classify(hw: dict) -> str:
    """``gpu`` | ``cpu_strong`` | ``cpu_mid`` | ``cpu_low``."""
    if hw.get("family") in ("cuda", "rocm") and float(hw.get("vram_gb") or 0) >= 6:
        return "gpu"
    ram = float(hw.get("ram_total_gb") or 0)
    if ram >= 24:
        return "cpu_strong"
    if ram >= 12:
        return "cpu_mid"
    return "cpu_low"


def _rate(total: float, need: float) -> str:
    # Judge by the nominal size: an "8 GB" laptop reports about 7.7 GB usable.
    nominal = round(total)
    if nominal >= need + 2:
        return "ok"
    if nominal >= need:
        return "tight"
    return "heavy"


def fit(engine_key: str, hw: dict) -> tuple[Optional[str], str]:
    """``(ok | tight | heavy | None, "gpu" | "cpu")`` for an engine on this machine."""
    need = ENGINE_NEEDS.get(engine_key)
    if not need:
        return None, "cpu"
    best, where = None, "cpu"
    ram = hw.get("ram_total_gb")
    if ram:
        best = _rate(float(ram), float(need["ram"] or 0))
    gpu_ok = hw.get("family") in ("cuda", "rocm") and need["vram"] is not None and hw.get("vram_gb")
    if gpu_ok:
        gpu_fit = _rate(float(hw["vram_gb"]), float(need["vram"]))
        if best is None or _RANK[gpu_fit] >= _RANK[best]:
            best, where = gpu_fit, "gpu"
    return best, where


def engine_order(language: str, hw: dict, base_order: list[str], languages_of: dict[str, list[str]]) -> list[str]:
    """Engines to try first-to-last for ``language`` on this machine.

    Engines that would be too heavy go last rather than disappearing, so a user
    with plenty of patience can still pick them.
    """
    order = GPU_PREFERENCE.get(language, base_order) if classify(hw) == "gpu" else base_order
    order = [k for k in order if language in languages_of.get(k, [])]
    light = [k for k in order if fit(k, hw)[0] != "heavy"]
    heavy = [k for k in order if k not in light]
    return light + heavy


def hardware_profile(refresh: bool = False, allow_probe: bool = True) -> dict:
    """CPU cores, RAM, accelerator family / name / VRAM. Cached for a minute.

    ``allow_probe=False`` never loads torch: it answers from the cache, or from
    RAM and cores alone when nothing is cached yet (the engine list polls often
    and must stay quick; the full probe is paid once by the first caller that
    allows it).
    """
    now = time.monotonic()
    if not refresh and _cache["hw"] is not None and now - _cache["at"] < 60:
        return _cache["hw"]
    if not allow_probe and _cache["hw"] is not None:
        return _cache["hw"]          # slightly stale beats a slow probe
    hw: dict[str, Any] = {
        "cpu_cores": os.cpu_count() or 1,
        "ram_total_gb": None,
        "ram_available_gb": None,
        "family": "cpu",
        "gpu_name": "",
        "vram_gb": 0.0,
    }
    try:
        import psutil
        vm = psutil.virtual_memory()
        hw["ram_total_gb"] = round(vm.total / 2**30, 1)
        hw["ram_available_gb"] = round(vm.available / 2**30, 1)
    except Exception:  # noqa: BLE001
        logger.debug("nova: RAM probe failed", exc_info=True)
    if not allow_probe:
        hw["tier"] = classify(hw)
        return hw                    # RAM and cores only; not cached, so a later full probe still runs
    try:
        from core.device_caps import detect_host_caps
        caps = detect_host_caps()
        hw["family"] = str(caps.family)
        hw["gpu_name"] = caps.device_name or ""
        hw["vram_gb"] = round(float(caps.vram_gb or 0), 1)
    except Exception:  # noqa: BLE001 — no torch or a probe failure: treat as CPU-only
        logger.debug("nova: accelerator probe failed", exc_info=True)
    hw["tier"] = classify(hw)
    _cache["hw"], _cache["at"] = hw, now
    return hw


# ── Measured speed ─────────────────────────────────────────────────────────
BENCH_TEXT: dict[str, list[str]] = {
    "ar": [
        "السلام عليكم ورحمة الله وبركاته، أهلًا بكم.",
        "هذه جملة قصيرة لقياس سرعة التوليد على جهازك.",
        "نكرر القياس على جملة ثالثة للحصول على رقم أدق.",
    ],
    "es": [
        "Hola a todos, bienvenidos a esta prueba de velocidad.",
        "Esta es una frase corta para medir la generación.",
        "Repetimos la medición con una tercera frase.",
    ],
    "en": [
        "Hello everyone, welcome to this speed test.",
        "This is a short sentence to measure generation speed.",
        "We repeat the measurement with a third sentence.",
    ],
    "he": [
        "שלום לכולם, ברוכים הבאים למבחן המהירות.",
        "זהו משפט קצר למדידת מהירות היצירה במחשב שלכם.",
        "אנחנו חוזרים על המדידה במשפט שלישי.",
    ],
}


def _bench_path() -> str:
    from core.config import DATA_DIR
    return os.path.join(str(DATA_DIR), "nova_bench.json")


def load_bench(path: Optional[str] = None) -> dict[str, dict]:
    try:
        with open(path or _bench_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_bench(engine: str, seconds_per_char: float, steps: Optional[int], audio_s_per_char: float,
                 path: Optional[str] = None) -> dict:
    """Keep the latest warm measurement for an engine."""
    entry = {
        "s_per_char": float(seconds_per_char),
        "steps": steps,
        "audio_s_per_char": float(audio_s_per_char),
        "at": int(time.time()),
    }
    data = load_bench(path)
    data[engine] = entry
    target = path or _bench_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError:
        logger.warning("nova: could not save the speed measurement", exc_info=True)
    return entry


def summarize(entry: Optional[dict]) -> Optional[dict]:
    """What the interface shows for a measurement."""
    if not entry or not entry.get("s_per_char"):
        return None
    spc = float(entry["s_per_char"])
    audio = float(entry.get("audio_s_per_char") or 0)
    return {
        "chars_per_min": round(60.0 / spc),
        "rtf": round(spc / audio, 2) if audio > 0 else None,   # seconds of work per second of speech
        "steps": entry.get("steps"),
        "at": entry.get("at"),
    }


def estimate_seconds(entry: Optional[dict], steps_now: Optional[int], chars: int) -> Optional[float]:
    """Time to render ``chars`` characters from a warm measurement, or None without one."""
    if not entry or not entry.get("s_per_char") or chars <= 0:
        return None
    spc = float(entry["s_per_char"])
    measured_steps = entry.get("steps")
    if measured_steps and steps_now:
        spc *= float(steps_now) / float(measured_steps)
    return chars * spc
