import yt_dlp
import os
import re
import tempfile
import logging
import requests

logger = logging.getLogger(__name__)

INSTAGRAM_COOKIES = os.environ.get("INSTAGRAM_COOKIES", None)
YOUTUBE_COOKIES   = os.environ.get("YOUTUBE_COOKIES",   None)
REDDIT_COOKIES    = os.environ.get("REDDIT_COOKIES",    None)
REDGIFS_COOKIES   = os.environ.get("REDGIFS_COOKIES",   None)
COOKIES_FILE      = os.environ.get("COOKIES_FILE",      None)

_PATHS = {
    "instagram": "/tmp/ig_cookies.txt",
    "youtube":   "/tmp/yt_cookies.txt",
    "reddit":    "/tmp/rd_cookies.txt",
    "redgifs":   "/tmp/rg_cookies.txt",
}


def _write_cookies(content: str, path: str) -> str | None:
    if not content:
        return None
    if os.path.exists(path):
        os.remove(path)
    lines = ["# Netscape HTTP Cookie File"]
    for line in content.strip().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _cookies(platform: str) -> str | None:
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        return COOKIES_FILE
    mapping = {
        "instagram":     (INSTAGRAM_COOKIES, "instagram"),
        "youtube_short": (YOUTUBE_COOKIES,   "youtube"),
        "youtube_long":  (YOUTUBE_COOKIES,   "youtube"),
        "reddit":        (REDDIT_COOKIES,    "reddit"),
        "redgifs":       (REDGIFS_COOKIES,   "redgifs"),
    }
    if platform in mapping:
        content, key = mapping[platform]
        if content:
            return _write_cookies(content, _PATHS[key])
    return None


# ─── YouTube via cobalt.tools (free downloader API) ──────────────────────────

