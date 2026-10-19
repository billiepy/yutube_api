"""Unit and regression tests for new endpoints:
- GET /youtube/v2/download
- GET /youtube/jobStatus
- GET /stream/telegram/{chat_id}/{message_id}
"""
import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from main import app
from utils.telegram_cache import set_telegram_client


class MockTgMedia:
    def __init__(self, file_size=1024, mime_type="audio/mpeg", file_name="test.mp3"):
        self.file_size = file_size
        self.mime_type = mime_type
        self.file_name = file_name


class MockTgMessage:
    def __init__(self, chat_id, message_id, media=None):
        self.id = message_id
        self.chat = MagicMock(id=chat_id)
        self.audio = media
        self.video = None
        self.document = None
        self.empty = False


class MockTgClient:
    def __init__(self):
        self.is_connected = True
        self.messages = {}

    async def get_messages(self, chat_id, message_ids):
        msg = self.messages.get((int(chat_id), int(message_ids)))
        if msg is None:
            m = MagicMock()
            m.empty = True
            return m
        return msg

    async def stream_media(self, message, limit=0, offset=0) -> AsyncIterator[bytes]:
        # Yield mock 512-byte chunks
        data = b"X" * 1024
        chunk_size = 512
        for i in range(offset * chunk_size, len(data), chunk_size):
            yield data[i:i + chunk_size]


@pytest.fixture
def mock_tg():
    client = MockTgClient()
    set_telegram_client(client)
    yield client
    set_telegram_client(None)


@pytest.mark.asyncio
async def test_job_status():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Without params
        r1 = await client.get("/youtube/jobStatus")
        assert r1.status_code == 200
        assert r1.json() == {"status": "error", "error": "job not found"}

        # With api_key and job_id
        r2 = await client.get("/youtube/jobStatus?api_key=test_key&job_id=job_123")
        assert r2.status_code == 200
        assert r2.json() == {"status": "error", "error": "job not found"}


@pytest.mark.asyncio
async def test_v2_download_invalid_query():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/youtube/v2/download?query=invalid!!notaid&isVideo=false")
        assert r.status_code == 400
        data = r.json()
        assert data["status"] == "error"


@pytest.mark.asyncio
async def test_v2_download_cache_miss():
    vid = "dQw4w9WgXcQ"
    mock_bg = MagicMock(side_effect=lambda *args, **kwargs: asyncio.sleep(0))
    with patch("utils.telegram_cache.get_cached_entry", new_callable=AsyncMock) as mock_get, \
         patch("utils.telegram_cache.cache_in_background", new=mock_bg), \
         patch("main._ensure_stream_job", new_callable=AsyncMock) as mock_ensure:
        mock_get.return_value = None
        mock_ensure.return_value = "mock_stream_123"

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(f"/youtube/v2/download?query={vid}&isVideo=false&api_key=some_key")
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "success"
            assert data["job_id"] is None
            result = data["result"]
            assert result["success"] is True
            assert result["cached"] is False
            assert result["source"] == "ytdlp"
            assert "mock_stream_123" in result["cdn"]
            assert result["is_audio"] is True
            assert result["is_video"] is False

            # Verify background caching was called
            await asyncio.sleep(0.05)
            mock_bg.assert_called_once()
            call_args = mock_bg.call_args[0]
            assert call_args[0] == vid
            assert call_args[1] == "audio"
            assert "dQw4w9WgXcQ" in call_args[2]


@pytest.mark.asyncio
async def test_v2_download_cache_hit():
    vid = "dQw4w9WgXcQ"
    cached_doc = {
        "video_id": vid,
        "mode": "video",
        "telegram_chat_id": -1001234567890,
        "telegram_message_id": 999,
    }
    mock_bg = MagicMock(side_effect=lambda *args, **kwargs: asyncio.sleep(0))
    with patch("utils.telegram_cache.get_cached_entry", new_callable=AsyncMock) as mock_get, \
         patch("utils.telegram_cache.cache_in_background", new=mock_bg):
        mock_get.return_value = cached_doc

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(f"/youtube/v2/download?query=https://youtu.be/{vid}&isVideo=true")
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "success"
            assert data["job_id"] is None
            result = data["result"]
            assert result["success"] is True
            assert result["cached"] is True
            assert result["source"] == "telegram"
            assert result["cdn"].endswith("/stream/telegram/-1001234567890/999")
            assert result["is_audio"] is False
            assert result["is_video"] is True

            # Background caching should NOT be triggered on cache hit
            await asyncio.sleep(0.01)
            mock_bg.assert_not_called()


@pytest.mark.asyncio
async def test_stream_telegram_route(mock_tg):
    chat_id = -1001234567890
    msg_id = 999
    media = MockTgMedia(file_size=1024, mime_type="audio/mpeg", file_name="song.mp3")
    msg = MockTgMessage(chat_id, msg_id, media=media)
    mock_tg.messages[(chat_id, msg_id)] = msg

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Full content request
        r = await client.get(f"/stream/telegram/{chat_id}/{msg_id}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/mpeg"
        assert r.headers["content-length"] == "1024"
        assert len(r.content) == 1024
        assert r.content == b"X" * 1024

        # Range request
        r_range = await client.get(
            f"/stream/telegram/{chat_id}/{msg_id}",
            headers={"range": "bytes=0-100"},
        )
        assert r_range.status_code == 206
        assert r_range.headers["content-range"] == "bytes 0-100/1024"
        assert r_range.headers["content-length"] == "101"
        assert len(r_range.content) == 101

        # Non-existent message
        r_404 = await client.get(f"/stream/telegram/{chat_id}/999999")
        assert r_404.status_code == 404


@pytest.mark.asyncio
async def test_v2_download_cdn_url_schemes():
    vid = "dQw4w9WgXcQ"
    mock_bg = MagicMock(side_effect=lambda *args, **kwargs: asyncio.sleep(0))
    with patch("utils.telegram_cache.get_cached_entry", new_callable=AsyncMock) as mock_get, \
         patch("utils.telegram_cache.cache_in_background", new=mock_bg), \
         patch("main._ensure_stream_job", new_callable=AsyncMock) as mock_ensure:
        mock_get.return_value = None
        mock_ensure.return_value = "mock_stream_123"

        # 1. Plain HTTP request -> CDN URL must use http://
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as client:
            r = await client.get(f"/youtube/v2/download?query={vid}&isVideo=false")
            assert r.status_code == 200
            cdn = r.json()["result"]["cdn"]
            assert cdn.startswith("http://localhost:8000/stream/resolver/")

        # 2. Behind TLS reverse proxy (X-Forwarded-Proto: https) -> CDN URL must use https://
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as client:
            r = await client.get(
                f"/youtube/v2/download?query={vid}&isVideo=false",
                headers={"x-forwarded-proto": "https", "x-forwarded-host": "api.nubcoders.com"},
            )
            assert r.status_code == 200
            cdn = r.json()["result"]["cdn"]
            assert cdn.startswith("https://api.nubcoders.com/stream/resolver/")

        # 3. Explicit BASE_URL override in config -> CDN URL uses BASE_URL
        with patch("config.BASE_URL", "https://custom-domain.com"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as client:
                r = await client.get(f"/youtube/v2/download?query={vid}&isVideo=false")
                assert r.status_code == 200
                cdn = r.json()["result"]["cdn"]
                assert cdn.startswith("https://custom-domain.com/stream/resolver/")
