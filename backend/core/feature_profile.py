"""NOVA Voice feature profile.

NOVA Voice is VoiceStudio narrowed to text-to-speech. Instead of deleting the
other workspaces outright, the backend decides at startup which routers to
register. The default profile is ``tts``; set ``NOVA_FEATURE_PROFILE=full`` to
get the complete upstream surface back (useful while pruning, and for tests).

The engines, model manager and services are untouched: only route
registration and the optional MCP mount depend on the profile.
"""

from __future__ import annotations

import os

PROFILE_ENV = "NOVA_FEATURE_PROFILE"
DEFAULT_PROFILE = "tts"

# Router modules (last segment of ``api.routers.<name>``) kept in the TTS profile.
TTS_ROUTERS: frozenset[str] = frozenset({
    "system",          # health, version, hardware info
    "profiles",        # saved voices (clone references)
    "profile_images",  # voice avatars
    "exports",         # saving generated audio
    "generation",      # POST /generate + history
    "engines",         # engine list / selection
    "setup",           # first-run runtime + model installation
    "settings",        # user settings, HF token
    "events",          # progress events used by the UI
    "tts_stream",      # streaming synthesis
    "openai_compat",   # /audio/speech local API
    "describe_voice",  # voice design from a text description
    "archetypes",      # voice design presets
    "pronunciation",   # pronunciation dictionary
    "auth",            # local session handling the UI expects
    # Needed by the first-run wizard and shared UI widgets; without them the
    # setup screens and the performance panel call endpoints that return 404.
    "model_licenses",  # model install prepare/commit + licence acceptance
    "media_tools",     # ffmpeg status/installation (audio conversion)
    "batch",           # /batch/jobs polled by the status bar
    "dictation",       # /dictation/* read by the setup wizard and settings
    "projects",        # /projects read by the voice panels
    "nova",            # NOVA Voice jobs: progress, pause/resume/cancel, device status
})


def active_profile() -> str:
    value = os.environ.get(PROFILE_ENV, DEFAULT_PROFILE).strip().lower()
    return value if value in {"tts", "full"} else DEFAULT_PROFILE


def router_enabled(module_name: str) -> bool:
    """True when the router module should be registered in this profile."""
    if active_profile() == "full":
        return True
    return module_name.rsplit(".", 1)[-1] in TTS_ROUTERS


def mcp_enabled() -> bool:
    """The MCP server is part of the full upstream surface only."""
    return active_profile() == "full"