def _cobalt_download(url: str) -> tuple[str | None, dict | None]:
    """
    Use cobalt.tools public API to get a direct download link for YouTube.
    Returns (file_path, fake_info_dict) or (None, None).
    """
    try:
        resp = requests.post(
            "https://api.cobalt.tools/",
            json={"url": url, "videoQuality": "1080", "filenameStyle": "basic"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=20,
        )
        data = resp.json()
        status = data.get("status", "")
        logger.info(f"cobalt status: {status}")

        download_url = None
        if status in ("stream", "redirect", "tunnel"):
            download_url = data.get("url")
        elif status == "picker":
            # multiple streams — take first video
            for item in data.get("picker", []):
                if item.get("type") == "video":
                    download_url = item.get("url")
                    break
            if not download_url and data.get("picker"):
                download_url = data["picker"][0].get("url")

        if not download_url:
            logger.error(f"cobalt no download_url: {data}")
            return None, None

        # Download the actual file
        r = requests.get(download_url, timeout=120, stream=True)
        r.raise_for_status()

        # Detect extension from content-type or URL
        ct = r.headers.get("content-type", "")
        ext = "mp4"
        if "webm" in ct:
            ext = "webm"
        elif "audio" in ct:
            ext = "m4a"

        tmp = os.path.join(tempfile.mkdtemp(), f"yt_video.{ext}")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

        # Build minimal info dict so the rest of the bot works
        info = {
            "id":          url.split("v=")[-1].split("&")[0].split("/")[-1],
            "title":       data.get("filename", "YouTube video"),
            "description": "",
            "webpage_url": url,
            "extractor_key": "Youtube",
        }
        return tmp, info

    except Exception as e:
        logger.error(f"cobalt error: {e}")
        return None, None


def _get_yt_meta(url: str) -> dict:
    """Get title/description/tags from YouTube without downloading."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception:
        return {}


def download_youtube(url: str, platform: str) -> tuple[str | None, dict | None]:
    """
    Step 1: fetch metadata with yt-dlp (no download, fast).
    Step 2: download file via cobalt.tools API.
    Step 3: fallback to yt-dlp with android_vr client.
    """
    # Get metadata first
    meta = _get_yt_meta(url)

    # Try cobalt
    file_path, info = _cobalt_download(url)
    if file_path:
        # Enrich with real metadata
        if meta:
            info["title"]       = meta.get("title", info["title"])
            info["description"] = meta.get("description", "")
            info["id"]          = meta.get("id", info["id"])
            info["webpage_url"] = meta.get("webpage_url", url)
        return file_path, info

    # Fallback: yt-dlp with android_vr client
    logger.info("cobalt failed, trying yt-dlp android_vr...")
    cookies = _cookies(platform)
    for clients in (["android_vr"], ["android"], ["tv_embedded"]):
        tmp_dir = tempfile.mkdtemp()
        opts = {
            "outtmpl":             os.path.join(tmp_dir, "%(id)s.%(ext)s"),
            "quiet":               True,
            "no_warnings":         True,
            "merge_output_format": "mp4",
            "noplaylist":          True,
            "socket_timeout":      30,
            "format":              "best",
            "extractor_args":      {"youtube": {"player_client": clients}},
        }
        if cookies:
            opts["cookiefile"] = cookies
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info2 = ydl.extract_info(url, download=True)
                for f in os.listdir(tmp_dir):
                    fp = os.path.join(tmp_dir, f)
                    if os.path.isfile(fp):
                        return fp, info2
        except Exception as e:
            logger.error(f"yt-dlp {clients} error: {e}")
            if "DRM" in str(e):
                return None, None

    return None, None


# ─── Generic downloader (non-YouTube) ────────────────────────────────────────

def download_media(url: str, platform: str = None) -> tuple[str | None, dict | None]:
    if "threads.com" in url:
        url = url.replace("threads.com", "threads.net")

    if platform in ("youtube_short", "youtube_long"):
        return download_youtube(url, platform)

    tmp_dir = tempfile.mkdtemp()
    opts = {
        "outtmpl":             os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "quiet":               True,
        "no_warnings":         True,
        "merge_output_format": "mp4",
        "noplaylist":          True,
        "socket_timeout":      30,
        "format":              "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    }
    cookies = _cookies(platform)
    if cookies:
        opts["cookiefile"] = cookies

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp):
                    return fp, info
    except Exception as e:
        logger.error(f"download_media error: {e}")

    return None, None


# ─── Reddit image fallback ────────────────────────────────────────────────────

def download_reddit_image(url: str) -> tuple[str | None, dict | None]:
    try:
        clean = url.split("?")[0].rstrip("/")
        headers = {"User-Agent": "mediabot/1.0"}
        resp = requests.get(clean + ".json", headers=headers, timeout=15)
        resp.raise_for_status()
        post = resp.json()[0]["data"]["children"][0]["data"]
        title = post.get("title", "Reddit post")
        img_url = post.get("url_overridden_by_dest", "")

        if img_url and any(img_url.lower().endswith(e) for e in [".jpg",".jpeg",".png",".gif",".webp"]):
            ext = img_url.split(".")[-1].split("?")[0].lower()
            r = requests.get(img_url, headers=headers, timeout=30)
            r.raise_for_status()
            tmp = os.path.join(tempfile.mkdtemp(), f"reddit.{ext}")
            with open(tmp, "wb") as f:
                f.write(r.content)
            return tmp, {"title": title, "webpage_url": url, "ext": ext, "extractor_key": "Reddit"}

        if post.get("is_gallery") and post.get("media_metadata"):
            for _, media in post["media_metadata"].items():
                if media.get("status") == "valid":
                    img_url = media.get("s", {}).get("u", "").replace("&amp;", "&")
                    if img_url:
                        r = requests.get(img_url, headers=headers, timeout=30)
                        tmp = os.path.join(tempfile.mkdtemp(), "reddit.jpg")
                        with open(tmp, "wb") as f:
                            f.write(r.content)
                        return tmp, {"title": title, "webpage_url": url, "ext": "jpg", "extractor_key": "Reddit"}
    except Exception as e:
        logger.error(f"download_reddit_image error: {e}")
    return None, None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def clean_hashtags(text: str) -> str:
    if not text:
        return ""
    return " ".join(re.findall(r"#\w+", text))


def get_clean_url(info: dict) -> str:
    webpage     = info.get("webpage_url", "")
    video_id    = info.get("id", "")
    uploader_id = info.get("uploader_id", "")
    platform    = info.get("extractor_key", "").lower()

    if "youtube"   in platform: return f"https://www.youtube.com/watch?v={video_id}"
    if "instagram" in platform: return f"https://www.instagram.com/p/{video_id}/"
    if "tiktok"    in platform: return f"https://www.tiktok.com/@{uploader_id}/video/{video_id}"
    if "twitter"   in platform or "x.com" in platform: return f"https://x.com/i/status/{video_id}"
    if "facebook"  in platform: return f"https://www.facebook.com/watch/?v={video_id}" if video_id else webpage
    return webpage
