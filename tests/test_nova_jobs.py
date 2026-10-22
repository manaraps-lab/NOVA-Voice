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
        if not meta["steps"]:
            assert nj.resolve_steps(nj.JobSettings(text="x", engine=key)) is None   # no step control (compact engine)
            continue
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


def _sine(sr, sec, amp=0.3, f=220.0):
    import numpy as np
    t = np.arange(int(sr * sec)) / sr
    return (amp * np.sin(2 * np.pi * f * t)).astype("float32")


def test_pause_depends_on_how_the_chunk_ended():
    assert nj.pause_ms_after("انتهت الجملة.", 300) == 300
    assert nj.pause_ms_after("هل انتهت؟»", 300) == 300
    assert nj.pause_ms_after("أولا، ", 300) == 135
    assert nj.pause_ms_after("بلا علامة", 300) == 60


def test_first_chunk_is_shortened_so_it_can_serve_as_the_voice_reference():
    first = "جملة أولى قصيرة. " + "جملة ثانية طويلة نسبيا جدا. " * 6
    out = nj.shorten_first([first.strip(), "الثالثة."])
    assert len(out[0]) <= 110 and len(out) == 3
    assert "".join(out).replace(" ", "") == (first.strip() + "الثالثة.").replace(" ", "")
    assert nj.shorten_first(["قصيرة."]) == ["قصيرة."]


def test_polish_evens_out_pace_loudness_and_pauses():
    import numpy as np
    sr = 16000
    text = "هذه جملة مناسبة لقياس السرعة. "
    # same text rendered at three different paces and levels, with ragged silence around it
    pad = np.zeros(int(sr * 0.9), dtype="float32")
    clips = [
        (np.concatenate([pad, _sine(sr, 2.0, 0.5), pad]), text),
        (np.concatenate([pad[:1000], _sine(sr, 3.0, 0.1), pad]), text),
        (np.concatenate([pad, _sine(sr, 2.0, 0.3), pad[:500]]), text),
    ]
    calls = []

    def tempo(x, f):
        calls.append(round(f, 2))
        return x[:: 1] if abs(f - 1) < 1e-6 else x[: int(len(x) / f)]

    out = nj.polish_clips(clips, sr, 250, tempo)
    assert out.dtype == np.float32 and float(np.max(np.abs(out))) <= 0.98 + 1e-6
    assert calls and all(0.85 <= c <= 1.18 for c in calls)        # the slow clip was sped up, within bounds
    # leading/trailing silence trimmed and exactly two sentence-length pauses were inserted
    silent = np.abs(out) < 1e-4
    runs, n = [], 0
    for v in silent:
        if v:
            n += 1
        elif n:
            runs.append(n)
            n = 0
    big = [r for r in runs if r > sr * 0.2]
    assert len(big) == 2 and all(abs(r - sr * 0.25) < sr * 0.05 for r in big)


def test_long_inner_silence_is_compressed():
    import numpy as np
    sr = 16000
    x = np.concatenate([_sine(sr, 1.0), np.zeros(int(sr * 2.0), dtype="float32"), _sine(sr, 1.0)])
    y = nj.compress_gaps(x, sr)
    assert len(y) < len(x) - int(sr * 1.4)
    assert len(y) > 2 * sr


def _patch_audio_io():
    import numpy as np
    saved = (nj._to_np, nj._write_wav, nj.trim_np)
    nj._to_np = lambda a: np.zeros(a.n, dtype="float32") + 0.1
    nj._write_wav = lambda path, x, sr: open(path, "wb").write(b"RIFF")
    nj.trim_np = lambda x, sr, keep_ms=60: x
    return saved


def _restore_audio_io(saved):
    nj._to_np, nj._write_wav, nj.trim_np = saved


