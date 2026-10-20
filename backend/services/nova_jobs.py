"""NOVA Voice: background text-to-speech jobs.

A job renders a text chunk by chunk inside the backend process, so it keeps
running whatever the window is doing (minimised, another app in front, the UI
closed and reopened). It reports real progress, supports pause / resume /
cancel, and on cancel hands back the audio rendered so far.

Control granularity is one chunk: an engine call cannot be interrupted midway,
so pause and cancel take effect when the chunk being rendered finishes. Chunks
are kept short (see ``CHUNK_CHARS``) to keep that wait small.

Progress is measured in characters, not chunks, because chunks differ in
length. The estimated time left is the measured rendering speed so far times
the characters still to render; before the first chunk is finished only a
rough per-engine prior is available and the job says so (``calibrating``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("omnivoice.nova")

# ── Engines ────────────────────────────────────────────────────────────────
# Three Arabic-capable engines from the parent project that need no reference
# clip. ``steps`` maps the quality level to the engine's own sampling-steps
# control (Supertonic 5-12, OmniVoice and VoxCPM2 up to 64).
ENGINES: dict[str, dict[str, Any]] = {
    "fast": {
        "engine_id": "supertonic3",
        "label": "سريع",
        "model": "Supertonic-3",
        "note": "الأسرع على المعالج. أصوات جاهزة بالجنس والسرعة.",
        "chunk_chars": 220,
        # Rough seconds of rendering per character on a plain CPU; only used to
        # size the "still warming up" check, never shown as an estimate.
        "prior_s_per_char": 0.03,
        "steps": {"draft": 5, "balanced": 8, "high": 10, "max": 12},
        "native_speed": True,
    },
    "quality": {
        "engine_id": "omnivoice-subprocess",
        "label": "عالي الجودة",
        "model": "OmniVoice",
        "note": "تحكم دقيق بالجنس والعمر وحدّة الصوت.",
        "chunk_chars": 160,
        "prior_s_per_char": 0.6,
        "steps": {"draft": 8, "balanced": 16, "high": 24, "max": 32},
        "native_speed": True,
    },
    "expressive": {
        "engine_id": "voxcpm2",
        "label": "الأعلى تعبيرًا",
        "model": "VoxCPM2",
        "note": "صوت استوديو 48 كيلوهرتز ويفهم وصف الإلقاء (حماسي، إخباري، سرد…).",
        "chunk_chars": 140,
        "prior_s_per_char": 1.0,
        "steps": {"draft": 6, "balanced": 10, "high": 16, "max": 24},
        "native_speed": False,
    },
}

QUALITY_LEVELS = {
    "draft": "مسودة سريعة",
    "balanced": "متوازنة",
    "high": "عالية",
    "max": "أعلى جودة",
}

# Supertonic-3 preset voices (backend/engines/supertonic3/constants.py).
FAST_VOICES = {
    "male": ["M1", "M3", "M4", "M5"],
    "female": ["F3", "F4", "F5"],
}

# ── Delivery styles ────────────────────────────────────────────────────────
# Honest model of what is controllable: speed, pitch, and the pause between
# sentences are applied to every engine; the quality engine additionally gets a
# pitch tag (and the whisper tag for "همس"). Emotion proper is not claimed.
STYLES: dict[str, dict[str, Any]] = {
    "natural":      {"label": "طبيعي",  "speed": 1.00, "semitones": 0.0,  "pause_ms": 250},
    "news":         {"label": "إخباري", "speed": 1.08, "semitones": 0.0,  "pause_ms": 180},
    "narration":    {"label": "سرد",    "speed": 0.92, "semitones": -0.5, "pause_ms": 420},
    "enthusiastic": {"label": "حماسي",  "speed": 1.12, "semitones": 1.0,  "pause_ms": 160},
    "calm":         {"label": "هادئ",   "speed": 0.88, "semitones": -1.0, "pause_ms": 450},
    "dramatic":     {"label": "درامي",  "speed": 0.90, "semitones": -1.5, "pause_ms": 520},
    "whisper":      {"label": "همس",    "speed": 0.92, "semitones": 0.0,  "pause_ms": 380},
}

AGES = {"child": "طفل", "teenager": "مراهق", "young adult": "شاب", "middle-aged": "متوسط العمر", "elderly": "كبير السن"}
PITCH_TAGS = ["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"]

SENTENCE_END = re.compile(r"(?<=[.!?؟؛…\n])\s+")


def split_for_job(text: str, max_chars: int) -> list[str]:
    """Sentence-aligned chunks no longer than ``max_chars`` (comma/space fallback)."""
    text = (text or "").strip()
    if not text:
        return []
    sentences = [s.strip() for s in SENTENCE_END.split(text) if s.strip()]
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        pieces = [s]
        if len(s) > max_chars:
            pieces = _split_long(s, max_chars)
        for p in pieces:
            if cur and len(cur) + 1 + len(p) > max_chars:
                chunks.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _split_long(s: str, max_chars: int) -> list[str]:
    out: list[str] = []
    while len(s) > max_chars:
        cut = max(s.rfind("،", 0, max_chars), s.rfind(",", 0, max_chars), s.rfind(" ", 0, max_chars))
        if cut < max_chars // 3:
            cut = max_chars
        out.append(s[:cut].strip())
        s = s[cut:].strip(" ،,")
    if s:
        out.append(s)
    return out


@dataclass
class JobSettings:
    text: str
    engine: str = "fast"            # key of ENGINES
    style: str = "natural"          # key of STYLES
    gender: str = "female"          # male | female
    age: str = "young adult"        # quality engine only
    voice: Optional[str] = None     # fast engine preset (M1.. / F3..)
    speed: float = 1.0              # user multiplier on top of the style
    pitch: float = 0.0              # extra semitones, -6..+6
    depth: int = 0                  # -2..+2, voice depth (quality engine tag)
    language: str = "ar"
    quality: str = "balanced"       # key of QUALITY_LEVELS
    wav_bits: int = 16              # 16 | 24 | 32


def resolve_voice(s: JobSettings) -> str:
    pool = FAST_VOICES.get(s.gender, FAST_VOICES["female"])
    return s.voice if s.voice in pool else pool[0]


def resolve_instruct(s: JobSettings) -> str:
    """OmniVoice voice-design tags (validated vocabulary only)."""
    tags = [s.gender if s.gender in ("male", "female") else "female"]
    if s.age in AGES:
        tags.append(s.age)
    idx = max(0, min(4, 2 + int(s.depth)))
    tags.append(PITCH_TAGS[idx])
    if s.style == "whisper":
        tags.append("whisper")
    return ", ".join(tags)


def effective_speed(s: JobSettings) -> float:
    base = STYLES.get(s.style, STYLES["natural"])["speed"]
    return max(0.7, min(2.0, base * float(s.speed)))


def effective_semitones(s: JobSettings) -> float:
    base = STYLES.get(s.style, STYLES["natural"])["semitones"]
    # The quality engine takes depth as a voice-design tag; the fast engine has
    # preset voices only, so depth is applied there as a one-semitone-per-step shift.
    depth = float(s.depth) if s.engine == "fast" else 0.0
    if s.engine == "expressive":
        base = 0.0   # the description already carries the style; avoid doubling it
    return max(-8.0, min(8.0, base + float(s.pitch) + depth))


_STYLE_PHRASES = {
    "natural": "natural and clear",
    "news": "a clear, confident and authoritative news presenter, steady brisk pace",
    "narration": "a warm storyteller narrating, expressive and measured pace",
    "enthusiastic": "enthusiastic, energetic and cheerful, upbeat and fast",
    "calm": "calm, soothing and gentle, slow and relaxed",
    "dramatic": "dramatic, intense and emotional, deep and slow",
    "whisper": "whispering softly and intimately",
}
_DEPTH_PHRASES = {-2: "very deep, low-pitched", -1: "deep", 0: "", 1: "bright, slightly higher-pitched", 2: "very bright, high-pitched"}


def resolve_description(s: JobSettings) -> str:
    """Free-text voice description for VoxCPM2 (it understands delivery wording)."""
    who = " ".join(p for p in (s.age if s.age in AGES else "", s.gender if s.gender in ("male", "female") else "female", "voice") if p)
    parts = [f"a {who}".replace("a elderly", "an elderly"), _STYLE_PHRASES.get(s.style, "")]
    depth = _DEPTH_PHRASES.get(max(-2, min(2, int(s.depth))), "")
    if depth:
        parts.append(depth)
    return ", ".join(p for p in parts if p)


def resolve_steps(s: JobSettings) -> int:
    steps = ENGINES[s.engine]["steps"]
    return steps.get(s.quality, steps["balanced"])


def change_tempo(audio, sample_rate: int, rate: float):
    """Speed up (rate>1) or slow down keeping pitch. Unchanged on any failure."""
    if abs(rate - 1.0) < 0.02:
        return audio
    try:
        import torch
        import torchaudio.functional as F
        x = audio if audio.ndim == 2 else audio.unsqueeze(0)
        n_fft, hop = 1024, 256
        window = torch.hann_window(n_fft)
        spec = torch.stft(x, n_fft, hop, window=window, return_complex=True)
        advance = torch.linspace(0, 3.14159265 * hop, spec.shape[-2])[..., None]
        out = F.phase_vocoder(spec, rate, advance)
        return torch.istft(out, n_fft, hop, window=window)
    except Exception:  # noqa: BLE001
        logger.warning("nova: tempo change unavailable, audio left unchanged", exc_info=True)
        return audio


def shift_pitch(audio, sample_rate: int, semitones: float):
    """Shift pitch keeping duration. Returns the input unchanged on any failure."""
    if abs(semitones) < 0.05:
        return audio
    try:
        import torchaudio.functional as F
        x = audio if audio.ndim == 2 else audio.unsqueeze(0)
        return F.pitch_shift(x, sample_rate, semitones)
    except Exception:  # noqa: BLE001 — DSP is optional; never lose the audio
        logger.warning("nova: pitch shift unavailable, audio left unchanged", exc_info=True)
        return audio


def _silence(sample_rate: int, ms: int, like):
    import torch
    n = int(sample_rate * ms / 1000)
    return torch.zeros(like.shape[:-1] + (n,), dtype=like.dtype)


# ── Final polish: one voice, steady pace, even pauses ─────────────────────
_SENT_END = ".!?؟۔…"
_CLAUSE_END = "،,;؛:"
_EDGE_MS = 30
_CLOSERS = "\"'»”’)]}"


def pause_ms_after(text: str, base_ms: int) -> int:
    """Pause that follows a chunk, by how its text ended."""
    t = (text or "").rstrip().rstrip(_CLOSERS).rstrip()
    last = t[-1:]
    if last and last in _SENT_END:
        return int(base_ms)
    if last and last in _CLAUSE_END:
        return int(base_ms * 0.45)
    return int(base_ms * 0.2)


def _letters(text: str) -> int:
    return sum(1 for c in (text or "") if c.isalnum())


def trim_np(x, sr: int, keep_ms: int = 60):
    """Strip the quiet lead-in and tail an engine leaves around one chunk."""
    import numpy as np
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak <= 0:
        return x
    loud = np.flatnonzero(np.abs(x) > peak * 0.01)
    if loud.size == 0:
        return x
    keep = int(sr * keep_ms / 1000)
    return x[max(0, int(loud[0]) - keep): min(x.size, int(loud[-1]) + 1 + keep)]


def compress_gaps(x, sr: int, max_gap_ms: int = 500, keep_ms: int = 320):
    """Shorten silences inside a chunk that run longer than ``max_gap_ms``."""
    import numpy as np
    hop = max(1, int(sr * 0.01))
    n = x.size // hop
    if n < 5:
        return x
    frames = np.abs(x[: n * hop]).reshape(n, hop).max(axis=1)
    thr = max(float(frames.max()) * 0.012, 1e-4)
    silent = frames < thr
    min_run = max_gap_ms // 10
    keep_f = keep_ms // 10
    mask = np.ones(x.size, dtype=bool)
    i = 0
    while i < n:
        if silent[i]:
            j = i
            while j < n and silent[j]:
                j += 1
            if j - i > min_run:
                mask[(i + keep_f // 2) * hop:(j - keep_f // 2) * hop] = False
            i = j
        else:
            i += 1
    return x[mask]


def _rms(x) -> float:
    import numpy as np
    if x.size == 0:
        return 0.0
    peak = float(np.max(np.abs(x)))
    v = x[np.abs(x) > peak * 0.05]
    return float(np.sqrt(np.mean(v.astype(np.float64) ** 2))) if v.size else 0.0


def _fade(x, n: int):
    import numpy as np
    n = min(n, x.size // 2)
    if n < 2:
        return x
    y = x.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def polish_clips(clips, sr: int, pause_ms: int, tempo_fn=None):
    """Join rendered chunks into one even take.

    ``clips`` is a list of ``(mono float32 array, chunk text)``. Each chunk is
    trimmed, its long inner silences shortened, its speaking pace pulled toward
    the median pace of the job, and its loudness matched; then chunks are joined
    with one pause per punctuation kind and a short fade at every seam.
    """
    import numpy as np
    prepared = []
    for x, t in clips:
        x = compress_gaps(trim_np(x, sr, _EDGE_MS), sr)
        if x.size:
            prepared.append([x, t])
    if not prepared:
        return np.zeros(1, dtype=np.float32)

    if tempo_fn is not None and len(prepared) >= 2:
        rates = [((len(x) / sr) / _letters(t)) if _letters(t) >= 15 else None for x, t in prepared]
        valid = sorted(r for r in rates if r)
        if len(valid) >= 2:
            target = valid[len(valid) // 2]
            for k, r in enumerate(rates):
                if not r:
                    continue
                f = r / target
                if abs(f - 1.0) > 0.06:
                    prepared[k][0] = np.asarray(tempo_fn(prepared[k][0], min(1.18, max(0.85, f))), dtype=np.float32)

    levels = [_rms(x) for x, _ in prepared]
    good = sorted(v for v in levels if v > 0)
    if len(good) >= 2:
        target = good[len(good) // 2]
        for k, v in enumerate(levels):
            if v > 0:
                prepared[k][0] = prepared[k][0] * float(min(1.7, max(0.6, target / v)))

    fade = int(sr * 0.006)
    pieces = []
    prev_text = ""
    for x, t in prepared:
        if pieces:
            # The kept quiet edges already count toward the pause.
            gap_ms = max(0, pause_ms_after(prev_text, pause_ms) - 2 * _EDGE_MS)
            pieces.append(np.zeros(int(sr * gap_ms / 1000), dtype=np.float32))
        pieces.append(_fade(x.astype(np.float32), fade))
        prev_text = t
    out = np.concatenate(pieces)
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.98:
        out = out * (0.98 / peak)
    return out


def shorten_first(chunks: list[str], limit: int = 110) -> list[str]:
    """Make the first chunk short: it becomes the voice reference for the rest."""
    if len(chunks) < 2 or len(chunks[0]) <= limit:
        return chunks
    sentences = [x.strip() for x in SENTENCE_END.split(chunks[0]) if x.strip()]
    head, used = "", 0
    for sent in sentences:
        if head and len(head) + 1 + len(sent) > limit:
            break
        head = f"{head} {sent}".strip()
        used += 1
    if used == 0 or len(head) > limit:
        parts = _split_long(chunks[0], limit)
        head, rest = parts[0], " ".join(parts[1:])
    else:
        rest = " ".join(sentences[used:])
    return [head] + ([rest] if rest else []) + chunks[1:]


def _to_np(audio):
    import numpy as np
    x = audio.detach().float().cpu().numpy()
    if x.ndim > 1:
        x = x.mean(axis=0)
    return np.asarray(x, dtype=np.float32)


def _write_wav(path: str, x, sr: int) -> None:
    import wave
    import numpy as np
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())



class _CancelledWhileWaiting(Exception):
    """Raised inside the runner when a cancel arrives during a blocking step."""


@dataclass
class NovaJob:
    id: str
    settings: JobSettings
    chunks: list[str]
    state: str = "queued"   # queued running pausing paused cancelling done cancelled error
    created: float = field(default_factory=time.monotonic)
    started: Optional[float] = None
    finished: Optional[float] = None
    paused_total: float = 0.0
    paused_at: Optional[float] = None
    chunk_started: Optional[float] = None
    done_chunks: int = 0
    chars_done: int = 0
    gen_seconds_done: float = 0.0
    error: Optional[str] = None
    result: Optional[dict] = None
    partial: bool = False
    _pause_requested: bool = False
    _cancel_requested: bool = False
    _resume: asyncio.Event = field(default_factory=asyncio.Event)
    _cancel_evt: asyncio.Event = field(default_factory=asyncio.Event)
    _parts: list = field(default_factory=list)
    _texts: list = field(default_factory=list)
    _anchor: Optional[tuple] = None        # (wav path, its text): the voice every chunk follows
    _anchor_off: bool = False
    _seed: int = field(default_factory=lambda: __import__('random').randint(1, 2**31 - 1))
    _anchor_dir: Optional[str] = None
    _task: Optional[asyncio.Task] = None
    _sr: int = 24000
    _cancellation: Any = None

    # ── derived numbers ────────────────────────────────────────────────
    @property
    def total_chars(self) -> int:
        return sum(len(c) for c in self.chunks) or 1

    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        end = self.finished if self.finished is not None else time.monotonic()
        paused = self.paused_total + ((time.monotonic() - self.paused_at) if self.paused_at else 0.0)
        return max(0.0, end - self.started - paused)

    def s_per_char(self) -> tuple[float, bool]:
        """(seconds per char, calibrating?)"""
        if self.chars_done > 0 and self.gen_seconds_done > 0:
            return self.gen_seconds_done / self.chars_done, False
        return ENGINES[self.settings.engine]["prior_s_per_char"], True

    def progress(self) -> dict:
        spc, calibrating = self.s_per_char()
        remaining_chars = max(0, self.total_chars - self.chars_done)
        live = self.state in ("running", "pausing", "cancelling")
        chunk_elapsed = (time.monotonic() - self.chunk_started) if (live and self.chunk_started) else 0.0
        in_chunk = 0.0
        if not calibrating and live and self.chunk_started and self.done_chunks < len(self.chunks):
            cur = self.chunks[self.done_chunks]
            predicted = max(0.5, len(cur) * spc)
            # Never show a chunk as more than 95% done before the engine returns.
            in_chunk = min(0.95, chunk_elapsed / predicted) * len(cur)
        percent = min(100.0, 100.0 * (self.chars_done + in_chunk) / self.total_chars)
        if self.state == "done":
            percent = 100.0
        # Until one chunk has really been timed there is nothing honest to
        # extrapolate from, so no estimate is given instead of a guessed one.
        eta = None if (calibrating or self.state == "paused") else max(0.0, (remaining_chars - in_chunk) * spc)
        if self.state in ("done", "cancelled", "error"):
            eta = 0.0
        elapsed = self.elapsed()
        # First chunk still pending after a while: the engine is loading (or
        # downloading its model for the first time), not slow at speaking.
        warming = calibrating and live and self.done_chunks == 0 and chunk_elapsed > 8
        return {
            "id": self.id,
            "state": self.state,
            "percent": round(percent, 1),
            "elapsed_s": round(elapsed, 1),
            "eta_s": None if eta is None else round(eta, 1),
            "total_estimate_s": None if eta is None else round(elapsed + eta, 1),
            "calibrating": calibrating and self.state not in ("done", "cancelled", "error"),
            "warming": warming,
            "chunk_elapsed_s": round(chunk_elapsed, 1),
            "chunk": min(self.done_chunks + 1, len(self.chunks)),
            "chunks": len(self.chunks),
            "chars_done": self.chars_done,
            "chars_total": self.total_chars,
            "engine": self.settings.engine,
            "error": self.error,
            "result": self.result,
            "partial": self.partial,
        }

    # ── controls ───────────────────────────────────────────────────────
    def pause(self) -> None:
        if self.state == "running":
            self._pause_requested = True
            self._resume.clear()
            self.state = "pausing"

    def resume(self) -> None:
        if self.state in ("pausing", "paused"):
            self._pause_requested = False
            if self.paused_at:
                self.paused_total += time.monotonic() - self.paused_at
                self.paused_at = None
            self.state = "running"
            self._resume.set()

    def cancel(self) -> None:
        if self.state in ("running", "pausing", "paused", "queued"):
            self._cancel_requested = True
            self.state = "cancelling"
            if self.paused_at:
                self.paused_total += time.monotonic() - self.paused_at
                self.paused_at = None
            self._resume.set()   # wake a paused loop so it can wind down
            self._cancel_evt.set()   # wake a loop waiting on model load / a chunk
            if self._cancellation is not None:
                # Engines that run as killable sidecars stop at once instead of
                # finishing the chunk in flight.
                self._cancellation.cancel()

    async def _until_cancelled(self, fn, *args):
        """Run ``fn`` in a thread, but return control the moment the user cancels,
        even while a model is still downloading or loading (a thread cannot be
        interrupted, so it is abandoned and finishes on its own)."""
        work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        waiter = asyncio.ensure_future(self._cancel_evt.wait())
        try:
            await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if work.done():
            return work.result()
        work.add_done_callback(lambda f: f.cancelled() or f.exception())
        raise _CancelledWhileWaiting()

    # ── runner ─────────────────────────────────────────────────────────
    async def run(self, *, get_backend: Callable[[str], Any], finalize: Callable[..., Any]) -> None:
        s = self.settings
        eng = ENGINES[s.engine]
        self.started = time.monotonic()
        self.state = "running"
        self._resume.set()
        from services.inference_cancellation import InferenceCancellation
        if self._cancellation is None:
            self._cancellation = InferenceCancellation()
        aborted = False
        try:
            try:
                backend = await self._until_cancelled(get_backend, eng["engine_id"])
            except _CancelledWhileWaiting:
                self.state = "cancelled"
                return
            sr = int(backend.sample_rate)
            self._sr = sr
            semis = effective_semitones(s)
            for i, chunk in enumerate(self.chunks):
                if self._cancel_requested:
                    break
                if self._pause_requested:
                    self.state = "paused"
                    self.paused_at = time.monotonic()
                    await self._resume.wait()
                    if self._cancel_requested:
                        break
                self.chunk_started = time.monotonic()
                try:
                    audio = await self._until_cancelled(self._render_chunk, backend, chunk, sr, semis)
                except (Exception, _CancelledWhileWaiting):
                    if self._cancel_requested:
                        # The engine was stopped on purpose mid-chunk: keep what
                        # earlier chunks produced and drop the unfinished one.
                        aborted = True
                        self.chunk_started = None
                        break
                    raise
                spent = time.monotonic() - self.chunk_started
                self.chunk_started = None
                self.gen_seconds_done += spent
                self.chars_done += len(chunk)
                self.done_chunks = i + 1
                if audio is not None and audio.shape[-1] > 0:
                    self._parts.append(audio)
                    self._texts.append(chunk)
                    if self._anchor is None and not self._anchor_off:
                        self._make_anchor(audio, chunk, sr)
            cancelled = self._cancel_requested and (aborted or self.done_chunks < len(self.chunks))
            if not self._parts:
                self.state = "cancelled" if cancelled else "error"
                self.error = None if cancelled else "لم يُنتج المحرك أي صوت."
                return
            self.partial = cancelled
            audio_all, sr_out = await asyncio.to_thread(self._join, sr)
            self.result = await finalize(audio_all, sr_out, self)
            self.state = "cancelled" if cancelled else "done"
        except Exception as exc:  # noqa: BLE001
            logger.exception("nova job %s failed", self.id)
            if self._parts:
                # Keep what was rendered rather than losing it.
                try:
                    audio_all, sr_out = await asyncio.to_thread(self._join, self._sr)
                    self.partial = True
                    self.result = await finalize(audio_all, sr_out, self)
                except Exception:  # noqa: BLE001
                    logger.exception("nova job %s: could not save partial audio", self.id)
            self.state = "error"
            self.error = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            self.finished = time.monotonic()
            self.chunk_started = None
            if self._anchor_dir:
                import shutil
                shutil.rmtree(self._anchor_dir, ignore_errors=True)

    def _render_chunk(self, backend, chunk: str, sr: int, semitones: float):
        s = self.settings
        eng = ENGINES[s.engine]
        speed = effective_speed(s)
        kw: dict[str, Any] = {"language": s.language, "num_step": resolve_steps(s)}
        if eng["native_speed"]:
            kw["speed"] = speed
        anchored = self._anchor is not None and s.engine in ("quality", "expressive")
        if s.engine == "fast":
            kw["voice"] = resolve_voice(s)
        elif anchored:
            # Same voice for every chunk: continue from the first chunk's audio.
            kw["ref_audio"], kw["ref_text"] = self._anchor
        elif s.engine == "quality":
            kw["instruct"] = resolve_instruct(s)
        else:
            kw["description"] = resolve_description(s)
        if s.engine == "quality":
            kw["seed"] = self._seed
        try:
            with self._cancellation.activate():
                audio = backend.generate(chunk, **kw)
        except Exception:
            if not anchored or self._cancel_requested:
                raise
            # The reference route failed on this engine: fall back to the plain
            # voice description for the rest of the job rather than failing it.
            logger.warning("nova job %s: voice anchoring failed, continuing without it", self.id, exc_info=True)
            self._anchor, self._anchor_off = None, True
            kw.pop("ref_audio", None)
            kw.pop("ref_text", None)
            if s.engine == "quality":
                kw["instruct"] = resolve_instruct(s)
            else:
                kw["description"] = resolve_description(s)
            with self._cancellation.activate():
                audio = backend.generate(chunk, **kw)
        if audio is None:
            return None
        if audio.ndim == 1:
            audio = audio.unsqueeze(0)
        if not eng["native_speed"]:
            audio = change_tempo(audio, sr, speed)
        return shift_pitch(audio, sr, semitones)

    def _make_anchor(self, audio, text: str, sr: int) -> None:
        """Keep the first chunk as the voice reference for the engines that can follow one."""
        s = self.settings
        if s.engine not in ("quality", "expressive") or len(self.chunks) < 2:
            self._anchor_off = True
            return
        try:
            import tempfile
            x = trim_np(_to_np(audio), sr)
            if x.size < sr or x.size > 15 * sr:      # too short to carry a voice, or too long to be cheap
                self._anchor_off = True
                return
            self._anchor_dir = tempfile.mkdtemp(prefix="nova_anchor_")
            path = f"{self._anchor_dir}/voice.wav"
            _write_wav(path, x, sr)
            self._anchor = (path, text)
        except Exception:  # noqa: BLE001
            logger.warning("nova job %s: could not keep a voice reference", self.id, exc_info=True)
            self._anchor_off = True

    def _join(self, sr: int):
        import numpy as np
        import torch
        pause_ms = STYLES.get(self.settings.style, STYLES["natural"])["pause_ms"]
        clips = [(_to_np(a), t) for a, t in zip(self._parts, self._texts)]

        def tempo(x, factor):
            y = change_tempo(torch.from_numpy(np.ascontiguousarray(x)).unsqueeze(0), sr, factor)
            return y.detach().cpu().numpy().reshape(-1)

        out = polish_clips(clips, sr, pause_ms, tempo)
        return torch.from_numpy(out).unsqueeze(0), sr


class NovaJobManager:
    """One render at a time: CPU-only machines gain nothing from parallel jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, NovaJob] = {}
        self._order: list[str] = []

    def active(self) -> Optional[NovaJob]:
        for j in self._jobs.values():
            if j.state in ("queued", "running", "pausing", "paused", "cancelling"):
                return j
        return None

    def get(self, job_id: str) -> Optional[NovaJob]:
        return self._jobs.get(job_id)

    def create(self, settings: JobSettings, chunks: list[str]) -> NovaJob:
        if self.active() is not None:
            raise RuntimeError("busy")
        job = NovaJob(id=uuid.uuid4().hex[:10], settings=settings, chunks=chunks)
        self._jobs[job.id] = job
        self._order.append(job.id)
        while len(self._order) > 20:
            self._jobs.pop(self._order.pop(0), None)
        return job


MANAGER = NovaJobManager()
