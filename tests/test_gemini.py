import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from google.genai import types

from notes_bot.gemini import INLINE_LIMIT_BYTES, GeminiClient, GeminiError, is_media_name


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
    server = make_server(200, _reply(json.dumps({"title": "Тема", "icon": "🧪", "takeaways": ["Вывод"]})))

    doc = asyncio.run(_client(server).write("Текст лекции. " * 20, ["takeaways"], "short"))

    assert doc.title == "Тема"
    assert doc.takeaways == ["Вывод"]
    request = server.requests[0]["body"]
    assert "gemini-test:generateContent" in server.requests[0]["path"]
    assert "systemInstruction" in request
    config = request["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert set(config["responseJsonSchema"]["properties"]) == {"title", "icon", "takeaways"}


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
        asyncio.run(_client(server).write("Текст лекции. " * 20, ["takeaways"], "medium"))


def test_bad_key_becomes_a_friendly_message(make_server):
    server = make_server(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}})
    with pytest.raises(GeminiError, match="не принял ключ"):
        asyncio.run(_client(server).write("Текст лекции. " * 20, ["takeaways"], "medium"))


def test_truncated_answer_is_reported(make_server):
    server = make_server(200, _reply('{"title": "Обрыв', finish="MAX_TOKENS"))
    with pytest.raises(GeminiError, match="не уместился"):
        asyncio.run(_client(server).write("Текст лекции. " * 20, ["takeaways"], "long"))


def test_malformed_json_is_reported(make_server):
    server = make_server(200, _reply("это не JSON"))
    with pytest.raises(GeminiError, match="неверном формате"):
        asyncio.run(_client(server).write("Текст лекции. " * 20, ["takeaways"], "medium"))


def test_unsupported_format_is_rejected_before_any_request(make_server):
    server = make_server(200, _reply("unused"))
    with pytest.raises(GeminiError, match="не поддерживается"):
        asyncio.run(_client(server).transcribe(b"x", "notes.pdf"))
    assert server.requests == []


@pytest.mark.parametrize(("name", "expected"), [("voice.ogg", True), ("lecture.MP3", True), ("notes.pdf", False)])
def test_is_media_name(name, expected):
    assert is_media_name(name) is expected


def test_check_key_fetches_the_configured_model(make_server):
    server = make_server(200, {"name": "models/gemini-test"})
    asyncio.run(_client(server).check_key())
    assert server.requests[0]["method"] == "GET"
    assert server.requests[0]["path"].endswith("/models/gemini-test")


def test_check_key_reports_a_rejected_key(make_server):
    body = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}
    server = make_server(400, body)
    with pytest.raises(GeminiError, match="не принял ключ"):
        asyncio.run(_client(server).check_key())
