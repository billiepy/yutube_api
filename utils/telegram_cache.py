"""Telegram media cache and background upload worker.

Manages MongoDB indexing and caching of resolved YouTube media uploaded to a
private Telegram channel, guarded by a Redis deduplication lock and an independent
upload concurrency limiter.
"""
import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Optional, Union

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

import config
from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    MONGO_URI,
    CACHE_DB_NAME,
    CACHE_CHANNEL_ID,
    UPLOAD_CONCURRENCY,
)
from tools import get_async_redis, get_async_mongo_client, get_async_mongo_db
from utils.media_downloader import download_media

logger = logging.getLogger("ytube_api.tg_cache")

# Global upload concurrency limiter (independent from subprocess_slot)
upload_slot = asyncio.Semaphore(UPLOAD_CONCURRENCY)

_telegram_client = None
_index_ensured = False


# ── MongoDB Helpers & Collection ──────────────────────────────────────────────

async def _ensure_indexes(col) -> None:
    """Create compound unique index on (video_id, mode) if not already created."""
    global _index_ensured
    if not _index_ensured:
        try:
            await col.create_index([("video_id", 1), ("mode", 1)], unique=True)
            _index_ensured = True
        except Exception as e:
            logger.warning(f"[TG_CACHE] Failed to ensure index: {e}")


async def get_cached_entry(video_id: str, mode: str) -> Optional[dict]:
    """Retrieve cached Telegram media entry from MongoDB by (video_id, mode)."""
    try:
        db = await get_async_mongo_db()
        col = db["telegram_cache"]
        await _ensure_indexes(col)
        doc = await col.find_one({"video_id": video_id, "mode": mode}, {"_id": 0})
        return doc
    except Exception as e:
        logger.error(f"[TG_CACHE] MongoDB lookup error for {video_id}:{mode}: {e}")
        return None


async def save_cached_entry(
    video_id: str,
    mode: str,
    chat_id: int,
    message_id: int,
    file_size: Optional[int] = None,
    mime_type: Optional[str] = None,
    duration: Optional[int] = None,
) -> dict:
    """Upsert cached Telegram media entry into MongoDB.

    Uses MongoDB's compound unique index upsert semantics to handle concurrent
    worker writes safely without an application-level check-then-insert race.
    """
    db = await get_async_mongo_db()
    col = db["telegram_cache"]
    await _ensure_indexes(col)

    now = datetime.datetime.now(datetime.timezone.utc)
    set_fields = {
        "telegram_chat_id": int(chat_id),
        "telegram_message_id": int(message_id),
        "cached_at": now,
    }
    if file_size is not None:
        set_fields["file_size"] = int(file_size)
    if mime_type is not None:
        set_fields["mime_type"] = str(mime_type)
    if duration is not None:
        set_fields["duration"] = int(duration)

    entry = {
        "video_id": video_id,
        "mode": mode,
        **set_fields,
    }

    try:
        await col.update_one(
            {"video_id": video_id, "mode": mode},
            {
                "$set": set_fields,
                "$setOnInsert": {
                    "video_id": video_id,
                    "mode": mode,
                },
            },
            upsert=True,
        )
    except DuplicateKeyError:
        # If a concurrent upsert race won at the index level, update the document
        await col.update_one(
            {"video_id": video_id, "mode": mode},
            {
                "$set": set_fields,
            },
        )

    logger.info(f"[TG_CACHE] Saved cache entry: {video_id}:{mode} -> msg={message_id}")
    return entry


# ── Telegram Client Management ────────────────────────────────────────────────

def set_telegram_client(client) -> None:
    """Allow injecting an already initialized Telegram client."""
    global _telegram_client
    _telegram_client = client


