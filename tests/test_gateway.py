import asyncio
import json
import shutil
import subprocess

import httpx
import pytest

from notes_bot.gateway import GatewayClient
from notes_bot.gemini import AUTH, MODEL, OVERLOADED, QUOTA, GeminiError
from notes_bot.keys import KEY_PATTERN, is_gateway_key
from notes_bot.services import GATEWAY_ADDED, GATEWAY_WARNING, key_report
from notes_bot.ui import Ui

GATEWAY_KEY = "sk-cvc-" + "a" * 64  # same shape as a real key, not one


def _client(handler, model="gemini-3.5-flash"):
    return GatewayClient(GATEWAY_KEY, model, transport=httpx.MockTransport(handler))


def _reply(content, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]})


def _seen(requests):
    return [json.loads(request.content) for request in requests]


def test_gateway_keys_are_recognised_and_only_they():
    assert is_gateway_key(GATEWAY_KEY)
    assert not is_gateway_key("AIzaSyTestKeyValue1234567890abcdefghij")
    assert KEY_PATTERN.findall(f"ключ {GATEWAY_KEY} спасибо") == [GATEWAY_KEY]


def test_check_key_sends_the_bearer_token_and_the_model():
    seen = []

    def handler(request):
        seen.append(request)
        return _reply("ok")

    asyncio.run(_client(handler).check_key())
    assert seen[0].url == "https://api.aikeysforyou.com/v1/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {GATEWAY_KEY}"
    assert _seen(seen)[0]["model"] == "gemini-3.5-flash"


def test_write_parses_the_note_even_inside_a_code_fence():
    note = {"title": "Реклама", "takeaways": ["Главный вывод"], "questions": []}
    requests = []

    def handler(request):
        requests.append(request)
        return _reply("```json\n" + json.dumps(note, ensure_ascii=False) + "\n```")

    document = asyncio.run(_client(handler).write("Текст лекции. " * 20, ["takeaways"], "medium"))
    assert document.title == "Реклама"
    body = _seen(requests)[0]
    assert body["messages"][0]["role"] == "system" and "takeaways" in body["messages"][0]["content"]


def test_truncated_note_and_bad_json_are_reported():
    with pytest.raises(GeminiError, match="не уместился"):
        asyncio.run(_client(lambda request: _reply("{", finish="length")).write("т " * 50, ["takeaways"], "medium"))
    with pytest.raises(GeminiError, match="неверном формате"):
        asyncio.run(_client(lambda request: _reply("не JSON")).write("т " * 50, ["takeaways"], "medium"))


@pytest.mark.parametrize(
    ("status", "headers", "body", "kind"),
    [
        (401, {}, {"error": "bad key"}, AUTH),
        (402, {}, {"error": "balance"}, AUTH),
        (429, {"retry-after": "30"}, {"error": "slow down"}, QUOTA),
        (403, {}, {"error": "model_not_allowed"}, MODEL),
        (404, {}, {"error": "no such model"}, MODEL),
        (503, {}, {"error": {"code": "model_temporarily_unavailable"}}, OVERLOADED),
        (500, {}, {"error": "boom"}, OVERLOADED),
    ],
)
def test_gateway_errors_map_to_the_same_kinds_as_google(status, headers, body, kind):
    with pytest.raises(GeminiError) as caught:
        asyncio.run(_client(lambda request: httpx.Response(status, json=body, headers=headers)).check_key())
    assert caught.value.kind == kind


def test_quota_error_keeps_the_retry_hint():
    with pytest.raises(GeminiError) as caught:
        asyncio.run(_client(lambda request: httpx.Response(429, headers={"retry-after": "30"})).check_key())
    assert caught.value.retry_after == 30


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_transcribe_sends_audio_as_mp3_input():
    ogg = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "libopus", "-f", "ogg", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    requests = []

    def handler(request):
        requests.append(request)
        return _reply("Расшифровка лекции")

    assert asyncio.run(_client(handler).transcribe(ogg, "voice.ogg")) == "Расшифровка лекции"
    content = _seen(requests)[0]["messages"][0]["content"]
    audio = next(part["input_audio"] for part in content if part["type"] == "input_audio")
    assert audio["format"] == "mp3"


def test_key_report_warns_that_a_third_party_sees_the_data():
    text = key_report(Ui(premium=False), [("sk-c…ce2c", GATEWAY_ADDED)])
    assert "добавлен" in text and GATEWAY_WARNING in text


def test_supplier_reason_is_shown_without_the_key():
    body = {"error": f"invalid key {GATEWAY_KEY} for this package"}
    with pytest.raises(GeminiError) as caught:
        asyncio.run(_client(lambda request: httpx.Response(401, json=body)).check_key())
    assert caught.value.kind == AUTH
    assert "Ответ поставщика (401)" in str(caught.value)
    assert GATEWAY_KEY not in str(caught.value) and "sk-cvc-…" in str(caught.value)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_long_recording_is_cut_and_the_pieces_are_joined_in_order(monkeypatch):
    import notes_bot.gateway as gateway

    monkeypatch.setattr(gateway, "SEGMENT_SECONDS", 1)
    wav = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    requests = []

    def handler(request):
        requests.append(request)
        return _reply(f"кусок {len(requests)}")

    text = asyncio.run(_client(handler).transcribe(wav, "lecture.wav"))
    assert len(requests) >= 3
    expected = "\n\n".join(f"кусок {number}" for number in range(1, len(requests) + 1))
    assert text == expected
