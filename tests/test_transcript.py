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
        "Если вы пришлёте файл с заданием, мы проверим его на практике. "
        "Событийный маркетинг строится на живом опыте участников и встрече с брендом. "
        "Информация сообщает факт, а реклама пытается изменить решение человека. "
        "Запоминается не баннер, а собственное впечатление от происходящего. "
    )
    assert junk_reason(lecture) is None
    assert junk_reason("Первая мысль. Вторая мысль.\nТретья мысль.\nЧетвёртая мысль.") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_the_gateway_reply_without_audio_is_refused_so_another_key_can_try(monkeypatch):
    import notes_bot.gateway as gateway

    monkeypatch.setattr(gateway, "PIECE_RETRY_DELAY", 0)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"message": {"content": NO_AUDIO_REPLY}, "finish_reason": "stop"}]}))
    client = GatewayClient(GATEWAY_KEY, "gemini-3.5-flash", transport=transport)
    wav = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    with pytest.raises(GeminiError) as caught:
        asyncio.run(client.transcribe(wav, "x.wav"))
    assert caught.value.kind == MODEL and "не распознал часть 1 из 1" in str(caught.value)


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


def test_speech_is_never_sent_to_a_gateway_key():
    calls = []
    model = RotatingModel([GATEWAY_KEY], lambda key: ScriptedClient(key, calls), KeyRing())
    with pytest.raises(GeminiError, match="ключ Google"):
        asyncio.run(model.transcribe(b"", "x.ogg"))
    assert calls == []


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


def _wav():
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_piece_the_gateway_failed_to_hear_is_asked_again(monkeypatch):
    import notes_bot.gateway as gateway

    monkeypatch.setattr(gateway, "PIECE_RETRY_DELAY", 0)
    replies = iter([NO_AUDIO_REPLY, "Настоящая расшифровка куска."])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}, "finish_reason": "stop"}]})

    client = GatewayClient(GATEWAY_KEY, "gemini-3.5-flash", transport=httpx.MockTransport(handler))
    assert asyncio.run(client.transcribe(_wav(), "x.wav")) == "Настоящая расшифровка куска."
    assert len(requests) == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_piece_that_never_comes_back_fails_with_its_number(monkeypatch):
    import notes_bot.gateway as gateway

    monkeypatch.setattr(gateway, "PIECE_RETRY_DELAY", 0)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": NO_AUDIO_REPLY}, "finish_reason": "stop"}]})

    client = GatewayClient(GATEWAY_KEY, "gemini-3.5-flash", transport=httpx.MockTransport(handler))
    with pytest.raises(GeminiError, match="часть 1 из 1") as caught:
        asyncio.run(client.transcribe(_wav(), "x.wav"))
    assert caught.value.kind == MODEL and len(requests) == gateway_attempts()


def gateway_attempts():
    from notes_bot.gateway import PIECE_ATTEMPTS

    return PIECE_ATTEMPTS


def test_requests_for_a_file_in_other_wordings_are_junk_and_stripped():
    from notes_bot.transcript import junk_reason, strip_requests

    ask1 = "Пожалуйста, предоставьте аудиофайл или текст записи, которую нужно расшифровать."
    ask2 = "Пожалуйста, прикрепите аудиофайл или видеозапись, которую необходимо расшифровать. Без исходного материала я не могу выполнить вашу просьбу."
    assert junk_reason(ask1 + "\n\n" + ask1) and junk_reason(ask2)
    real = "21 на 8. Так? И еще один способ, то есть мы могли, например, 3/8 домножить на два. " * 3
    assert junk_reason(real) is None
    assert strip_requests(f"{ask1}\n\n{real}\n\n{ask2}") == real.strip()


def test_a_loop_on_one_line_is_not_a_transcript():
    loop = "за- не " * 200
    assert junk_reason(loop.strip())
    real = "Событийный маркетинг строится на опыте участников, и мероприятие становится каналом для бренда. " * 4
    assert junk_reason(real) is None


def test_a_loop_at_the_start_then_normal_text_is_still_not_a_transcript():
    lecture = (
        "Событийный маркетинг строится на опыте участников и встрече с брендом. " * 2
        + "за- не " * 60
        + " Реклама пытается изменить решение человека, а информация сообщает факт. "
        "Участник запоминает не баннер, а собственное впечатление от происходящего. " * 6
    )
    assert junk_reason(lecture)


def test_a_russian_recording_translated_to_english_is_refused_when_russian_is_set():
    from notes_bot.pipeline import mostly_latin

    english = (
        "Today we talk about event marketing and how people remember brands after a meeting. "
        "Advertising tries to change a decision, while information only reports a fact. "
        "Participants remember their own impression of the place, not the banner on the wall. "
        "Next we look at a festival where organisers counted repeat visits instead of guests. "
    )
    assert mostly_latin(english) and not mostly_latin("Сегодня говорим о событийном маркетинге. " * 5)

    class TranslatingModel:
        async def transcribe(self, audio, filename):
            return english

        async def write(self, *args, **kwargs):
            raise AssertionError("a note must not be written from a translation")

    async def progress(stage, detail=""):
        return None

    source = Source(audio=b"OggS" + b"\x00" * 60, name="v.ogg")
    with pytest.raises(GeminiError, match="вышла на английском"):
        asyncio.run(NotesPipeline(TranslatingModel()).run(UserSettings(language="ru"), progress=progress, sources=[source]))
    # With auto the same text is accepted: an English lecture is a legitimate choice there.
    note = asyncio.run(NotesPipeline(TranslatingModel()).read([source], progress=progress, language="auto"))
    assert note == english
