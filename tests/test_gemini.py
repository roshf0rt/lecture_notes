import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from google.genai import types

from notes_bot.gemini import AUTH, INLINE_LIMIT_BYTES, QUOTA, GeminiClient, GeminiError


class FakeGeminiServer:
    """Stands in for the Gemini REST API and records every request the SDK sends."""

    def __init__(self, status: int, body: dict) -> None:
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, payload):
                server.requests.append({"method": self.command, "path": self.path, "body": payload})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self._respond(json.loads(self.rfile.read(length) or b"{}"))

            def do_GET(self):
                self._respond(None)

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def make_server():
    servers: list[FakeGeminiServer] = []

    def factory(status: int = 200, body: dict | None = None) -> FakeGeminiServer:
        server = FakeGeminiServer(status, body or {})
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.close()


def _reply(text: str, finish: str = "STOP") -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}]}


def _field(data: dict, camel: str, snake: str):
    """The SDK mixes camelCase and snake_case keys; the API accepts both."""
    return data[camel] if camel in data else data[snake]


def _client(server: FakeGeminiServer) -> GeminiClient:
    return GeminiClient("test-key", "gemini-test", base_url=server.url)


def test_write_sends_json_schema_and_parses_the_note(make_server):
    server = make_server(200, _reply(json.dumps({"title": "Тема", "icon": "🧪", "main_idea": "Идея", "sections": [{"heading": "Раздел", "elements": [{"type": "bullets", "items": ["Вывод"]}]}]})))

    doc = asyncio.run(_client(server).write("Текст лекции. " * 20, "short"))

    assert doc.title == "Тема"
    assert doc.sections[0].elements[0].items == ["Вывод"]
    request = server.requests[0]["body"]
    assert "gemini-test:generateContent" in server.requests[0]["path"]
    assert "systemInstruction" in request
    config = request["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert set(config["responseJsonSchema"]["properties"]) == {"title", "icon", "main_idea", "sections"}


def test_transcribe_sends_inline_audio_and_returns_trimmed_text(make_server):
    server = make_server(200, _reply("  Текст расшифровки.  "))
    audio = b"OggS-fake-opus-bytes"

    text = asyncio.run(_client(server).transcribe(audio, "voice.ogg"))

    assert text == "Текст расшифровки."
    parts = server.requests[0]["body"]["contents"][0]["parts"]
    inline = next(part["inlineData"] for part in parts if "inlineData" in part)
    assert _field(inline, "mimeType", "mime_type") == "audio/ogg"
    assert base64.b64decode(inline["data"]) == audio


def test_empty_transcript_is_reported_as_no_speech(make_server):
    server = make_server(200, _reply("   "))
    with pytest.raises(GeminiError, match="не нашлось речи"):
        asyncio.run(_client(server).transcribe(b"x", "voice.ogg"))


def test_large_audio_goes_through_files_api_and_is_deleted(make_server, monkeypatch):
    server = make_server(200, _reply("Длинная расшифровка."))
    client = _client(server)
    uploaded = SimpleNamespace(
        name="files/abc",
        uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
        state=types.FileState.ACTIVE,
    )
    calls: dict[str, object] = {}

    async def fake_upload(*, file, config):
        calls["upload_mime"] = config.mime_type
        calls["upload_size"] = len(file.getvalue())
        return uploaded

    async def fake_delete(*, name):
        calls["deleted"] = name

    monkeypatch.setattr(client._client.aio.files, "upload", fake_upload)
    monkeypatch.setattr(client._client.aio.files, "delete", fake_delete)

    audio = b"\0" * (INLINE_LIMIT_BYTES + 1)
    text = asyncio.run(client.transcribe(audio, "lecture.m4a"))

    assert text == "Длинная расшифровка."
    assert calls == {"upload_mime": "audio/m4a", "upload_size": len(audio), "deleted": "files/abc"}
    parts = server.requests[0]["body"]["contents"][0]["parts"]
    file_part = next(part["fileData"] for part in parts if "fileData" in part)
    assert _field(file_part, "fileUri", "file_uri") == uploaded.uri


def test_quota_error_becomes_a_friendly_message(make_server):
    server = make_server(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    with pytest.raises(GeminiError, match="лимит запросов"):
        asyncio.run(_client(server).write("Текст лекции. " * 20, "medium"))


def test_bad_key_becomes_a_friendly_message(make_server):
    server = make_server(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}})
    with pytest.raises(GeminiError, match="не принял ключ"):
        asyncio.run(_client(server).write("Текст лекции. " * 20, "medium"))


def test_truncated_answer_is_reported(make_server):
    server = make_server(200, _reply('{"title": "Обрыв', finish="MAX_TOKENS"))
    with pytest.raises(GeminiError, match="не уместился"):
        asyncio.run(_client(server).write("Текст лекции. " * 20, "long"))


def test_malformed_json_is_reported(make_server):
    server = make_server(200, _reply("это не JSON"))
    with pytest.raises(GeminiError, match="неверном формате"):
        asyncio.run(_client(server).write("Текст лекции. " * 20, "medium"))


def test_unsupported_format_is_rejected_before_any_request(make_server):
    server = make_server(200, _reply("unused"))
    with pytest.raises(GeminiError, match="не поддерживается"):
        asyncio.run(_client(server).transcribe(b"x", "notes.pdf"))
    assert server.requests == []


def test_daily_quota_error_carries_its_kind_and_retry_hint(make_server):
    body = {
        "error": {
            "code": 429,
            "message": "Quota exceeded",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
            ],
        }
    }
    server = make_server(429, body)
    with pytest.raises(GeminiError) as caught:
        asyncio.run(_client(server).check_key())
    assert caught.value.kind == QUOTA
    assert caught.value.daily is True
    assert caught.value.retry_after == 37


def test_aac_goes_inline_with_its_own_mime_type(make_server):
    server = make_server(200, _reply("Расшифровка"))
    asyncio.run(_client(server).transcribe(b"\xff\xf1aac", "audio.aac"))
    parts = server.requests[0]["body"]["contents"][0]["parts"]
    inline = next(_field(part, "inlineData", "inline_data") for part in parts if {"inlineData", "inline_data"} & part.keys())
    assert _field(inline, "mimeType", "mime_type") == "audio/aac"


def test_check_key_makes_a_tiny_generation_request(make_server):
    server = make_server(200, _reply("ok"))
    asyncio.run(_client(server).check_key())
    request = server.requests[0]
    assert request["method"] == "POST"
    assert request["path"].endswith("gemini-test:generateContent")
    assert request["body"]["generationConfig"]["maxOutputTokens"] == 16


@pytest.mark.parametrize(("status", "expected"), [(404, "недоступна"), (503, "перегружен")])
def test_model_problems_get_their_own_messages(make_server, status, expected):
    server = make_server(status, {"error": {"code": status, "message": "x", "status": "ERR"}})
    with pytest.raises(GeminiError, match=expected):
        asyncio.run(_client(server).write("Текст лекции. " * 20, "medium"))


def test_check_key_reports_a_rejected_key(make_server):
    body = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}
    server = make_server(400, body)
    with pytest.raises(GeminiError, match="не принял ключ") as caught:
        asyncio.run(_client(server).check_key())
    assert caught.value.kind == AUTH


def test_unreadable_recording_gets_a_clear_message(make_server):
    server = make_server(400, {"error": {"code": 400, "message": "Request contains an invalid argument.", "status": "INVALID_ARGUMENT"}})
    with pytest.raises(GeminiError, match="повреждена или пустая"):
        asyncio.run(_client(server).transcribe(b"OggS", "voice.ogg"))
