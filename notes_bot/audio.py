"""Recognise what a recording really is and turn it into something Gemini reads well.

Telegram often gives a file a wrong or empty name (an AAC voice memo arrives as
"audio.mp3", a document has no extension), so the format is taken from the bytes
first, then from the MIME type, and only then from the name. With ffmpeg on the
server, video and less common audio are converted to mono Opus: the file shrinks
several times, so it usually fits into one inline request.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Formats Gemini accepts as they are, by our canonical extension.
GEMINI_MIME = {
    ".ogg": "audio/ogg",
    ".mp3": "audio/mp3",
    ".aac": "audio/aac",
    ".m4a": "audio/m4a",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aiff": "audio/aiff",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
    ".mov": "video/mov",
}
# Everything we try to read: the formats above plus ones that need ffmpeg.
EXTENSION_ALIASES = {
    ".ogg": ".ogg", ".oga": ".ogg", ".opus": ".ogg",
    ".mp3": ".mp3", ".mpeg": ".mp3", ".mpga": ".mp3",
    ".aac": ".aac", ".adts": ".aac",
    ".m4a": ".m4a", ".m4b": ".m4a", ".mp4a": ".m4a",
    ".wav": ".wav", ".wave": ".wav",
    ".flac": ".flac",
    ".aif": ".aiff", ".aiff": ".aiff",
    ".webm": ".webm", ".weba": ".webm", ".mkv": ".mkv",
    ".mp4": ".mp4", ".m4v": ".mp4", ".3gp": ".3gp", ".3gpp": ".3gp",
    ".mov": ".mov", ".avi": ".avi",
    ".amr": ".amr", ".wma": ".wma", ".caf": ".caf",
}
MIME_ALIASES = {
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/x-opus+ogg": ".ogg", "application/ogg": ".ogg",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mpeg3": ".mp3", "audio/x-mpeg": ".mp3",
    "audio/aac": ".aac", "audio/x-aac": ".aac", "audio/aacp": ".aac", "audio/vnd.dlna.adts": ".aac",
    "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/vnd.wave": ".wav",
    "audio/flac": ".flac", "audio/x-flac": ".flac",
    "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
    "audio/webm": ".webm", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-msvideo": ".avi",
    "audio/3gpp": ".3gp", "video/3gpp": ".3gp", "audio/amr": ".amr", "audio/x-ms-wma": ".wma",
    "audio/x-caf": ".caf",
}
INLINE_FRIENDLY = {".ogg", ".mp3", ".aac", ".m4a", ".wav", ".flac"}
CONVERT_TIMEOUT_SECONDS = 600
SUPPORTED_HINT = "ogg, opus, mp3, aac, m4a, wav, flac, amr, webm, mp4 или mov"


class AudioError(Exception):
    """The recording cannot be read."""


def sniff(data: bytes) -> str | None:
    """Return the canonical extension that the file header shows, or None."""
    head = data[:64]
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return ".wav"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return ".avi"
    if head.startswith(b"FORM") and head[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    if head.startswith(b"#!AMR"):
        return ".amr"
    if head.startswith(b"caff"):
        return ".caf"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ".mkv" if b"matroska" in data[:4096] else ".webm"
    if head.startswith(b"\x30\x26\xb2\x75"):
        return ".wma"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith(b"3g"):
            return ".3gp"
        if brand == b"qt  ":
            return ".mov"
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return ".m4a"
        return ".mp4"
    if head.startswith(b"ID3"):
        return ".mp3"
    if len(head) >= 2 and head[0] == 0xFF:
        # ADTS (AAC) has layer bits 00; MPEG audio frames (MP3) have a non-zero layer.
        if head[1] & 0xF6 == 0xF0:
            return ".aac"
        if head[1] & 0xE0 == 0xE0:
            return ".mp3"
    return None


def canonical_extension(filename: str | None, mime_type: str | None = None) -> str | None:
    if mime_type:
        found = MIME_ALIASES.get(mime_type.split(";")[0].strip().lower())
        if found:
            return found
    if filename:
        return EXTENSION_ALIASES.get(Path(filename).suffix.lower())
    return None


def is_media(filename: str | None, mime_type: str | None = None) -> bool:
    if mime_type and mime_type.lower().startswith(("audio/", "video/")):
        return True
    return canonical_extension(filename, mime_type) is not None


def detect(data: bytes, filename: str | None, mime_type: str | None = None) -> str | None:
    """Best guess for the real format: bytes first, then MIME, then the name."""
    return sniff(data) or canonical_extension(filename, mime_type)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _read_head(path: Path) -> bytes:
    with path.open("rb") as file:
        return file.read(4096)


async def _ffmpeg(data: bytes | None, path: Path | None, extension: str, output_args: list[str], output_name: str) -> bytes:
    """Run ffmpeg on the recording. A file already on disk is read in place, not copied."""
    with tempfile.TemporaryDirectory(prefix="notes-bot-") as folder:
        source = path
        if source is None:
            source = Path(folder) / f"input{extension}"
            source.write_bytes(data)
        target = Path(folder) / output_name
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", *output_args, str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), CONVERT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise AudioError("Запись слишком долго конвертируется.") from None
        if process.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            log.warning("ffmpeg failed for %s: %s", extension, stderr.decode(errors="replace")[-500:])
            raise AudioError("Не получилось прочитать запись.")
        return target.read_bytes()


async def _to_opus(data: bytes | None, path: Path | None, extension: str) -> bytes:
    """Convert to mono Opus."""
    return await _ffmpeg(data, path, extension, ["-c:a", "libopus", "-b:a", "24k"], "output.ogg")


async def to_mp3(data: bytes, extension: str) -> bytes:
    """Convert to mono MP3, the format the gateway accepts for audio input."""
    return await _ffmpeg(data, None, extension, ["-c:a", "libmp3lame", "-b:a", "32k"], "output.mp3")


async def prepare(
    data: bytes | None,
    filename: str | None,
    mime_type: str | None = None,
    *,
    inline_limit: int,
    path: Path | None = None,
) -> tuple[bytes, str]:
    """Return audio bytes and a file name whose extension matches the real format.

    The recording comes either as bytes or as a path to a file on disk (big files from a
    local Bot API server are never loaded whole into memory). Small files in a format
    Gemini reads go as they are. Video, rare formats and big files are converted to Opus
    when ffmpeg is installed.
    """
    if data is None and path is None:
        raise AudioError("Нет записи для обработки.")
    head = data[:4096] if data is not None else await asyncio.to_thread(_read_head, path)
    size = len(data) if data is not None else path.stat().st_size
    extension = detect(head, filename, mime_type)
    if extension is None:
        raise AudioError(f"Не понимаю формат записи. Подойдут {SUPPORTED_HINT}.")

    async def whole() -> bytes:
        return data if data is not None else await asyncio.to_thread(path.read_bytes)

    if extension in INLINE_FRIENDLY and size <= inline_limit:
        return await whole(), f"audio{extension}"
    if ffmpeg_available():
        try:
            return await _to_opus(data, path, extension), "audio.ogg"
        except AudioError:
            if extension not in GEMINI_MIME:
                raise
    if extension in GEMINI_MIME:
        return await whole(), f"audio{extension}"
    raise AudioError(f"Формат {extension} нужно сконвертировать, а ffmpeg на сервере не установлен.")
