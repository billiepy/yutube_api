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
from typing import Optional

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

def format_duration(duration_seconds: Optional[int]) -> str:
    """Format duration in seconds as m:ss (or h:mm:ss if >= 1 hour)."""
    if not duration_seconds or duration_seconds < 0:
        return "0:00"
    m, s = divmod(int(duration_seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def build_caption(
    video_id: str,
    mode: str,
    ext: str,
    size_mb: float,
    duration: Optional[int],
    title: Optional[str] = None,
) -> str:
    """Build Telegram upload caption under 1024 characters.

    Line 1: video title (omitted if no title)
    Line 2: yt:{video_id}:{mode}
    Line 3: {ext} | {size_mb}MB | {m:ss}
    Line 4: https://www.youtube.com/watch?v={video_id}
    """
    dur_str = format_duration(duration)
    metadata_lines = [
        f"yt:{video_id}:{mode}",
        f"{ext} | {size_mb}MB | {dur_str}",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    metadata_block = "\n".join(metadata_lines)

    if not title:
        return metadata_block

    clean_title = " ".join(str(title).split())
    if not clean_title:
        return metadata_block

    # Telegram caption max length is 1024 characters
    # Max title length is 1024 - len(metadata_block) - 1 (for the newline)
    max_title_len = 1024 - len(metadata_block) - 1
    if max_title_len <= 0:
        return metadata_block

    if len(clean_title) > max_title_len:
        clean_title = clean_title[:max_title_len]

    return f"{clean_title}\n{metadata_block}"


def parse_caption(text: Optional[str]) -> Optional[tuple[str, str]]:
    """Parse cached Telegram upload caption into (video_id, mode).

    Finds a line matching 'yt:<video_id>:<mode>' where mode is 'audio' or 'video'.
    Returns (video_id, mode) or None.
    """
    if not text or not isinstance(text, str):
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = re.match(r"^yt:([^:\s]+):(audio|video)$", line, re.IGNORECASE)
        if m:
            return m.group(1), m.group(2).lower()
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
                if temp_dir and os.path.isdir(temp_dir):
                    title_file = os.path.join(temp_dir, ".title")
                    if os.path.exists(title_file):
                        try:
                            with open(title_file, "r", encoding="utf-8") as f:
                                content = f.read().strip()
                                if content:
                                    title = content.splitlines()[0].strip()
                        except Exception as e:
                            logger.warning(f"[TG_CACHE] Failed to read .title file: {e}")
                    if not title:
                        try:
                            for f in os.listdir(temp_dir):
                                if f.endswith(".info.json"):
                                    with open(os.path.join(temp_dir, f), "r", encoding="utf-8") as jf:
                                        info = json.load(jf)
                                        t = info.get("title")
                                        if t and str(t).strip():
                                            title = str(t).strip()
                                            break
                        except Exception:
                            pass

                if not title and ff_title and ff_title.strip():
                    title = ff_title.strip()

                file_size = os.path.getsize(file_path)
                ext = os.path.splitext(file_path)[1].lstrip(".").lower() or ("m4a" if mode == "audio" else "mp4")
                size_mb = round(file_size / (1024 * 1024), 2)
                caption = build_caption(
                    video_id=video_id,
                    mode=mode,
                    ext=ext,
                    size_mb=size_mb,
                    duration=duration,
                    title=title,
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
                    if performer:
                        audio_kwargs["performer"] = str(performer)
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
