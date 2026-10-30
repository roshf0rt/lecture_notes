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
    note = {"title": "Реклама", "main_idea": "Идея", "sections": [{"heading": "Р", "elements": [{"type": "bullets", "items": ["Главный вывод"]}]}]}
    requests = []

    def handler(request):
        requests.append(request)
        return _reply("```json\n" + json.dumps(note, ensure_ascii=False) + "\n```")

    document = asyncio.run(_client(handler).write("Текст лекции. " * 20, "medium"))
    assert document.title == "Реклама"
    body = _seen(requests)[0]
    assert body["messages"][0]["role"] == "system" and "sections" in body["messages"][0]["content"]


def test_truncated_note_and_bad_json_are_reported():
    with pytest.raises(GeminiError, match="не уместился"):
        asyncio.run(_client(lambda request: _reply("{", finish="length")).write("т " * 50, "medium"))
    with pytest.raises(GeminiError, match="неверном формате"):
        asyncio.run(_client(lambda request: _reply("не JSON")).write("т " * 50, "medium"))


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

    monkeypatch.setattr(gateway, "MAX_PIECE_SECONDS", 1)
    monkeypatch.setattr(gateway, "PIECE_GAP", 0)
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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_recording_under_the_cut_threshold_goes_as_one_request():
    wav = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    requests = []

    def handler(request):
        requests.append(request)
        return _reply("целиком")

    assert asyncio.run(_client(handler).transcribe(wav, "short.wav")) == "целиком"
    assert len(requests) == 1


def test_content_given_as_parts_is_read():
    body = {"choices": [{"message": {"content": [{"type": "text", "text": "часть ответа"}]}, "finish_reason": "stop"}]}
    text, _ = asyncio.run(_client(lambda request: httpx.Response(200, json=body))._post([], 16))
    assert text == "часть ответа"


def test_unreadable_reply_shows_what_the_supplier_sent_without_the_key():
    with pytest.raises(GeminiError) as caught:
        asyncio.run(_client(lambda request: httpx.Response(200, json={"oops": GATEWAY_KEY})).check_key())
    assert "не понимает" in str(caught.value) and "Ответ поставщика (200)" in str(caught.value)
    assert GATEWAY_KEY not in str(caught.value)


def test_a_streamed_reply_is_joined_into_one_text():
    stream = (
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":"Первая "},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{"content":"часть"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    text, truncated = asyncio.run(_client(handler)._post([], 16))
    assert text == "Первая часть" and truncated is False
    assert seen[0]["stream"] is False


def test_requests_ask_for_a_plain_reply():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _reply("ok")

    asyncio.run(_client(handler).check_key())
    assert seen[0]["stream"] is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_there_is_a_pause_between_the_pieces_but_not_before_the_first(monkeypatch):
    import notes_bot.gateway as gateway

    monkeypatch.setattr(gateway, "MAX_PIECE_SECONDS", 1)
    monkeypatch.setattr(gateway, "PIECE_GAP", 3.0)
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(gateway.asyncio, "sleep", fake_sleep)
    wav = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    asyncio.run(_client(lambda request: _reply("кусок")).transcribe(wav, "long.wav"))
    assert pauses and set(pauses) == {3.0}
    assert len(pauses) >= 2  # one before every piece after the first


def test_the_limiter_never_lets_more_than_the_allowed_requests_run_at_once():
    from notes_bot.gateway import RequestLimiter

    async def scenario():
        limiter = RequestLimiter(concurrent=2, per_minute=1000)
        running = peak = 0

        async def one():
            nonlocal running, peak
            async with limiter:
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1

        await asyncio.gather(*(one() for _ in range(6)))
        return peak

    assert asyncio.run(scenario()) == 2


def test_the_limiter_waits_when_the_minute_is_used_up():
    from notes_bot.gateway import RequestLimiter

    now = [0.0]
    waited = []

    async def sleep(seconds):
        waited.append(round(seconds))
        now[0] += seconds

    async def scenario():
        limiter = RequestLimiter(concurrent=5, per_minute=3, clock=lambda: now[0], sleep=sleep)
        for _ in range(4):
            async with limiter:
                pass

    asyncio.run(scenario())
    assert waited == [60]  # the fourth request waits for the first to leave the window


def test_clients_of_one_key_share_the_limiter_and_it_is_used():
    from notes_bot.gateway import RequestLimiter

    entered = []

    class Counting(RequestLimiter):
        async def __aenter__(self):
            entered.append(1)
            await super().__aenter__()

    client = GatewayClient(GATEWAY_KEY, "gemini-3.5-flash", transport=httpx.MockTransport(lambda request: _reply("ok")),
                           limiter=Counting())
    asyncio.run(client.check_key())
    assert entered == [1]


def test_note_with_nulls_is_accepted_and_bad_reply_is_asked_again():
    good = {"title": "Реклама", "main_idea": None, "sections": [{"heading": "Р", "icon": None, "elements": [{"type": "bullets", "items": ["Вывод", None]}]}]}
    document = asyncio.run(_client(lambda request: _reply(json.dumps(good, ensure_ascii=False))).write("т " * 50, "medium"))
    assert document.sections[0].elements[0].items == ["Вывод"] and document.main_idea == ""

    replies = iter(["[1, 2]", json.dumps({"title": "Реклама", "sections": []})])
    requests = []

    def handler(request):
        requests.append(request)
        return _reply(next(replies))

    document = asyncio.run(_client(handler).write("т " * 50, "medium"))
    assert document.title == "Реклама" and len(requests) == 2
    assert "Ответ не подошёл" in _seen(requests)[1]["messages"][-1]["content"]


def test_a_dropped_request_is_sent_again_and_only_three_drops_give_up(monkeypatch):
    import notes_bot.gateway as gateway

    monkeypatch.setattr(gateway, "RECONNECT_DELAY", 0)
    tries = []

    def flaky(request):
        tries.append(1)
        if len(tries) < 3:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return _reply("ok")

    assert asyncio.run(_client(flaky)._post([{"role": "user", "content": "x"}], 16))[0] == "ok"
    assert len(tries) == 3

    def dead(request):
        raise httpx.ReadTimeout("")

    with pytest.raises(GeminiError) as caught:
        asyncio.run(_client(dead)._post([{"role": "user", "content": "x"}], 16))
    assert caught.value.kind == "unreachable"
