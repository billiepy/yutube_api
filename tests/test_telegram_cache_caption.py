import datetime
import pytest
from utils.telegram_cache import (
    format_duration,
    build_caption,
    parse_caption,
    probe_file_metadata,
)


def test_format_duration():
    assert format_duration(None) == "0:00"
    assert format_duration(-5) == "0:00"
    assert format_duration(0) == "0:00"
    assert format_duration(5) == "0:05"
    assert format_duration(65) == "1:05"
    assert format_duration(214) == "3:34"
    assert format_duration(3600) == "1:00:00"
    assert format_duration(3665) == "1:01:05"


def test_build_caption_full_layout():
    cached_time = datetime.datetime(2026, 10, 26, 7, 0, tzinfo=datetime.timezone.utc)
    caption = build_caption(
        video_id="dQw4w9WgXcQ",
        mode="audio",
        ext="m4a",
        size_mb=3.29,
        duration=213,
        title="Rick Astley - Never Gonna Give You Up",
        uploader="RickAstleyVEVO",
        codecs="AAC",
        quality="128 kbps",
        cached_at=cached_time,
    )
    expected_lines = [
        "🎵 Title: Rick Astley - Never Gonna Give You Up",
        "📺 Channel: RickAstleyVEVO",
        "⏱ Duration: 3:33",
        "💾 Size: 3.29 MB",
        "📁 Format: m4a · AAC",
        "🎧 Quality: 128 kbps",
        "🎼 Type: audio",
        "🌐 Source: YouTube",
        "🆔 ID: dQw4w9WgXcQ",
        "🔗 Link: https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "🕒 Cached: 2026-10-26 07:00 UTC",
    ]
    assert caption.split("\n") == expected_lines


def test_build_caption_skips_unknown_values():
    caption = build_caption(
        video_id="dQw4w9WgXcQ",
        mode="video",
        ext="mp4",
        size_mb=15.2,
        duration=None,
        title=None,
        uploader=None,
        codecs=None,
        quality=None,
        cached_at=None,
    )
    expected_lines = [
        "💾 Size: 15.2 MB",
        "📁 Format: mp4",
        "🎼 Type: video",
        "🌐 Source: YouTube",
        "🆔 ID: dQw4w9WgXcQ",
        "🔗 Link: https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    ]
    assert caption.split("\n") == expected_lines
    # Verify skipped lines
    assert "Title" not in caption
    assert "Channel" not in caption
    assert "Duration" not in caption
    assert "Quality" not in caption
    assert "Cached" not in caption


def test_build_caption_truncation():
    huge_title = "A" * 2000
    caption = build_caption(
        video_id="dQw4w9WgXcQ",
        mode="audio",
        ext="m4a",
        size_mb=3.29,
        duration=213,
        title=huge_title,
        uploader="RickAstleyVEVO",
        codecs="AAC",
        quality="128 kbps",
        cached_at="2026-10-26 07:00",
    )
    assert len(caption) < 1000
    lines = caption.split("\n")
    assert lines[0].startswith("🎵 Title: AAAAA")
    assert "📺 Channel: RickAstleyVEVO" in lines
    assert "🆔 ID: dQw4w9WgXcQ" in lines


def test_parse_caption_new_format():
    # 1. Full new layout with emojis
    caption_new = (
        "🎵 Title: Rick Astley - Never Gonna Give You Up\n"
        "📺 Channel: RickAstleyVEVO\n"
        "⏱ Duration: 3:33\n"
        "💾 Size: 3.29 MB\n"
        "📁 Format: m4a · AAC\n"
        "🎧 Quality: 128 kbps\n"
        "🎼 Type: audio\n"
        "🌐 Source: YouTube\n"
        "🆔 ID: dQw4w9WgXcQ\n"
        "🔗 Link: https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
        "🕒 Cached: 2026-10-26 07:00 UTC"
    )
    assert parse_caption(caption_new) == ("dQw4w9WgXcQ", "audio")

    # Tolerant of extra spaces and missing emoji prefix
    caption_tolerant = (
        "Some random title\n"
        "🆔   ID  :   dQw4w9WgXcQ  \n"
        "🎼   Type :  video  \n"
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert parse_caption(caption_tolerant) == ("dQw4w9WgXcQ", "video")

    caption_no_emoji = (
        "ID: dQw4w9WgXcQ\n"
        "Type: audio\n"
    )
    assert parse_caption(caption_no_emoji) == ("dQw4w9WgXcQ", "audio")


def test_parse_caption_previous_format():
    # Previous format: yt:<id>:<mode>
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


def test_parse_caption_oldest_format_returns_none():
    # Oldest format: plain YouTube URL only -> return None
    assert parse_caption("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert parse_caption("Rick Astley\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert parse_caption("https://youtu.be/dQw4w9WgXcQ") is None
    assert parse_caption("") is None
    assert parse_caption(None) is None
    assert parse_caption("random text without required tags") is None
    assert parse_caption("yt::audio") is None
    assert parse_caption("yt:dQw4w9WgXcQ:invalidmode") is None
    # Incomplete new format (missing Type or missing ID) -> return None
    assert parse_caption("🆔 ID: dQw4w9WgXcQ") is None
    assert parse_caption("🎼 Type: audio") is None
