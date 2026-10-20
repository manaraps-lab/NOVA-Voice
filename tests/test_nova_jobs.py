"""NOVA Voice jobs: chunking, progress, pause/resume and cancel-with-partial-result."""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from services import nova_jobs as nj  # noqa: E402


class Fake:
    """Minimal stand-in for a (1, n) tensor."""
    ndim = 2

    def __init__(self, n):
        self.n = n
        self.shape = (1, n)
        self.dtype = "f32"

    def unsqueeze(self, _):
        return self

    def to(self, _):
        return self


class FakeBackend:
    sample_rate = 1000

    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []
        self.gate = None

    def generate(self, text, **kw):
        self.calls.append((text, kw))
        if self.gate is not None:
            self.gate(len(self.calls))
        time.sleep(self.delay)
        return Fake(len(text))


def _patch(monkeypatch=None):
    nj.shift_pitch = lambda a, sr, st: a
    nj._silence = lambda sr, ms, like: Fake(int(sr * ms / 1000))
    nj.NovaJob._join = lambda self, sr: (Fake(sum(p.n for p in self._parts)), sr)


def _job(text, engine="fast", **kw):
    s = nj.JobSettings(text=text, engine=engine, **kw)
    chunks = nj.split_for_job(text, nj.ENGINES[engine]["chunk_chars"])
    return nj.NovaJob(id="t", settings=s, chunks=chunks)


async def _finalize(audio, sr, job):
    return {"id": "x", "filename": "x.wav", "duration": audio.n / sr}