async def get_telegram_client():
    """Retrieve or lazy-initialize a Telegram client for background uploads.

    If no client has been injected (e.g. FastAPI process running separately from
    bot.py), a dedicated in-memory client is initialized with no_updates=True so
    it does not interfere with the long-polling bot process.
    """
    global _telegram_client
    if _telegram_client is not None and getattr(_telegram_client, "is_connected", False):
        return _telegram_client

    if not BOT_TOKEN:
        logger.warning("[TG_CACHE] BOT_TOKEN not configured; Telegram client unavailable")
        return None

    try:
        from pyrogram import Client
        client = Client(
            "ytube_cache_uploader",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            in_memory=True,
            ipv6=False,
            no_updates=True,
        )
        await client.start()
        _telegram_client = client
        logger.info("[TG_CACHE] Started background Telegram cache client")
        return _telegram_client
    except Exception as e:
        logger.error(f"[TG_CACHE] Failed to start Telegram client: {e}")
        return None


async def close_telegram_client() -> None:
    """Stop the cached Telegram client on shutdown."""
    global _telegram_client
    if _telegram_client is not None:
        try:
            await _telegram_client.stop()
        except Exception:
            pass
        _telegram_client = None


# ── Metadata Probe & Caption Builders ──────────────────────────────────────────

_CODEC_MAP = {
    "aac": "AAC",
    "h264": "H.264",
    "avc": "H.264",
    "avc1": "H.264",
    "hevc": "H.265",
    "h265": "H.265",
    "av01": "AV1",
    "av1": "AV1",
    "vp9": "VP9",
    "vp8": "VP8",
    "opus": "Opus",
    "mp3": "MP3",
    "flac": "FLAC",
    "vorbis": "Vorbis",
}


def _format_codec_name(name: str) -> str:
    if not name:
        return ""
    clean = name.strip()
    return _CODEC_MAP.get(clean.lower(), clean.upper())


def probe_file_metadata(file_path: str, mode: Optional[str] = None) -> dict:
    """Run ffprobe on downloaded/stripped file to read duration, size, codecs, bitrate, height."""
    meta = {
        "duration": None,
        "size_mb": None,
        "ext": None,
        "codecs": None,
        "quality": None,
        "mode": None,
        "title": None,
        "uploader": None,
    }
    if not file_path or not os.path.exists(file_path):
        return meta

    try:
        size_bytes = os.path.getsize(file_path)
        if size_bytes > 0:
            meta["size_mb"] = round(size_bytes / (1024 * 1024), 2)
    except Exception:
        pass

    ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    if ext:
        meta["ext"] = ext

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries",
                "format=duration,size,bit_rate:stream=index,codec_type,codec_name,bit_rate,width,height:format_tags=title,artist,performer,uploader:stream_tags=title,artist,performer,uploader",
                "-of", "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            fmt = data.get("format", {})
            streams = data.get("streams", [])

            dur = fmt.get("duration")
            if dur is None or dur == "N/A":
                for st in streams:
                    d = st.get("duration")
                    if d not in (None, "N/A"):
                        dur = d
                        break
            if dur not in (None, "N/A"):
                try:
                    d_int = int(round(float(dur)))
                    if d_int > 0:
                        meta["duration"] = d_int
                except (ValueError, TypeError):
                    pass

            fmt_tags = fmt.get("tags") or {}
            meta["title"] = fmt_tags.get("title")
            meta["uploader"] = (
                fmt_tags.get("artist")
                or fmt_tags.get("performer")
                or fmt_tags.get("uploader")
            )

            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

            v_codec = None
            if video_streams:
                cname = video_streams[0].get("codec_name")
                if cname:
                    v_codec = _format_codec_name(cname)

            a_codec = None
            if audio_streams:
                cname = audio_streams[0].get("codec_name")
                if cname:
                    a_codec = _format_codec_name(cname)

            inferred_mode = "video" if video_streams else ("audio" if audio_streams else None)
            eff_mode = (mode or inferred_mode or "").lower()
            if eff_mode in ("audio", "video"):
                meta["mode"] = eff_mode

            if eff_mode == "video":
                if v_codec and a_codec:
                    meta["codecs"] = f"{v_codec}+{a_codec}"
                elif v_codec:
                    meta["codecs"] = v_codec
                elif a_codec:
                    meta["codecs"] = a_codec

                if video_streams:
                    h = video_streams[0].get("height")
                    if h:
                        meta["quality"] = f"{h}p"
            else:
                if a_codec:
                    meta["codecs"] = a_codec
                elif v_codec:
                    meta["codecs"] = v_codec

                br = None
                if audio_streams:
                    br = audio_streams[0].get("bit_rate")
                    if not br or br == "N/A":
                        tags = audio_streams[0].get("tags") or {}
                        br = tags.get("BPS") or tags.get("BPS-eng") or tags.get("bps")
                if not br or br == "N/A":
                    br = fmt.get("bit_rate")
                if br and br != "N/A":
                    try:
                        kbps = int(round(float(br) / 1000))
                        if kbps > 0:
                            meta["quality"] = f"{kbps} kbps"
                    except (ValueError, TypeError):
                        pass

    except Exception as e:
        logger.warning(f"[TG_CACHE] probe_file_metadata error for {file_path}: {e}")

    return meta


