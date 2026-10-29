import asyncio
import shutil
import subprocess

import pytest

from notes_bot.audio import AudioError, duration_seconds, canonical_extension, detect, find_pauses, is_media, plan_cuts, prepare, sniff, split_mp3, trim_silence

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


MAX_PIECE = 15 * 60


def test_a_short_recording_is_not_cut():
    assert plan_cuts(14 * 60, [300.0], MAX_PIECE) == []
    assert plan_cuts(MAX_PIECE, [], MAX_PIECE) == []


def test_seventeen_minutes_make_two_equal_halves_not_fifteen_and_two():
    cuts = plan_cuts(17 * 60, [], MAX_PIECE)
    assert cuts == [pytest.approx(510.0)]


def test_a_cut_moves_to_the_nearest_pause_near_the_even_split():
    assert plan_cuts(17 * 60, [100.0, 530.0, 900.0], MAX_PIECE) == [530.0]
    assert plan_cuts(17 * 60, [100.0, 900.0], MAX_PIECE) == [pytest.approx(510.0)]  # no pause close enough


def test_a_cut_does_not_move_when_the_pieces_already_fill_the_limit():
    # 30 minutes in two pieces of exactly 15: there is no slack, so a pause nearby changes nothing.
    assert plan_cuts(30 * 60, [880.0], MAX_PIECE) == [pytest.approx(900.0)]


def test_a_recording_just_over_the_limit_still_gets_cut():
    assert len(plan_cuts(900.5, [], MAX_PIECE)) == 1


def test_no_piece_is_ever_longer_than_the_limit_and_the_count_is_the_smallest():
    import math
    import random

    generator = random.Random(7)
    for _ in range(300):
        duration = generator.uniform(MAX_PIECE + 0.1, 4 * 3600)
        pauses = sorted(generator.uniform(0, duration) for _ in range(generator.randint(0, 60)))
        cuts = plan_cuts(duration, pauses, MAX_PIECE)
        edges = [0.0, *cuts, duration]
        lengths = [end - start for start, end in zip(edges, edges[1:])]
        assert len(lengths) == math.ceil(duration / MAX_PIECE)
        assert max(lengths) <= MAX_PIECE + 1e-6, (duration, lengths)
        assert min(lengths) > 0


def test_ninety_minutes_are_six_equal_pieces():
    cuts = plan_cuts(90 * 60, [], MAX_PIECE)
    assert len(cuts) == 5
    assert cuts[0] == pytest.approx(900.0) and cuts[-1] == pytest.approx(4500.0)


def test_cuts_never_pile_up_on_the_same_pause():
    cuts = plan_cuts(31 * 60, [930.0, 931.0], MAX_PIECE)
    assert len(cuts) == 2 and cuts[1] - cuts[0] >= 20


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_pause_in_the_speech_is_found():
    wav = subprocess.run(
        ["ffmpeg", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=16000",
         "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=16000",
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    pauses = asyncio.run(find_pauses(wav, ".wav"))
    assert len(pauses) == 1 and 2.0 <= pauses[0] <= 3.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_split_at_the_planned_moments_gives_one_piece_per_part():
    wav = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=6:sample_rate=16000",
         "-f", "wav", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    assert len(asyncio.run(split_mp3(wav, ".wav", [2.0, 4.0]))) == 3
    assert len(asyncio.run(split_mp3(wav, ".wav", []))) == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_long_silences_are_cut_out_and_short_ones_stay():
    mp3 = subprocess.run(
        ["ffmpeg", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=16000",
         "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=16000",
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", "-c:a", "libmp3lame", "-b:a", "32k", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    trimmed = asyncio.run(trim_silence(mp3))
    before = asyncio.run(duration_seconds(mp3, ".mp3"))
    after = asyncio.run(duration_seconds(trimmed, ".mp3"))
    assert before > 7.5 and before - after >= 2.0  # most of the four seconds of silence is gone
    assert after >= 4.0  # both seconds of tone on each side are still there
    assert len(trimmed) < len(mp3)


def test_trimming_never_loses_the_recording_when_ffmpeg_fails():
    garbage = b"this is not audio" * 20
    assert asyncio.run(trim_silence(garbage)) == garbage
