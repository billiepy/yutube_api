"""Live end-to-end verification script for GET /youtube/v2/download,

GET /youtube/jobStatus, and GET /stream/telegram/{chat_id}/{message_id}.
"""
import asyncio
import time
import httpx
from main import app
from tools import get_async_mongo_db
from utils.telegram_cache import close_telegram_client

TEST_VID = "M7lc1UVf-VE"
TEST_MODE = "audio"
TEST_IS_VIDEO = "false"


async def run_live_verification():
    print("=" * 70)
    print("  STARTING LIVE END-TO-END VERIFICATION")
    print("=" * 70)

    # 0. Ensure video is not cached in MongoDB beforehand
    db = await get_async_mongo_db()
    await db["telegram_cache"].delete_one({"video_id": TEST_VID, "mode": TEST_MODE})
    print(f"[*] Cleared cache for {TEST_VID}:{TEST_MODE} in MongoDB.")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Test /youtube/jobStatus compatibility
        print("\n[+] Testing GET /youtube/jobStatus...")
        r_job = await client.get("/youtube/jobStatus?api_key=test_compat_key&job_id=non_existent_123")
        assert r_job.status_code == 200
        assert r_job.json() == {"status": "error", "error": "job not found"}
        print("    -> Job status responded correctly:", r_job.json())

        # 2. Test cache-miss on /youtube/v2/download
        print(f"\n[+] Testing GET /youtube/v2/download (Cache Miss) for {TEST_VID}...")
        t0 = time.perf_counter()
        r_miss = await client.get(
            f"/youtube/v2/download?query={TEST_VID}&isVideo={TEST_IS_VIDEO}&api_key=compat_test_key"
        )
        miss_latency_ms = (time.perf_counter() - t0) * 1000

        print(f"    -> Response status: {r_miss.status_code}")
        print(f"    -> Latency: {miss_latency_ms:.2f} ms")
        data_miss = r_miss.json()
        print(f"    -> Response JSON: {data_miss}")

        assert r_miss.status_code == 200
        assert data_miss["status"] == "success"
        assert data_miss["job_id"] is None
        res_miss = data_miss["result"]
        assert res_miss["success"] is True
        assert res_miss["cached"] is False
        assert res_miss["source"] == "ytdlp"
        assert "/stream/resolver/" in res_miss["cdn"]
        assert res_miss["is_audio"] is True
        assert res_miss["is_video"] is False
        print("    -> Cache miss response shape verified perfectly! Background cache task spawned.")

        # 3. Wait for background task to populate MongoDB
        print("\n[+] Waiting for background cache worker to download, upload to Telegram, and index in Mongo...")
        max_wait_seconds = 60
        entry = None
        for i in range(max_wait_seconds):
            await asyncio.sleep(1)
            entry = await db["telegram_cache"].find_one({"video_id": TEST_VID, "mode": TEST_MODE})
            if entry and entry.get("telegram_chat_id") and entry.get("telegram_message_id"):
                print(f"    -> Found cached entry in MongoDB after {i+1} seconds!")
                print(f"       Chat ID: {entry['telegram_chat_id']}, Message ID: {entry['telegram_message_id']}")
                break
            if (i + 1) % 5 == 0:
                print(f"       Still waiting... ({i+1}s elapsed)")

        assert entry is not None, f"Background caching did not complete within {max_wait_seconds}s"

        # 4. Test cache-hit on /youtube/v2/download
        print(f"\n[+] Testing GET /youtube/v2/download (Cache Hit) for {TEST_VID}...")
        t1 = time.perf_counter()
        r_hit = await client.get(
            f"/youtube/v2/download?query={TEST_VID}&isVideo={TEST_IS_VIDEO}&api_key=compat_test_key"
        )
        hit_latency_ms = (time.perf_counter() - t1) * 1000

        print(f"    -> Response status: {r_hit.status_code}")
        print(f"    -> Latency: {hit_latency_ms:.2f} ms")
        data_hit = r_hit.json()
        print(f"    -> Response JSON: {data_hit}")

        assert r_hit.status_code == 200
        assert data_hit["status"] == "success"
        assert data_hit["job_id"] is None
        res_hit = data_hit["result"]
        assert res_hit["success"] is True
        assert res_hit["cached"] is True
        assert res_hit["source"] == "telegram"
        expected_suffix = f"/stream/telegram/{entry['telegram_chat_id']}/{entry['telegram_message_id']}"
        assert res_hit["cdn"].endswith(expected_suffix)
        assert res_hit["is_audio"] is True
        assert res_hit["is_video"] is False
        print("    -> Cache hit response shape verified perfectly!")

        # 5. Fetch stream from Telegram route and verify real bytes returned
        print(f"\n[+] Fetching audio bytes from Telegram stream route {expected_suffix}...")
        r_stream = await client.get(expected_suffix)
        print(f"    -> Stream response status: {r_stream.status_code}")
        print(f"    -> Content-Type: {r_stream.headers.get('content-type')}")
        print(f"    -> Content-Length: {r_stream.headers.get('content-length')}")
        print(f"    -> Bytes received: {len(r_stream.content)}")

        assert r_stream.status_code == 200
        assert len(r_stream.content) > 1000
        assert int(r_stream.headers.get("content-length", 0)) == len(r_stream.content)
        print("    -> Real audio bytes successfully received from Telegram message!")

        # Range request check
        r_range = await client.get(expected_suffix, headers={"range": "bytes=0-1023"})
        assert r_range.status_code == 206
        assert len(r_range.content) == 1024
        print("    -> Range request (206 Partial Content) verified successfully!")

    await close_telegram_client()

    print("\n" + "=" * 70)
    print("  SUMMARY OF MEASURED RESPONSE TIMES")
    print("=" * 70)
    print(f"  Cache Miss Latency: {miss_latency_ms:.2f} ms")
    print(f"  Cache Hit Latency:  {hit_latency_ms:.2f} ms")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_live_verification())
