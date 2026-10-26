import pytest
from utils.telegram_cache import format_duration, build_caption, parse_caption


def test_format_duration():
    assert format_duration(None) == "0:00"
    assert format_duration(-5) == "0:00"
    assert format_duration(0) == "0:00"
    assert format_duration(5) == "0:05"
    assert format_duration(65) == "1:05"
    assert format_duration(214) == "3:34"
    assert format_duration(3600) == "1:00:00"
    assert format_duration(3665) == "1:01:05"


def test_build_caption_with_title():
    caption = build_caption(
        video_id="dQw4w9WgXcQ",
        mode="audio",
        ext="m4a",
        size_mb=3.29,
        duration=213,
        title="Rick Astley - Never Gonna Give You Up",
    )
    lines = caption.split("\n")
    assert len(lines) == 4
    assert lines[0] == "Rick Astley - Never Gonna Give You Up"
    assert lines[1] == "yt:dQw4w9WgXcQ:audio"
    assert lines[2] == "m4a | 3.29MB | 3:33"
    assert lines[3] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_build_caption_without_title():
    caption = build_caption(
        video_id="dQw4w9WgXcQ",
        mode="video",
        ext="mp4",
        size_mb=15.2,
        duration=213,
        title=None,
    )
    lines = caption.split("\n")
    assert len(lines) == 3
    assert lines[0] == "yt:dQw4w9WgXcQ:video"
    assert lines[1] == "mp4 | 15.2MB | 3:33"
    assert lines[2] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_build_caption_truncation():
    huge_title = "A" * 2000
    caption = build_caption(
        video_id="dQw4w9WgXcQ",
        mode="audio",
        ext="m4a",
        size_mb=3.29,
        duration=213,
        title=huge_title,
    )
    assert len(caption) <= 1024
    lines = caption.split("\n")
    assert len(lines) == 4
    assert lines[1] == "yt:dQw4w9WgXcQ:audio"
    assert lines[2] == "m4a | 3.29MB | 3:33"
    assert lines[3] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_parse_caption():
    caption_audio = (
        "Rick Astley - Never Gonna Give You Up\n"
        "yt:dQw4w9WgXcQ:audio\n"
        "m4a | 3.29MB | 3:33\n"
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert parse_caption(caption_audio) == ("dQw4w9WgXcQ", "audio")

    caption_video = (
        "yt:dQw4w9WgXcQ:video\n"
        "mp4 | 15.2MB | 3:33\n"
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert parse_caption(caption_video) == ("dQw4w9WgXcQ", "video")

    assert parse_caption("") is None
    assert parse_caption(None) is None
    assert parse_caption("random text with no tag") is None
    assert parse_caption("yt::audio") is None
    assert parse_caption("yt:dQw4w9WgXcQ:invalidmode") is None
