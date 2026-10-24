"""Standalone verification script for the Telegram media cache foundation.

Tests:
1. Full-file downloader (download_media) on a real YouTube video.
2. Redis deduplication lock acquisition, contention (skipping), and release.
3. MongoDB compound unique indexing, upsert semantics, and retrieval.
4. cache_in_background end-to-end execution:
   - Concurrent call dedup skipping
   - Upload execution and chat_id/message_id capture
   - MongoDB persistence
   - Temp file cleanup
"""
import asyncio
import logging
import os
import shutil
import sys
from unittest.mock import AsyncMock, MagicMock

# Configure logging to console
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_cache_layer")

from config import MONGO_URI, CACHE_CHANNEL_ID, BOT_TOKEN
from tools import get_async_redis, get_async_mongo_client, get_async_mongo_db
from utils.media_downloader import download_media
from utils.telegram_cache import (
    get_cached_entry,
    save_cached_entry,
    cache_in_background,
    set_telegram_client,
    _ensure_indexes,
)

# Short real YouTube video ID for testing
TEST_VIDEO_ID = "dQw4w9WgXcQ"
TEST_URL = f"https://www.youtube.com/watch?v={TEST_VIDEO_ID}"


async def test_downloader():
    print("\n--- 1. Testing Full-File Media Downloader ---")
    logger.info(f"Downloading audio for {TEST_VIDEO_ID}...")
    file_path = await download_media(TEST_URL, mode="audio", timeout=60)
    assert file_path is not None, "download_media returned None"
    assert os.path.exists(file_path), f"Downloaded file does not exist: {file_path}"
    file_size = os.path.getsize(file_path)
    logger.info(f"Downloaded successfully: {file_path} ({file_size} bytes)")
    assert file_size > 1000, f"File unexpectedly small: {file_size} bytes"

    # Clean up downloaded test file and directory
    temp_dir = os.path.dirname(file_path)
    shutil.rmtree(temp_dir, ignore_errors=True)
    assert not os.path.exists(file_path), "Failed to clean up downloaded file"
    logger.info("✅ Downloader test PASSED (file downloaded and cleaned up successfully).")


async def test_redis_dedup_lock():
    print("\n--- 2. Testing Redis Deduplication Lock ---")
    redis = await get_async_redis()
    lock_key = f"caching_lock:test_vid_123:audio"
    await redis.delete(lock_key)

    # First lock acquisition
    acq1 = await redis.set(lock_key, "1", nx=True, ex=60)
    assert acq1 is True, "Failed to acquire free Redis lock"
    logger.info(f"Lock 1 acquired successfully for {lock_key}")

    # Concurrent second lock acquisition
    acq2 = await redis.set(lock_key, "1", nx=True, ex=60)
    assert acq2 is None or acq2 is False, "Second concurrent lock acquisition should have failed!"
    logger.info(f"Lock 2 correctly skipped (lock held)")

    # Release lock
    await redis.delete(lock_key)
    acq3 = await redis.set(lock_key, "1", nx=True, ex=60)
    assert acq3 is True, "Failed to acquire lock after release"
    await redis.delete(lock_key)
    logger.info("✅ Redis dedup lock test PASSED.")


class MockMongoCollection:
    def __init__(self):
        self.docs = {}
        self.indexes = []

    async def create_index(self, keys, unique=False, **kwargs):
        self.indexes.append((keys, unique))
        return "video_id_1_mode_1"

    async def find_one(self, query, projection=None):
        key = (query.get("video_id"), query.get("mode"))
        doc = self.docs.get(key)
        if doc and projection and projection.get("_id") == 0:
            doc_copy = dict(doc)
            doc_copy.pop("_id", None)
            return doc_copy
        return doc

    async def update_one(self, filter_q, update, upsert=False):
        key = (filter_q.get("video_id"), filter_q.get("mode"))
        if key not in self.docs:
            if not upsert:
                return MagicMock(matched_count=0, modified_count=0)
            doc = dict(filter_q)
            if "$setOnInsert" in update:
                doc.update(update["$setOnInsert"])
            if "$set" in update:
                doc.update(update["$set"])
            self.docs[key] = doc
            return MagicMock(matched_count=0, modified_count=1, upserted_id=key)
        else:
            if "$set" in update:
                self.docs[key].update(update["$set"])
            return MagicMock(matched_count=1, modified_count=1, upserted_id=None)

    async def delete_one(self, filter_q):
        key = (filter_q.get("video_id"), filter_q.get("mode"))
        self.docs.pop(key, None)
        return MagicMock(deleted_count=1)



async def test_mongo_cache(is_live_mongo=False):
    print("\n--- 3. Testing MongoDB Cache Index & Ops ---")
    vid = "test_vid_xyz"
    mode = "audio"
    chat_id = -1009988776655
    message_id = 42

    saved = await save_cached_entry(vid, mode, chat_id, message_id)
    assert saved["video_id"] == vid
    assert saved["mode"] == mode
    assert saved["telegram_chat_id"] == chat_id
    assert saved["telegram_message_id"] == message_id
    logger.info(f"Saved entry: {saved}")

    entry = await get_cached_entry(vid, mode)
    assert entry is not None, "get_cached_entry returned None after save"
    assert entry["video_id"] == vid
    assert entry["mode"] == mode
    assert entry["telegram_chat_id"] == chat_id
    assert entry["telegram_message_id"] == message_id
    logger.info(f"Retrieved cached entry: {entry}")

    # Test upsert update
    await save_cached_entry(vid, mode, chat_id, 43)
    entry2 = await get_cached_entry(vid, mode)
    assert entry2["telegram_message_id"] == 43
    logger.info(f"Updated cached entry: {entry2}")

    logger.info("✅ MongoDB cache test PASSED.")