def test_split_respects_limit_and_keeps_all_text():
    text = ("جملة قصيرة. " * 80) + ("كلمة " * 120)
    chunks = nj.split_for_job(text, 160)
    assert chunks and all(len(c) <= 160 for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_instruct_uses_only_valid_tags_and_depth_maps_to_pitch():
    s = nj.JobSettings(text="x", gender="male", age="elderly", depth=-2, style="whisper")
    assert nj.resolve_instruct(s) == "male, elderly, very low pitch, whisper"


def test_full_run_reports_done_and_full_progress():
    _patch()
    job = _job("مرحبا بكم. " * 60)
    backend = FakeBackend(delay=0.01)
    asyncio.run(job.run(get_backend=lambda _id: backend, finalize=_finalize))
    p = job.progress()
    assert p["state"] == "done" and p["percent"] == 100.0 and not p["partial"]
    assert backend.calls[0][1]["voice"] in nj.FAST_VOICES["female"]


def test_pause_then_resume_continues_and_excludes_paused_time():
    _patch()
    job = _job("جملة طويلة نسبيا للاختبار. " * 40)
    assert len(job.chunks) >= 3
    backend = FakeBackend(delay=0.05)

    async def scenario():
        task = asyncio.create_task(job.run(get_backend=lambda _id: backend, finalize=_finalize))
        await asyncio.sleep(0.07)
        job.pause()
        for _ in range(100):
            if job.state == "paused":
                break
            await asyncio.sleep(0.02)
        assert job.state == "paused"
        done_at_pause = job.done_chunks
        await asyncio.sleep(0.4)
        assert job.done_chunks == done_at_pause          # nothing renders while paused
        elapsed_a = job.elapsed()
        await asyncio.sleep(0.2)
        assert abs(job.elapsed() - elapsed_a) < 0.05     # the clock stops too
        job.resume()
        await task

    asyncio.run(scenario())
    assert job.state == "done" and job.done_chunks == len(job.chunks)


def test_cancel_returns_partial_audio():
    _patch()
    job = _job("جملة طويلة نسبيا للاختبار. " * 40)
    backend = FakeBackend(delay=0.05)

    async def scenario():
        task = asyncio.create_task(job.run(get_backend=lambda _id: backend, finalize=_finalize))
        await asyncio.sleep(0.12)
        job.cancel()
        await task

    asyncio.run(scenario())
    p = job.progress()
    assert p["state"] == "cancelled" and p["partial"] is True
    assert p["result"] and p["result"]["duration"] > 0
    assert 0 < job.done_chunks < len(job.chunks)


def test_cancel_while_paused_still_saves_what_was_rendered():
    _patch()
    job = _job("جملة طويلة نسبيا للاختبار. " * 40)
    backend = FakeBackend(delay=0.05)

    async def scenario():
        task = asyncio.create_task(job.run(get_backend=lambda _id: backend, finalize=_finalize))
        await asyncio.sleep(0.07)
        job.pause()
        for _ in range(100):
            if job.state == "paused":
                break
            await asyncio.sleep(0.02)
        job.cancel()
        await task

    asyncio.run(scenario())
    assert job.state == "cancelled" and job.result is not None


def test_eta_is_marked_calibrating_until_first_chunk_then_measured():
    job = _job("جملة. " * 100)
    first = job.progress()
    assert first["calibrating"] is True and first["eta_s"] is None   # nothing honest to extrapolate yet
    job.chars_done, job.gen_seconds_done, job.done_chunks = 100, 5.0, 1
    p = job.progress()
    assert p["calibrating"] is False
    assert abs(p["eta_s"] - (job.total_chars - 100) * 0.05) < 0.2


def test_only_one_active_job():
    m = nj.NovaJobManager()
    s = nj.JobSettings(text="x")
    j = m.create(s, ["x"])
    j.state = "running"
    try:
        m.create(s, ["y"])
        assert False, "second job must be refused"
    except RuntimeError:
        pass


def test_quality_levels_map_to_each_engines_own_steps():
    for key, meta in nj.ENGINES.items():
        assert set(meta["steps"]) == set(nj.QUALITY_LEVELS)
        s = nj.JobSettings(text="x", engine=key, quality="max")
        assert nj.resolve_steps(s) == meta["steps"]["max"]
    assert nj.resolve_steps(nj.JobSettings(text="x", engine="fast", quality="max")) <= 12   # Supertonic's ceiling


def test_expressive_description_carries_style_gender_age_and_depth():
    s = nj.JobSettings(text="x", engine="expressive", style="enthusiastic", gender="male", age="elderly", depth=-1)
    d = nj.resolve_description(s)
    assert d.startswith("an elderly male voice") and "enthusiastic" in d and "deep" in d
    assert nj.effective_semitones(s) == 0.0   # style lives in the description, not in DSP


def test_cancel_interrupts_a_long_running_chunk_immediately():
    """A sidecar engine is killed on cancel, so a slow chunk must not delay the stop."""
    from services.inference_cancellation import current_cancellation
    _patch()
    job = _job("جملة طويلة نسبيا للاختبار. " * 40)

    class SlowBackend(FakeBackend):
        def generate(self, text, **kw):
            self.calls.append((text, kw))
            if len(self.calls) == 1:
                return Fake(len(text))            # first chunk finishes normally
            scope = current_cancellation()
            for _ in range(600):                  # up to 30 s unless cancelled
                if scope and scope.cancelled.is_set():
                    raise RuntimeError("sidecar killed")
                time.sleep(0.05)
            return Fake(len(text))

    backend = SlowBackend(delay=0.0)

    async def scenario():
        task = asyncio.create_task(job.run(get_backend=lambda _id: backend, finalize=_finalize))
        await asyncio.sleep(0.4)                  # first chunk done, second one stuck
        t0 = time.monotonic()
        job.cancel()
        await task
        return time.monotonic() - t0

    waited = asyncio.run(scenario())
    assert waited < 2.0
    p = job.progress()
    assert p["state"] == "cancelled" and p["partial"] is True and p["result"]
    assert job.done_chunks == 1                   # the interrupted chunk is not counted


def test_cancel_during_model_loading_returns_at_once():
    """First use downloads/loads a multi-GB model; cancel must not wait for it."""
    _patch()
    job = _job("جملة للاختبار. " * 20)

    def slow_load(_id):
        time.sleep(5)                             # a thread cannot be interrupted
        return FakeBackend()

    async def scenario():
        task = asyncio.create_task(job.run(get_backend=slow_load, finalize=_finalize))
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        job.cancel()
        await task
        return time.monotonic() - t0

    waited = asyncio.run(scenario())
    assert waited < 1.0
    assert job.progress()["state"] == "cancelled"


def test_cancel_wakes_a_paused_job():
    _patch()
    job = _job("جملة للاختبار. " * 40)
    backend = FakeBackend(delay=0.4)

    async def scenario():
        task = asyncio.create_task(job.run(get_backend=lambda _id: backend, finalize=_finalize))
        await asyncio.sleep(0.2)
        job.pause()
        await asyncio.sleep(0.5)
        job.cancel()
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())
    assert job.progress()["state"] == "cancelled"