def test_later_chunks_follow_the_first_chunks_voice():
    _patch()
    saved = _patch_audio_io()
    job = _job("جملة أولى للاختبار. " * 3 + "جملة أخرى مختلفة تماما عن الأولى. " * 12, engine="quality")
    job.chunks = nj.shorten_first(job.chunks)
    backend = FakeBackend(delay=0.0)
    backend.sample_rate = 100

    class Long(FakeBackend):
        sample_rate = 100

        def generate(self, text, **kw):
            self.calls.append((text, kw))
            return Fake(300)                      # 3 s at 100 Hz: long enough to serve as a reference

    backend = Long(delay=0.0)
    asyncio.run(job.run(get_backend=lambda _id: backend, finalize=_finalize))
    assert len(backend.calls) >= 2
    first, later = backend.calls[0][1], backend.calls[1][1]
    assert "instruct" in first and "ref_audio" not in first
    assert later.get("ref_audio") and later.get("ref_text") == job.chunks[0] and "instruct" not in later
    assert first["seed"] == later["seed"]
    _restore_audio_io(saved)


def test_anchor_failure_falls_back_to_plain_voice():
    _patch()
    saved = _patch_audio_io()
    job = _job("جملة أولى للاختبار. " * 3 + "جملة أخرى مختلفة تماما عن الأولى. " * 12, engine="quality")
    job.chunks = nj.shorten_first(job.chunks)

    class Picky(FakeBackend):
        sample_rate = 100

        def generate(self, text, **kw):
            self.calls.append((text, kw))
            if "ref_audio" in kw:
                raise RuntimeError("reference not supported")
            return Fake(300)

    backend = Picky(delay=0.0)
    asyncio.run(job.run(get_backend=lambda _id: backend, finalize=_finalize))
    assert job.progress()["state"] == "done"
    _restore_audio_io(saved)
    assert any("instruct" in kw for _t, kw in backend.calls[2:])      # later chunks use the plain voice again


def test_every_target_language_has_an_engine_that_supports_it():
    for lang in ("ar", "es", "en", "he"):
        assert lang in nj.LANGUAGES
        assert [k for k in nj.ENGINE_PREFERENCE[lang] if lang in nj.ENGINES[k]["languages"]] == nj.ENGINE_PREFERENCE[lang]
    assert "he" not in nj.ENGINES["fast"]["languages"]          # Supertonic-3 has no Hebrew


def test_arabic_dialect_selects_the_omnivoice_language_id_and_only_there():
    s = nj.JobSettings(text="x", engine="quality", language="ar", dialect="egyptian")
    assert nj.resolve_language_code(s) == "arz"
    assert nj.resolve_language_code(nj.JobSettings(text="x", engine="quality", language="ar")) == "ar"
    assert nj.resolve_language_code(nj.JobSettings(text="x", engine="fast", language="ar", dialect="egyptian")) == "ar"
    assert nj.valid_dialect("ar", "gulf") and not nj.valid_dialect("ar", "british")
    assert nj.valid_dialect("en", "british") and not nj.valid_dialect("es", "mexican") and nj.valid_dialect("he", "default")


def test_english_accent_reaches_both_instruct_and_description():
    s = nj.JobSettings(text="x", engine="quality", language="en", dialect="british")
    assert nj.resolve_instruct(s).endswith("british accent")
    d = nj.JobSettings(text="x", engine="expressive", language="en", dialect="british")
    assert "british accent" in nj.resolve_description(d)
    ar = nj.JobSettings(text="x", engine="expressive", language="ar", dialect="egyptian")
    assert "Egyptian Arabic" in nj.resolve_description(ar)


def test_compact_engine_sends_only_a_language_and_follows_the_first_chunk():
    import numpy as np
    _patch()
    saved = _patch_audio_io()
    job = _job("שלום לכולם. זהו משפט לבדיקה. " * 15, engine="compact", language="he")
    job.chunks = nj.shorten_first(job.chunks)

    class Long(FakeBackend):
        sample_rate = 100

        def generate(self, text, **kw):
            self.calls.append((text, kw))
            return Fake(300)

    backend = Long(delay=0.0)
    asyncio.run(job.run(get_backend=lambda _id: backend, finalize=_finalize))
    _restore_audio_io(saved)
    first, later = backend.calls[0][1], backend.calls[1][1]
    assert first == {"language": "he"}
    assert later.get("ref_audio") and "instruct" not in later and "description" not in later and "num_step" not in later
