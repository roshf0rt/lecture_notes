import asyncio
import shutil
import subprocess

import httpx
import pytest

from notes_bot.gateway import GatewayClient
from notes_bot.gemini import MODEL, GeminiError
from notes_bot.keyring import KeyRing, RotatingModel
from notes_bot.pipeline import NotesPipeline, Source
from notes_bot.settings import UserSettings
from notes_bot.transcript import junk_reason

# What the supplier's gateway really returned for a recording: the model never got the audio.
NO_AUDIO_REPLY = (
    "Пожалуйста, прикрепите файл или ссылку на запись, которую нужно расшифровать.\n\n"
    "Пожалуйста, предоставьте аудиозапись или файл, который нужно расшифровать.\n\n"
    "Пожалуйста, прикрепите файл с записью, которую нужно расшифровать."
)
GATEWAY_KEY = "sk-cvc-" + "b" * 64
GOOGLE_KEY = "AIzaSyTestKeyValue1234567890abcdefghij"


def test_requests_for_a_file_are_not_a_transcript():
    assert junk_reason(NO_AUDIO_REPLY)
    assert junk_reason("Пожалуйста, прикрепите файл.")  # one request and nothing else


def test_endless_repetition_is_not_a_transcript():
    assert junk_reason("\n".join(["Это тест."] * 12))


def test_a_real_transcript_passes_even_if_it_mentions_files():
    lecture = (
        "Сегодня разберём, как устроена реклама и чем она отличается от информации. "
        "Если вы пришлёте файл с заданием, мы проверим его на практике. " * 8
    )
    assert junk_reason(lecture) is None
    assert junk_reason("Первая мысль. Вторая мысль.\nТретья мысль.\nЧетвёртая мысль.") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_the_gateway_reply_without_audio_is_refused_so_another_key_can_try():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"message": {"content": NO_AUDIO_REPLY}, "finish_reason": "stop"}]}))
    client = GatewayClient(GATEWAY_KEY, "gemini-3.5-flash", transport=transport)
    wav = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    with pytest.raises(GeminiError) as caught:
        asyncio.run(client.transcribe(wav, "x.wav"))
    assert caught.value.kind == MODEL and "не распознал аудио" in str(caught.value)


class ScriptedClient:
    def __init__(self, key, calls):
        self.key, self.calls = key, calls

    async def transcribe(self, audio, filename):
        self.calls.append(self.key)
        return "расшифровка " + self.key[:4]

    async def check_key(self):
        self.calls.append(self.key)


def test_speech_goes_to_google_keys_before_the_gateway_key():
    calls = []
    model = RotatingModel([GATEWAY_KEY, GOOGLE_KEY], lambda key: ScriptedClient(key, calls), KeyRing())
    assert asyncio.run(model.transcribe(b"", "x.ogg")) == "расшифровка AIza"
    assert calls == [GOOGLE_KEY]


def test_the_gateway_key_is_still_used_when_it_is_the_only_one():
    calls = []
    model = RotatingModel([GATEWAY_KEY], lambda key: ScriptedClient(key, calls), KeyRing())
    assert asyncio.run(model.transcribe(b"", "x.ogg")).startswith("расшифровка")
    assert calls == [GATEWAY_KEY]


def test_no_note_is_made_from_a_junk_transcript():
    class JunkModel:
        async def transcribe(self, audio, filename):
            return NO_AUDIO_REPLY

        async def write(self, *args, **kwargs):
            raise AssertionError("a note must not be written from junk")

    async def progress(stage, detail=""):
        return None

    with pytest.raises(GeminiError, match="Расшифровка не получилась"):
        asyncio.run(NotesPipeline(JunkModel()).run(
            UserSettings(), progress=progress, sources=[Source(audio=b"OggS" + b"\x00" * 60, name="v.ogg")]))


def test_the_model_reports_when_a_gateway_key_recognised_the_speech():
    from notes_bot.gemini import QUOTA

    class QuotaGoogle(ScriptedClient):
        async def transcribe(self, audio, filename):
            if self.key == GOOGLE_KEY:
                raise GeminiError("limit", QUOTA)
            return await super().transcribe(audio, filename)

    model = RotatingModel([GOOGLE_KEY, GATEWAY_KEY], lambda key: QuotaGoogle(key, []), KeyRing())
    assert model.gateway_transcribed is False
    asyncio.run(model.transcribe(b"", "x.ogg"))
    assert model.gateway_transcribed is True

    quiet = RotatingModel([GOOGLE_KEY], lambda key: ScriptedClient(key, []), KeyRing())
    asyncio.run(quiet.transcribe(b"", "x.ogg"))
    assert quiet.gateway_transcribed is False
