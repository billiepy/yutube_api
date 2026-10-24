"""Full-file media downloader using yt-dlp.

Downloads YouTube audio or video to a local temp file, adhering to the
shared concurrency limiter (subprocess_slot) and the anonymous-then-cookies
retry convention.
"""
import asyncio
import logging
import os
import shutil
import tempfile
import time
from typing import Optional

from utils.cookies import cookie_args
from utils.subprocess_limit import subprocess_slot
from utils.ytdlp_runner import _COOKIE_CLIENTS

logger = logging.getLogger("ytube_api.downloader")

# Format selectors matching cache_manager.py and media_extractor.py
_AUDIO_SELECTOR = "140/bestaudio[ext=m4a]/251/250/bestaudio"
_VIDEO_SELECTOR = "22/18/best[ext=mp4]"


async def _run_download(
    url: str,
    selector: str,
    extra: list[str],
    temp_dir: str,
    timeout: int,
    tag: str,
) -> Optional[str]:
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        *extra,
        "-f", selector,
        "--no-playlist",
        "-o", output_template,
        url,
    ]
    start = time.time()
    process = None
    try:
        async with subprocess_slot:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"[{tag}] TIMEOUT after {timeout}s")
        if process:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        return None
    except Exception as e:
        logger.error(f"[{tag}] Exception: {e}")
        if process:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        return None

    elapsed = round(time.time() - start, 2)
    if process.returncode == 0:
        candidates = [
            os.path.join(temp_dir, f)
            for f in os.listdir(temp_dir)
            if not f.endswith(".part") and not f.endswith(".ytdl")
        ]
        if candidates:
            downloaded = max(candidates, key=os.path.getsize)
            logger.info(f"[{tag}] ✅ ({elapsed}s): {downloaded} ({os.path.getsize(downloaded)} bytes)")
            return downloaded

    err = stderr.decode().strip() if stderr else "no stderr"
    logger.error(f"[{tag}] ❌ exit={process.returncode} ({elapsed}s): {err[-300:]}")
    return None


async def download_media(
    url: str,
    mode: str = "audio",
    cookies: Optional[str] = None,
    timeout: int = 180,
    tag: str = "DOWNLOAD",
) -> Optional[str]:
    """Download full media file to a unique temp directory.

    Args:
        url: YouTube watch/share URL or video identifier.
        mode: "audio" or "video".
        cookies: Optional cookies path or Netscape cookie data.
        timeout: Subprocess timeout in seconds.
        tag: Logging tag prefix.

    Returns:
        The local file path to the downloaded media file, or None on failure.
        On failure, all temporary files/directories are cleaned up.
        On success, caller is responsible for deleting the file and its parent
        directory after consumption.
    """
    temp_dir = tempfile.mkdtemp(prefix="ytube_dl_")
    selector = _AUDIO_SELECTOR if mode == "audio" else _VIDEO_SELECTOR

    try:
        # 1. Anonymous attempt first (matching ytdlp_runner convention)
        file_path = await _run_download(
            url, selector, [], temp_dir, timeout, f"{tag}/anon"
        )
        if file_path and os.path.exists(file_path):
            return file_path

        # 2. Cookie attempt retry if anonymous fails
        cargs = cookie_args(cookies)
        if cargs:
            file_path = await _run_download(
                url,
                f"{selector}/best",
                [*cargs, *_COOKIE_CLIENTS],
                temp_dir,
                timeout,
                f"{tag}/cookies",
            )
            if file_path and os.path.exists(file_path):
                return file_path

        # Clean up on failure
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    except Exception as e:
        logger.error(f"[{tag}] Unexpected download error: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
