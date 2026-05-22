import yt_dlp
import os
import tempfile
import re
import logging

logger = logging.getLogger(__name__)

COOKIES_FILE = os.environ.get("COOKIES_FILE", None)


def _base_ydl_opts(output_dir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def extract_info(url: str) -> dict | None:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        logger.error(f"extract_info error: {e}")
        return None


def download_media(url: str) -> tuple[str | None, dict | None]:
    tmp_dir = tempfile.mkdtemp()
    ydl_opts = _base_ydl_opts(tmp_dir)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            for f in os.listdir(tmp_dir):
                full_path = os.path.join(tmp_dir, f)
                if os.path.isfile(full_path):
                    return full_path, info
        return None, None
    except Exception as e:
        logger.error(f"download_media error: {e}")
        return None, None


def clean_hashtags(text: str) -> str:
    if not text:
        return ""
    tags = re.findall(r"#\w+", text)
    return " ".join(tags)


def get_clean_url(info: dict) -> str:
    webpage = info.get("webpage_url", "")
    video_id = info.get("id", "")
    uploader_id = info.get("uploader_id", "")
    platform = info.get("extractor_key", "").lower()

    if "youtube" in platform:
        return f"https://www.youtube.com/watch?v={video_id}"
    elif "instagram" in platform:
        return f"https://www.instagram.com/p/{video_id}/"
    elif "tiktok" in platform:
        return f"https://www.tiktok.com/@{uploader_id}/video/{video_id}"
    elif "twitter" in platform or "x.com" in platform:
        return f"https://x.com/i/status/{video_id}"
    elif "reddit" in platform:
        return webpage
    elif "facebook" in platform:
        return f"https://www.facebook.com/watch/?v={video_id}" if video_id else webpage
    elif "threads" in platform:
        return webpage
    else:
        return webpage
