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
    assert job.progress()["calibrating"] is True
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