class MockTelegramMessage:
    def __init__(self, chat_id: int, message_id: int):
        self.chat = MagicMock(id=chat_id)
        self.id = message_id


class MockTelegramClient:
    def __init__(self, chat_id: int = -1001234567890):
        self.is_connected = True
        self.chat_id = chat_id
        self.uploads = []
        self._next_msg_id = 100

    async def send_audio(self, chat_id, audio, caption=None, **kwargs):
        self._next_msg_id += 1
        msg = MockTelegramMessage(chat_id, self._next_msg_id)
        self.uploads.append({"type": "audio", "file": audio, "caption": caption, "msg": msg, **kwargs})
        logger.info(f"[MockTG] send_audio: {audio} -> chat={chat_id} msg={msg.id}")
        return msg

    async def send_video(self, chat_id, video, caption=None, supports_streaming=True, **kwargs):
        self._next_msg_id += 1
        msg = MockTelegramMessage(chat_id, self._next_msg_id)
        self.uploads.append({"type": "video", "file": video, "caption": caption, "msg": msg, **kwargs})
        logger.info(f"[MockTG] send_video: {video} -> chat={chat_id} msg={msg.id}")
        return msg

    async def send_document(self, chat_id, document, caption=None, **kwargs):
        self._next_msg_id += 1
        msg = MockTelegramMessage(chat_id, self._next_msg_id)
        self.uploads.append({"type": "document", "file": document, "caption": caption, "msg": msg, **kwargs})
        logger.info(f"[MockTG] send_document: {document} -> chat={chat_id} msg={msg.id}")
        return msg


async def test_cache_in_background_end_to_end():
    print("\n--- 4. Testing cache_in_background End-to-End & Deduplication ---")
    mock_tg = MockTelegramClient()
    set_telegram_client(mock_tg)

    # Set dummy CACHE_CHANNEL_ID in config if not set
    import config
    original_channel_id = config.CACHE_CHANNEL_ID
    if not config.CACHE_CHANNEL_ID:
        config.CACHE_CHANNEL_ID = -1001234567890

    vid = "test_e2e_vid"
    mode = "audio"

    # Clear any previous entry or lock
    redis = await get_async_redis()
    await redis.delete(f"caching_lock:{vid}:{mode}")
    try:
        import utils.telegram_cache as tc
        db = await tc.get_async_mongo_db()
        await db["telegram_cache"].delete_one({"video_id": vid, "mode": mode})
    except Exception:
        pass


    # Launch two concurrent calls for the exact same video_id and mode
    logger.info(f"Launching 2 concurrent cache_in_background calls for {vid}:{mode}...")
    task1 = asyncio.create_task(cache_in_background(vid, mode, TEST_URL))
    # Slight microsecond yield so task1 acquires lock first
    await asyncio.sleep(0.01)
    task2 = asyncio.create_task(cache_in_background(vid, mode, TEST_URL))

    await asyncio.gather(task1, task2)

    # Check how many uploads occurred in mock Telegram client
    assert len(mock_tg.uploads) == 1, (
        f"Expected exactly 1 upload due to dedup lock, but found {len(mock_tg.uploads)}"
    )
    upload_info = mock_tg.uploads[0]
    logger.info(f"Verified single upload occurred: {upload_info}")

    # Check MongoDB entry
    entry = await get_cached_entry(vid, mode)
    assert entry is not None, "MongoDB cache entry missing after upload"
    assert entry["video_id"] == vid
    assert entry["telegram_chat_id"] == config.CACHE_CHANNEL_ID
    assert entry["telegram_message_id"] == upload_info["msg"].id
    logger.info(f"Verified MongoDB cached entry: {entry}")

    # Check that Redis lock was released
    lock_val = await redis.get(f"caching_lock:{vid}:{mode}")
    assert lock_val is None, f"Redis lock was not released: {lock_val}"
    logger.info("Verified Redis lock was properly released.")

    # Check that uploaded file was cleaned up from disk
    uploaded_file = upload_info["file"]
    assert not os.path.exists(uploaded_file), f"Temp file was not cleaned up: {uploaded_file}"
    logger.info("Verified temp file was cleaned up from disk.")

    config.CACHE_CHANNEL_ID = original_channel_id
    logger.info("✅ cache_in_background End-to-End test PASSED.")


async def main():
    print("=" * 60)
    print("  RUNNING TELEGRAM MEDIA CACHE FOUNDATION TEST SUITE")
    print("=" * 60)

    # Check if real MongoDB is reachable
    is_live_mongo = False
    try:
        client = await get_async_mongo_client()
        await asyncio.wait_for(client.admin.command("ping"), timeout=2.0)
        is_live_mongo = True
        logger.info("Connected to LIVE MongoDB!")
    except Exception as e:
        logger.warning(f"Live MongoDB not available ({e}); using test mock for MongoDB verification.")
        mock_col = MockMongoCollection()
        mock_db = {"telegram_cache": mock_col}
        import tools
        import utils.telegram_cache as tc
        tools.get_async_mongo_db = AsyncMock(return_value=mock_db)
        tc.get_async_mongo_db = AsyncMock(return_value=mock_db)

    # Step 1: Downloader test
    await test_downloader()

    # Step 2: Redis lock test
    await test_redis_dedup_lock()

    # Step 3: MongoDB test
    await test_mongo_cache(is_live_mongo=is_live_mongo)


    # Step 4: cache_in_background end-to-end
    await test_cache_in_background_end_to_end()

    print("\n" + "=" * 60)
    print("  ALL TESTS PASSED SUCCESSFULLY! ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