def format_duration(duration_seconds: Optional[Union[int, float]]) -> str:
    """Format duration in seconds as m:ss (or h:mm:ss if >= 1 hour)."""
    if not duration_seconds or duration_seconds < 0:
        return "0:00"
    m, s = divmod(int(round(float(duration_seconds))), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def build_caption(
    video_id: Optional[str] = None,
    mode: Optional[str] = None,
    file_path: Optional[str] = None,
    size_mb: Optional[Union[float, str]] = None,
    duration: Optional[Union[int, float, str]] = None,
    title: Optional[str] = None,
    *,
    ext: Optional[str] = None,
    uploader: Optional[str] = None,
    codecs: Optional[str] = None,
    quality: Optional[str] = None,
    source: Optional[str] = "YouTube",
    cached_at: Optional[Any] = None,
) -> str:
    """Build Telegram upload caption under 1000 characters.

    Layout:
    🎵 Title: {title}
    📺 Channel: {uploader}
    ⏱ Duration: {m:ss}
    💾 Size: {size_mb} MB
    📁 Format: {ext} · {codecs}
    🎧 Quality: {quality}
    🎼 Type: {audio|video}
    🌐 Source: YouTube
    🆔 ID: {video_id}
    🔗 Link: https://www.youtube.com/watch?v={video_id}
    🕒 Cached: {YYYY-MM-DD HH:MM} UTC

    Each line only if its value is known; never invent a value.
    """
    if file_path and not os.path.exists(file_path) and ("/" not in file_path and "\\" not in file_path):
        if not ext:
            ext = file_path
        file_path = None

    if file_path and os.path.exists(file_path):
        probed = probe_file_metadata(file_path, mode=mode)
        if duration is None:
            duration = probed["duration"]
        if size_mb is None:
            size_mb = probed["size_mb"]
        if ext is None:
            ext = probed["ext"]
        if codecs is None:
            codecs = probed["codecs"]
        if quality is None:
            quality = probed["quality"]
        if mode is None:
            mode = probed["mode"]

        temp_dir = os.path.dirname(file_path)
        if temp_dir and os.path.isdir(temp_dir):
            if not title:
                t_file = os.path.join(temp_dir, ".title")
                if os.path.exists(t_file):
                    try:
                        with open(t_file, "r", encoding="utf-8") as f:
                            c = f.read().strip()
                            if c and c != "NA":
                                title = c.splitlines()[0].strip()
                    except Exception:
                        pass
            if not uploader:
                u_file = os.path.join(temp_dir, ".uploader")
                if os.path.exists(u_file):
                    try:
                        with open(u_file, "r", encoding="utf-8") as f:
                            c = f.read().strip()
                            if c and c != "NA":
                                uploader = c.splitlines()[0].strip()
                    except Exception:
                        pass

        if not title and probed.get("title"):
            title = probed["title"]
        if not uploader and probed.get("uploader"):
            uploader = probed["uploader"]

    clean_title = None
    if title:
        t = " ".join(str(title).split())
        if t and t != "NA":
            clean_title = t

    lines_after_title = []

    if uploader:
        u = " ".join(str(uploader).split())
        if u and u != "NA":
            lines_after_title.append(f"📺 Channel: {u}")

    if duration is not None:
        dur_str = None
        if isinstance(duration, (int, float)):
            if duration > 0:
                dur_str = format_duration(duration)
        elif isinstance(duration, str) and duration.strip():
            dur_str = duration.strip()
        if dur_str:
            lines_after_title.append(f"⏱ Duration: {dur_str}")

    if size_mb is not None:
        s_str = str(size_mb).strip()
        if s_str and s_str != "NA":
            if s_str.endswith("MB"):
                lines_after_title.append(f"💾 Size: {s_str}")
            else:
                lines_after_title.append(f"💾 Size: {s_str} MB")

    format_parts = []
    if ext and str(ext).strip() and str(ext).strip() != "NA":
        format_parts.append(str(ext).strip())
    if codecs and str(codecs).strip() and str(codecs).strip() != "NA":
        format_parts.append(str(codecs).strip())

    if len(format_parts) == 2:
        lines_after_title.append(f"📁 Format: {format_parts[0]} · {format_parts[1]}")
    elif len(format_parts) == 1:
        lines_after_title.append(f"📁 Format: {format_parts[0]}")

    if quality and str(quality).strip() and str(quality).strip() != "NA":
        lines_after_title.append(f"🎧 Quality: {str(quality).strip()}")

    if mode and str(mode).strip().lower() in ("audio", "video"):
        lines_after_title.append(f"🎼 Type: {str(mode).strip().lower()}")

    if source and str(source).strip():
        lines_after_title.append(f"🌐 Source: {str(source).strip()}")

    if video_id and str(video_id).strip() and str(video_id).strip() != "NA":
        lines_after_title.append(f"🆔 ID: {str(video_id).strip()}")

    if video_id and str(video_id).strip() and str(video_id).strip() != "NA":
        lines_after_title.append(f"🔗 Link: https://www.youtube.com/watch?v={str(video_id).strip()}")

    if cached_at:
        if isinstance(cached_at, (datetime.datetime, datetime.date)):
            cached_str = cached_at.strftime("%Y-%m-%d %H:%M") + " UTC"
        else:
            c_s = str(cached_at).strip()
            cached_str = c_s if c_s.endswith("UTC") else f"{c_s} UTC"
        lines_after_title.append(f"🕒 Cached: {cached_str}")

    after_block = "\n".join(lines_after_title)

    if not clean_title:
        return after_block

    title_prefix = "🎵 Title: "
    max_caption_len = 999  # under 1000 characters

    if after_block:
        overhead = len(title_prefix) + 1 + len(after_block)
        max_title_len = max_caption_len - overhead
        if max_title_len <= 0:
            return after_block
        if len(clean_title) > max_title_len:
            clean_title = clean_title[:max_title_len]
        return f"{title_prefix}{clean_title}\n{after_block}"
    else:
        overhead = len(title_prefix)
        max_title_len = max_caption_len - overhead
        if max_title_len <= 0:
            return ""
        if len(clean_title) > max_title_len:
            clean_title = clean_title[:max_title_len]
        return f"{title_prefix}{clean_title}"


def parse_caption(text: Optional[str]) -> Optional[tuple[str, str]]:
    """Parse cached Telegram upload caption into (video_id, mode).

    Supports:
    1. New layout:
       🆔 ID: {video_id}
       🎼 Type: {audio|video}
       (tolerant of emoji prefix and extra spaces)
    2. Previous format:
       yt:{video_id}:{mode}
    3. Oldest format:
       Plain YouTube URL only -> returns None.
    """
    if not text or not isinstance(text, str):
        return None

    # Previous format: "yt:<id>:<mode>"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = re.match(r"^yt:([^:\s]+):(audio|video)$", line, re.IGNORECASE)
        if m:
            return m.group(1), m.group(2).lower()

    # New layout:
    # 🆔 ID: {video_id}
    # 🎼 Type: {mode}
    video_id = None
    mode = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        m_id = re.match(r"^(?:[^\w\s]*\s*)?ID\s*:\s*([^\s]+)$", line, re.IGNORECASE)
        if m_id:
            video_id = m_id.group(1).strip()

        m_type = re.match(r"^(?:[^\w\s]*\s*)?Type\s*:\s*(audio|video)$", line, re.IGNORECASE)
        if m_type:
            mode = m_type.group(1).lower().strip()

    if video_id and mode:
        return video_id, mode

    return None


async def _probe_media_metadata(file_path: str) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Extract duration (int), title (str), and performer (str) via ffprobe (local, fast)."""
    duration: Optional[int] = None
    title: Optional[str] = None
    performer: Optional[str] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=duration:format_tags=title,artist,performer,uploader:stream_tags=title,artist,performer,uploader",
            "-of", "json",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            import json
            data = json.loads(stdout.decode())
            fmt = data.get("format", {})
            dur = fmt.get("duration")
            if dur is None or dur == "N/A":
                for st in data.get("streams", []):
                    if st.get("duration") is not None and st.get("duration") != "N/A":
                        dur = st.get("duration")
                        break
            if dur is not None and dur != "N/A":
                try:
                    duration = int(round(float(dur)))
                except (ValueError, TypeError):
                    pass
            fmt_tags = fmt.get("tags") or {}
            title = fmt_tags.get("title")
            performer = fmt_tags.get("artist") or fmt_tags.get("performer") or fmt_tags.get("uploader")
    except Exception as e:
        logger.warning(f"[TG_CACHE] ffprobe metadata extraction failed for {file_path}: {e}")
    return duration, title, performer


# ── Background Worker with Dedup Lock ─────────────────────────────────────────

async def cache_in_background(video_id: str, mode: str, url: str) -> None:
    """Fire-and-forget worker to download media and upload it to Telegram cache channel.

    1. Checks if already cached in MongoDB.
    2. Acquires Redis deduplication lock via SET NX EX to avoid duplicate downloads.
    3. Acquires upload_slot semaphore to bound concurrent downloads/uploads.
    4. Downloads full file via download_media.
    5. Uploads to CACHE_CHANNEL_ID via Telegram client.
    6. Upserts cache record into MongoDB.
    7. Cleans up temp file regardless of outcome.
    8. Releases Redis lock.
    """
    # Fire-and-forget safety: catch all exceptions so caller is never blocked or faulted
    try:
        # Check if already present in Mongo before acquiring lock
        existing = await get_cached_entry(video_id, mode)
        if existing:
            logger.info(f"[TG_CACHE] {video_id}:{mode} already cached in Mongo, skipping")
            return

        redis = await get_async_redis()
        lock_key = f"caching_lock:{video_id}:{mode}"
        lock_ttl = 600  # 10 minutes TTL safety net

        # 1. Acquire Redis lock
        acquired = await redis.set(lock_key, "1", nx=True, ex=lock_ttl)
        if not acquired:
            logger.info(f"[TG_CACHE] Lock already held for {video_id}:{mode}, skipping background cache")
            return

        file_path: Optional[str] = None
        temp_dir: Optional[str] = None

        try:
            # 2. Concurrency limiting
            async with upload_slot:
                logger.info(f"[TG_CACHE] Starting background cache for {video_id} ({mode})")

                # 3. Download the full file
                file_path = await download_media(url, mode=mode)
                if not file_path or not os.path.exists(file_path):
                    logger.error(f"[TG_CACHE] Download failed for {video_id} ({mode})")
                    return
                temp_dir = os.path.dirname(file_path)

                channel_id = config.CACHE_CHANNEL_ID
                if not channel_id:
                    logger.warning(f"[TG_CACHE] CACHE_CHANNEL_ID is not configured; skipping Telegram upload")
                    return

                # 4. Upload to Telegram
                tg_client = await get_telegram_client()
                if not tg_client:
                    logger.error(f"[TG_CACHE] Telegram client unavailable; aborting upload for {video_id}")
                    return

                # 4. Extract metadata & build caption
                duration, ff_title, performer = await _probe_media_metadata(file_path)
                title: Optional[str] = None
                uploader: Optional[str] = None
                if temp_dir and os.path.isdir(temp_dir):
                    title_file = os.path.join(temp_dir, ".title")
                    if os.path.exists(title_file):
                        try:
                            with open(title_file, "r", encoding="utf-8") as f:
                                content = f.read().strip()
                                if content and content != "NA":
                                    title = content.splitlines()[0].strip()
                        except Exception as e:
                            logger.warning(f"[TG_CACHE] Failed to read .title file: {e}")

                    uploader_file = os.path.join(temp_dir, ".uploader")
                    if os.path.exists(uploader_file):
                        try:
                            with open(uploader_file, "r", encoding="utf-8") as f:
                                content = f.read().strip()
                                if content and content != "NA":
                                    uploader = content.splitlines()[0].strip()
                        except Exception as e:
                            logger.warning(f"[TG_CACHE] Failed to read .uploader file: {e}")

                    if not title or not uploader:
                        try:
                            for f in os.listdir(temp_dir):
                                if f.endswith(".info.json"):
                                    with open(os.path.join(temp_dir, f), "r", encoding="utf-8") as jf:
                                        info = json.load(jf)
                                        if not title:
                                            t = info.get("title")
                                            if t and str(t).strip() and str(t).strip() != "NA":
                                                title = str(t).strip()
                                        if not uploader:
                                            u = info.get("uploader") or info.get("channel")
                                            if u and str(u).strip() and str(u).strip() != "NA":
                                                uploader = str(u).strip()
                                        if title and uploader:
                                            break
                        except Exception:
                            pass

                if not title and ff_title and ff_title.strip():
                    title = ff_title.strip()
                if not uploader and performer and performer.strip():
                    uploader = performer.strip()

                file_size = os.path.getsize(file_path)
                now = datetime.datetime.now(datetime.timezone.utc)
                caption = build_caption(
                    video_id=video_id,
                    mode=mode,
                    file_path=file_path,
                    title=title,
                    uploader=uploader,
                    cached_at=now,
                )
                msg = None

                if mode == "audio":
                    audio_kwargs = {
                        "chat_id": channel_id,
                        "audio": file_path,
                        "caption": caption,
                    }
                    if duration is not None:
                        audio_kwargs["duration"] = int(duration)
                    if title:
                        audio_kwargs["title"] = str(title)
                    if performer or uploader:
                        audio_kwargs["performer"] = str(performer or uploader)
                    try:
                        msg = await tg_client.send_audio(**audio_kwargs)
                    except Exception as e:
                        logger.warning(f"[TG_CACHE] send_audio failed, trying send_document: {e}")
                        msg = await tg_client.send_document(
                            chat_id=channel_id,
                            document=file_path,
                            caption=caption,
                        )
                else:
                    video_kwargs = {
                        "chat_id": channel_id,
                        "video": file_path,
                        "caption": caption,
                        "supports_streaming": True,
                    }
                    if duration is not None:
                        video_kwargs["duration"] = int(duration)
                    try:
                        msg = await tg_client.send_video(**video_kwargs)
                    except Exception as e:
                        logger.warning(f"[TG_CACHE] send_video failed, trying send_document: {e}")
                        msg = await tg_client.send_document(
                            chat_id=channel_id,
                            document=file_path,
                            caption=caption,
                        )

                if msg:
                    # 5. Save to MongoDB
                    chat_id = msg.chat.id
                    message_id = msg.id
                    media = getattr(msg, "audio", None) or getattr(msg, "video", None) or getattr(msg, "document", None)
                    msg_file_size = getattr(media, "file_size", None) or file_size
                    msg_mime_type = getattr(media, "mime_type", None)
                    msg_duration = getattr(media, "duration", None) or duration
                    await save_cached_entry(
                        video_id=video_id,
                        mode=mode,
                        chat_id=chat_id,
                        message_id=message_id,
                        file_size=msg_file_size,
                        mime_type=msg_mime_type,
                        duration=msg_duration,
                    )
                    logger.info(
                        f"[TG_CACHE] ✅ Successfully cached {video_id}:{mode} "
                        f"to chat={chat_id} msg={message_id}"
                    )
                else:
                    logger.error(f"[TG_CACHE] Upload produced no message object for {video_id}")

        finally:
            # 6. Always clean up temporary files/directory
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            elif file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass

            # 7. Release Redis lock
            try:
                await redis.delete(lock_key)
            except Exception as e:
                logger.debug(f"[TG_CACHE] Failed to delete Redis lock {lock_key}: {e}")

    except Exception as e:
        logger.error(f"[TG_CACHE] Unexpected error in cache_in_background for {video_id}:{mode}: {e}")
