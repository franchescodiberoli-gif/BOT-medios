import yt_dlp
import os
import re
import tempfile
import logging
import requests
import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

logger = logging.getLogger(__name__)

INSTAGRAM_COOKIES = os.environ.get("INSTAGRAM_COOKIES", None)
YOUTUBE_COOKIES   = os.environ.get("YOUTUBE_COOKIES",   None)
REDDIT_COOKIES    = os.environ.get("REDDIT_COOKIES",    None)
REDGIFS_COOKIES   = os.environ.get("REDGIFS_COOKIES",   None)
COOKIES_FILE      = os.environ.get("COOKIES_FILE",      None)

# ─── Proxy residencial SOCKS5 ─────────────────────────────────────
PROXY = "socks5://Ot1bKeBOWiEOWQ2:nXx63GvaccqaYB2@178.93.24.237:42342"
PROXIES = {"http": PROXY, "https": PROXY}

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


def _extract_video_id(url: str) -> str | None:
    for pat in [
        r"v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"shorts/([a-zA-Z0-9_-]{11})",
        r"embed/([a-zA-Z0-9_-]{11})",
        r"live/([a-zA-Z0-9_-]{11})",
    ]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


# ═══════════════════════════════════════════════════════════════════
# ESTRATEGIA 1: cobalt.tools (vía proxy)
# ═══════════════════════════════════════════════════════════════════

COBALT_INSTANCES = [
    "https://api.cobalt.tools",
    "https://cobalt.api.timelessnesses.me",
    "https://cob.frytea.com",
    "https://cobalt.datura.network",
    "https://cobalt.svaba.site",
]


def _download_direct_url(url: str, ext: str = "mp4") -> str | None:
    try:
        tmp = os.path.join(tempfile.mkdtemp(), f"video.{ext}")
        hdrs = {"User-Agent": "Mozilla/5.0 (compatible; MediaBot/2.0)"}
        with requests.get(url, headers=hdrs, stream=True, timeout=120,
                          proxies=PROXIES, verify=False) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
        if os.path.getsize(tmp) > 10_000:
            return tmp
    except Exception as e:
        logger.warning(f"  _download_direct_url: {e}")
    return None


def _try_cobalt(url: str) -> tuple[str | None, dict | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "MediaBot/2.0",
    }
    payload = {"url": url}

    for instance in COBALT_INSTANCES:
        try:
            logger.info(f"→ cobalt [{instance}]...")
            r = requests.post(
                instance, json=payload, headers=headers,
                timeout=25, proxies=PROXIES, verify=False
            )
            if r.status_code == 429:
                logger.warning(f"  cobalt {instance} → rate limit")
                continue
            if r.status_code != 200:
                logger.warning(f"  cobalt {instance} → HTTP {r.status_code}")
                continue

            data = r.json()
            status = data.get("status", "")

            if status in ("stream", "tunnel", "redirect") and data.get("url"):
                fp = _download_direct_url(data["url"], "mp4")
                if fp:
                    vid_id = _extract_video_id(url) or "video"
                    info = {
                        "id": vid_id,
                        "title": data.get("filename", vid_id),
                        "webpage_url": url,
                        "extractor_key": "Youtube",
                    }
                    logger.info(f"✅ cobalt OK [{instance}]")
                    return fp, info

            if status == "picker" and data.get("picker"):
                for item in data["picker"]:
                    if item.get("url"):
                        fp = _download_direct_url(item["url"], item.get("type", "mp4"))
                        if fp:
                            vid_id = _extract_video_id(url) or "video"
                            return fp, {
                                "id": vid_id, "title": vid_id,
                                "webpage_url": url, "extractor_key": "Youtube"
                            }

            if status == "error":
                code = data.get("error", {}).get("code", "?")
                logger.warning(f"  cobalt {instance} → error: {code}")

        except requests.exceptions.Timeout:
            logger.warning(f"  cobalt {instance} → timeout")
        except Exception as e:
            logger.warning(f"  cobalt {instance} → {e}")

    return None, None


