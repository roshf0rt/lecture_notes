import asyncio
import shutil
import subprocess

import pytest

from notes_bot.audio import AudioError, canonical_extension, detect, is_media, prepare, sniff

LIMIT = 12 * 1024 * 1024


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"OggS\x00\x02", ".ogg"),
        (b"\xff\xf1\x50\x80", ".aac"),
        (b"\xff\xf9\x50\x80", ".aac"),
        (b"\xff\xfb\x90\x64", ".mp3"),
        (b"ID3\x04\x00", ".mp3"),
        (b"\x00\x00\x00\x20ftypM4A \x00", ".m4a"),
        (b"\x00\x00\x00\x18ftypmp42\x00", ".mp4"),
        (b"\x00\x00\x00\x14ftypqt  \x00", ".mov"),
        (b"\x00\x00\x00\x14ftyp3gp4\x00", ".3gp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", ".wav"),
        (b"fLaC\x00", ".flac"),
        (b"#!AMR\n", ".amr"),
        (b"\x1a\x45\xdf\xa3\x01webm", ".webm"),
        (b"hello", None),
    ],
)
def test_sniff_recognises_formats_by_their_header(head, expected):
    assert sniff(head + b"\x00" * 64) == expected


def test_bytes_beat_a_wrong_name():
    # Telegram names an untitled audio "audio.mp3" even when it is AAC.
    assert detect(b"\xff\xf1\x50\x80" + b"\x00" * 60, "audio.mp3", None) == ".aac"


@pytest.mark.parametrize(
    ("name", "mime", "expected"),
    [
        ("Запись.AAC", None, ".aac"),
        (None, "audio/x-aac", ".aac"),
        (None, "audio/vnd.dlna.adts", ".aac"),
        ("memo.m4a", "audio/x-m4a", ".m4a"),
        ("voice.opus", None, ".ogg"),
        ("notes.pdf", "application/pdf", None),
    ],
)
def test_canonical_extension_from_name_or_mime(name, mime, expected):
    assert canonical_extension(name, mime) == expected


def test_is_media_accepts_any_audio_mime():
    assert is_media("recording", "audio/x-something")
    assert is_media("lecture.aac")
    assert not is_media("notes.pdf", "application/pdf")


def test_small_aac_goes_as_it_is():
    data = b"\xff\xf1\x50\x80" + b"\x00" * 100
    assert asyncio.run(prepare(data, "audio.mp3", "audio/mpeg", inline_limit=LIMIT)) == (data, "audio.aac")


def test_unknown_format_is_refused():
    with pytest.raises(AudioError, match="Не понимаю формат"):
        asyncio.run(prepare(b"hello there", "file.bin", "application/octet-stream", inline_limit=LIMIT))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_video_and_rare_formats_become_small_opus(tmp_path):
    source = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=3", "-shortest", "-c:a", "aac", "-c:v", "libx264", str(source)],
        check=True,
    )
    data, name = asyncio.run(prepare(source.read_bytes(), "clip.mp4", "video/mp4", inline_limit=LIMIT))
    assert name == "audio.ogg"
    assert data.startswith(b"OggS")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_broken_file_in_a_gemini_format_is_passed_through_when_ffmpeg_fails():
    data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 50
    assert asyncio.run(prepare(data, "x.mp4", "video/mp4", inline_limit=LIMIT)) == (data, "audio.mp4")


def test_file_on_disk_is_read_in_place(tmp_path):
    data = b"\xff\xf1\x50\x80" + b"\x00" * 100
    path = tmp_path / "recording"
    path.write_bytes(data)
    assert asyncio.run(prepare(None, "recording", None, inline_limit=LIMIT, path=path)) == (data, "audio.aac")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_big_file_on_disk_is_converted_without_being_loaded(tmp_path):
    source = tmp_path / "wide.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=5", "-ar", "44100",
                    "-ac", "2", str(source)], check=True)
    data, name = asyncio.run(prepare(None, None, None, inline_limit=1000, path=source))  # "big" = over the inline limit
    assert name == "audio.ogg" and data.startswith(b"OggS") and len(data) < source.stat().st_size / 5
