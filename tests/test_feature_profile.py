"""NOVA Voice feature profile: TTS-only by default, full surface on request."""

import importlib
import os
import sys

BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")


def _load(monkeypatch, profile=None):
    monkeypatch.syspath_prepend(os.path.abspath(BACKEND))
    if profile is None:
        monkeypatch.delenv("NOVA_FEATURE_PROFILE", raising=False)
    else:
        monkeypatch.setenv("NOVA_FEATURE_PROFILE", profile)
    sys.modules.pop("core.feature_profile", None)
    return importlib.import_module("core.feature_profile")


def test_default_is_tts_only(monkeypatch):
    fp = _load(monkeypatch)
    assert fp.active_profile() == "tts"
    assert fp.router_enabled("api.routers.generation")
    assert fp.router_enabled("api.routers.setup")
    assert not fp.router_enabled("api.routers.dub_core")
    assert not fp.router_enabled("api.routers.telephony_twilio")
    assert not fp.router_enabled("api.routers.dictation")
    assert not fp.mcp_enabled()


def test_full_profile_restores_everything(monkeypatch):
    fp = _load(monkeypatch, "full")
    assert fp.router_enabled("api.routers.dub_core")
    assert fp.mcp_enabled()


def test_unknown_profile_falls_back_to_tts(monkeypatch):
    fp = _load(monkeypatch, "bogus")
    assert fp.active_profile() == "tts"