# ═══════════════════════════════════════════════════════════════════
# ESTRATEGIA 2: yt-dlp con proxy
# ═══════════════════════════════════════════════════════════════════

def _get_js_runtimes() -> dict:
    import shutil
    node_path = shutil.which("node") or shutil.which("nodejs") or "/usr/bin/nodejs"
    return {"node": {"path": node_path}}


def _ytdlp_download(url: str, client: str, cookies: str | None) -> tuple[str | None, dict | None]:
    tmp_dir = tempfile.mkdtemp()
    opts = {
        "quiet":               True,
        "no_warnings":         True,
        "merge_output_format": "mp4",
        "noplaylist":          True,
        "socket_timeout":      60,
        "nocheckcertificate":  True,
        "format":              "best[height<=720]/best",
        "js_runtimes":         _get_js_runtimes(),
        "outtmpl":             os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "extractor_args":      {"youtube": {"player_client": [client]}},
        "proxy":               PROXY,
    }
    if cookies:
        opts["cookiefile"] = cookies

    try:
        logger.info(f"→ yt-dlp [{client}] + proxy...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp) and os.path.getsize(fp) > 10_000:
                    logger.info(f"✅ yt-dlp OK [{client}]")
                    return fp, info
    except Exception as e:
        err = str(e)
        if "Private video" in err or "private" in err.lower():
            logger.error(f"[{client}] Video privado")
            return "PRIVATE", None
        logger.warning(f"[{client}] {err[:120]}")

    return None, None


def _try_ytdlp_all(url: str, cookies: str | None) -> tuple[str | None, dict | None]:
    for client in ["tv_embedded", "mweb", "ios", "web"]:
        fp, info = _ytdlp_download(url, client, cookies)
        if fp == "PRIVATE":
            return None, None
        if fp:
            return fp, info
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Función principal YouTube
# ═══════════════════════════════════════════════════════════════════

def download_youtube(url: str, platform: str) -> tuple[str | None, dict | None]:
    cookies = _cookies(platform)

    logger.info("🔄 [1/2] cobalt.tools + proxy...")
    fp, info = _try_cobalt(url)
    if fp:
        return fp, info

    logger.info("🔄 [2/2] yt-dlp + proxy...")
    fp, info = _try_ytdlp_all(url, cookies)
    if fp:
        return fp, info

    logger.error(f"❌ Todas las estrategias fallaron: {url}")
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Descargador genérico (non-YouTube) — también usa proxy para Reddit
# ═══════════════════════════════════════════════════════════════════

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
        "nocheckcertificate":  True,
        "format":              "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "proxy":               PROXY,
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


# ═══════════════════════════════════════════════════════════════════
# Reddit image fallback — con proxy
# ═══════════════════════════════════════════════════════════════════

def download_reddit_image(url: str) -> tuple[str | None, dict | None]:
    try:
        clean = url.split("?")[0].rstrip("/")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        resp = requests.get(clean + ".json", headers=headers,
                            proxies=PROXIES, timeout=15)
        resp.raise_for_status()
        post = resp.json()[0]["data"]["children"][0]["data"]
        title = post.get("title", "Reddit post")
        img_url = post.get("url_overridden_by_dest", "")

        if img_url and any(img_url.lower().endswith(e) for e in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            ext = img_url.split(".")[-1].split("?")[0].lower()
            r = requests.get(img_url, headers=headers, proxies=PROXIES, timeout=30)
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
                        r = requests.get(img_url, headers=headers, proxies=PROXIES, timeout=30)
                        tmp = os.path.join(tempfile.mkdtemp(), "reddit.jpg")
                        with open(tmp, "wb") as f:
                            f.write(r.content)
                        return tmp, {"title": title, "webpage_url": url, "ext": "jpg", "extractor_key": "Reddit"}
    except Exception as e:
        logger.error(f"download_reddit_image error: {e}")
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

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
