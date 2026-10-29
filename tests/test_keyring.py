import asyncio
from datetime import datetime

import pytest

from notes_bot.gemini import AUTH, MODEL, OTHER, OVERLOADED, QUOTA, GeminiError
from notes_bot import keyring
from notes_bot.keyring import QUOTA_ZONE, KeyRing, RotatingModel, seconds_to_quota_reset


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScriptedClient:
    """Fails with the errors queued for its key, then answers with the key name."""

    def __init__(self, key, script, calls):
        self.key = key
        self.script = script
        self.calls = calls

    async def check_key(self):
        self.calls.append(self.key)
        errors = self.script.get(self.key, [])
        if errors:
            raise errors.pop(0)
        return self.key

    async def transcribe(self, audio, filename):
        return await self.check_key()

    async def write(self, transcript, enabled, length, language="auto"):
        return await self.check_key()


def _model(keys, script, ring):
    calls = []
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    model = RotatingModel(keys, lambda key: ScriptedClient(key, script, calls), ring, sleep=sleep)
    return model, calls, slept


def test_quota_moves_to_the_next_key_and_rests_the_first():
    ring = KeyRing(Clock())
    model, calls, _ = _model(["a", "b"], {"a": [GeminiError("limit", QUOTA, retry_after=30)]}, ring)
    assert asyncio.run(model.transcribe(b"", "x.ogg")) == "b"
    assert calls == ["a", "b"]
    assert ring.resting("a") == 60  # at least a minute
    assert ring.resting("b") == 0


def test_resting_keys_go_last_and_come_back():
    clock = Clock()
    ring = KeyRing(clock)
    ring.rest("a", 60)
    assert ring.order(["a", "b"]) == ["b", "a"]
    clock.now += 61
    assert ring.resting("a") == 0


def test_least_recently_used_key_goes_first():
    clock = Clock()
    ring = KeyRing(clock)
    ring.used("a")
    clock.now += 1
    ring.used("b")
    assert ring.order(["a", "b", "c"]) == ["c", "a", "b"]


def test_daily_quota_rests_until_pacific_midnight(monkeypatch):
    monkeypatch.setattr(keyring, "seconds_to_quota_reset", lambda: 7200.0)
    ring = KeyRing(Clock())
    model, _, _ = _model(["a", "b"], {"a": [GeminiError("limit", QUOTA, daily=True)]}, ring)
    asyncio.run(model.write("t", ("takeaways",), "medium"))
    assert ring.resting("a") == 7200


def test_rejected_key_is_skipped():
    ring = KeyRing(Clock())
    model, calls, _ = _model(["bad", "good"], {"bad": [GeminiError("no", AUTH)]}, ring)
    assert asyncio.run(model.transcribe(b"", "x.ogg")) == "good"
    assert ring.resting("bad") == 3600


def test_all_keys_out_of_quota_gives_one_clear_message():
    ring = KeyRing(Clock())
    script = {"a": [GeminiError("limit", QUOTA)], "b": [GeminiError("limit", QUOTA)]}
    model, _, _ = _model(["a", "b"], script, ring)
    with pytest.raises(GeminiError, match="Все ключи \\(2\\)") as caught:
        asyncio.run(model.check_key())
    assert caught.value.kind == QUOTA


def test_overload_is_retried_on_the_same_key():
    ring = KeyRing(Clock())
    script = {"a": [GeminiError("busy", OVERLOADED), GeminiError("busy", OVERLOADED)]}
    model, calls, slept = _model(["a", "b"], script, ring)
    assert asyncio.run(model.transcribe(b"", "x.ogg")) == "a"
    assert calls == ["a", "a", "a"]
    assert slept == [4.0, 8.0]


def test_unexpected_errors_are_not_retried_on_other_keys():
    ring = KeyRing(Clock())
    model, calls, _ = _model(["a", "b"], {"a": [GeminiError("broken", OTHER)]}, ring)
    with pytest.raises(GeminiError, match="broken"):
        asyncio.run(model.transcribe(b"", "x.ogg"))
    assert calls == ["a"]


def test_overloaded_or_unavailable_key_is_skipped_for_the_next_one():
    ring = KeyRing(Clock())
    script = {"a": [GeminiError("busy", OVERLOADED)] * 3, "b": []}
    model, calls, _ = _model(["a", "b"], script, ring)
    assert asyncio.run(model.transcribe(b"", "x.ogg")) == "b"
    assert ring.resting("a") == 300
    assert calls[-1] == "b"


def test_model_missing_from_a_package_moves_on_to_another_key():
    ring = KeyRing(Clock())
    model, calls, _ = _model(["a", "b"], {"a": [GeminiError("no model", MODEL)]}, ring)
    assert asyncio.run(model.transcribe(b"", "x.ogg")) == "b"
    assert ring.resting("a") == 3600


def test_quota_reset_is_counted_to_the_next_midnight():
    evening = datetime(2026, 10, 27, 23, 0, tzinfo=QUOTA_ZONE)
    assert seconds_to_quota_reset(evening) == 3600


def test_unreachable_supplier_moves_on_to_a_google_key():
    from notes_bot.gemini import UNREACHABLE

    ring = KeyRing(Clock())
    model, calls, _ = _model(["gateway", "google"], {"gateway": [GeminiError("timeout", UNREACHABLE)]}, ring)
    assert asyncio.run(model.transcribe(b"", "x.ogg")) == "google"
    assert calls == ["gateway", "google"]
    assert ring.resting("gateway") == 300
