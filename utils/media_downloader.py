"""Full-file media downloader using yt-dlp.

Downloads YouTube audio or video to a local temp file, adhering to the
shared concurrency limiter (subprocess_slot) and the anonymous-then-cookies
retry convention.
"""
import asyncio
import json
import logging
import os
import re
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


def _extract_video_id(url: str, file_path: str) -> str:
    m = re.search(r"(?:v=|\/|^)([0-9A-Za-z_-]{11})(?:[&?]|$)", url.strip())
    if m:
        return m.group(1)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    if stem and not stem.startswith("ytube_dl_"):
        return stem
    return url


async def _run_download(
    url: str,
    selector: str,
    extra: list[str],
    temp_dir: str,
    timeout: int,
    tag: str,
) -> tuple[Optional[str], Optional[str]]:
    format_file = os.path.join(temp_dir, f".format_id_{time.time_ns()}")
    title_file = os.path.join(temp_dir, ".title")
    uploader_file = os.path.join(temp_dir, ".uploader")
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--print-to-file", "%(format_id)s", format_file,
        "--print-to-file", "%(title)s", title_file,
        "--print-to-file", "%(uploader)s", uploader_file,
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
        if os.path.exists(format_file):
            try:
                os.remove(format_file)
            except Exception:
                pass
        return None, None
    except Exception as e:
        logger.error(f"[{tag}] Exception: {e}")
        if process:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        if os.path.exists(format_file):
            try:
                os.remove(format_file)
            except Exception:
                pass
        return None, None

    elapsed = round(time.time() - start, 2)
    format_id = None
    if os.path.exists(format_file):
        try:
            with open(format_file, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
                if lines:
                    format_id = lines[-1]
            os.remove(format_file)
        except Exception:
            pass

    if not format_id and stdout:
        m = re.search(r"Downloading (?:[0-9]+ )?format\(s\):\s*([^\s\n\r:]+)", stdout.decode(errors="replace"))
        if m:
            format_id = m.group(1)

    if process.returncode == 0:
        candidates = [
            os.path.join(temp_dir, f)
            for f in os.listdir(temp_dir)
            if not f.endswith(".part") and not f.endswith(".ytdl") and not f.startswith(".")
        ]
        if candidates:
            downloaded = max(candidates, key=os.path.getsize)
            logger.info(f"[{tag}] ✅ ({elapsed}s): {downloaded} ({os.path.getsize(downloaded)} bytes)")
            return downloaded, format_id

    err = stderr.decode().strip() if stderr else "no stderr"
    logger.error(f"[{tag}] ❌ exit={process.returncode} ({elapsed}s): {err[-300:]}")
    return None, None


async def _strip_video_if_needed(file_path: str, temp_dir: str, tag: str) -> tuple[str, bool]:
    """If file contains a video stream and an AAC audio stream, strip video stream without transcoding."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=index,codec_type,codec_name",
            "-of", "json",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0 or not stdout:
            logger.warning(f"[{tag}] ffprobe failed: {stderr.decode(errors='replace')}")
            return file_path, False
        data = json.loads(stdout.decode())
    except Exception as e:
        logger.warning(f"[{tag}] ffprobe check failed: {e}")
        return file_path, False

    streams = data.get("streams", [])
    logger.info(f"[{tag}] ffprobe streams before stripping: {streams}")

    has_video = any(s.get("codec_type") == "video" for s in streams)
    if not has_video:
        return file_path, False

    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    has_aac = any(s.get("codec_name", "").lower() == "aac" for s in audio_streams)
    if not has_aac:
        # Audio codec is not AAC; leave as is for send_document fallback
        logger.info(f"[{tag}] Video stream present but audio is not AAC; leaving file as is")
        return file_path, False

    base_stem = os.path.splitext(os.path.basename(file_path))[0]
    out_file = os.path.join(temp_dir, f"{base_stem}.stripped.m4a")
    final_file = os.path.join(temp_dir, f"{base_stem}.m4a")

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i", file_path,
        "-vn",
        "-c:a", "copy",
        out_file,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            if os.path.exists(final_file) and final_file != out_file:
                try:
                    os.remove(final_file)
                except Exception:
                    pass
            os.replace(out_file, final_file)

            # Probe stripped file streams for verification and logging
            try:
                p2 = await asyncio.create_subprocess_exec(
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "stream=index,codec_type,codec_name",
                    "-of", "json",
                    final_file,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                so2, _ = await asyncio.wait_for(p2.communicate(), timeout=10)
                if p2.returncode == 0 and so2:
                    after_streams = json.loads(so2.decode()).get("streams", [])
                    logger.info(f"[{tag}] ffprobe streams after stripping: {after_streams}")
            except Exception:
                pass

            return final_file, True
        else:
            err = stderr.decode().strip() if stderr else "unknown error"
            logger.warning(f"[{tag}] ffmpeg strip failed (exit={proc.returncode}): {err[-200:]}")
            if os.path.exists(out_file):
                try:
                    os.remove(out_file)
                except Exception:
                    pass
            return file_path, False
    except Exception as e:
        logger.warning(f"[{tag}] ffmpeg strip exception: {e}")
        if os.path.exists(out_file):
            try:
                os.remove(out_file)
            except Exception:
                pass
        return file_path, False


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
    used_cookies = False
    format_id: Optional[str] = None
    file_path: Optional[str] = None

    try:
        # 1. Anonymous attempt first (matching ytdlp_runner convention)
        file_path, format_id = await _run_download(
            url, selector, [], temp_dir, timeout, f"{tag}/anon"
        )

        # 2. Cookie attempt retry if anonymous fails - KEEP /best fallback
        if not (file_path and os.path.exists(file_path)):
            cargs = cookie_args(cookies)
            if cargs:
                used_cookies = True
                file_path, format_id = await _run_download(
                    url,
                    f"{selector}/best",
                    [*cargs, *_COOKIE_CLIENTS],
                    temp_dir,
                    timeout,
                    f"{tag}/cookies",
                )

        if file_path and os.path.exists(file_path):
            video_stripped = False
            if mode == "audio":
                file_path, video_stripped = await _strip_video_if_needed(
                    file_path, temp_dir, tag
                )

            video_id = _extract_video_id(url, file_path)
            ext = os.path.splitext(file_path)[1].lstrip(".").lower()
            logger.info(
                f"[{tag}] video_id={video_id} format_id={format_id} ext={ext} "
                f"cookie_retry={'yes' if used_cookies else 'no'} video_stripped={'yes' if video_stripped else 'no'}"
            )
            return file_path

        # Clean up on failure
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    except Exception as e:
        logger.error(f"[{tag}] Unexpected download error: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
