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
TWITTER_COOKIES   = os.environ.get("TWITTER_COOKIES",   None)
COOKIES_FILE      = os.environ.get("COOKIES_FILE",      None)

PROXY   = os.environ.get("PROXY_URL", "")
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else {}

_PATHS = {
    "instagram": "/tmp/ig_cookies.txt",
    "youtube":   "/tmp/yt_cookies.txt",
    "reddit":    "/tmp/rd_cookies.txt",
    "redgifs":   "/tmp/rg_cookies.txt",
    "twitter":   "/tmp/tw_cookies.txt",
}


def _write_cookies(content: str, path: str) -> str | None:
    if not content:
        return None
    if os.path.exists(path):
        os.remove(path)

    # Detectar si el contenido es JSON (cookies exportadas con extensión de navegador)
    stripped = content.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            import json
            cookie_list = json.loads(stripped)
            lines = ["# Netscape HTTP Cookie File"]
            for c in cookie_list:
                domain = c.get("domain", "")
                # hostOnly=True → sin punto; hostOnly=False → con punto
                if not domain.startswith(".") and not c.get("hostOnly", True):
                    domain = "." + domain
                include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                secure = "TRUE" if c.get("secure", False) else "FALSE"
                expiry = int(c.get("expirationDate", 0))
                name  = c.get("name", "")
                value = c.get("value", "")
                path_ = c.get("path", "/")
                lines.append(f"{domain}\t{include_sub}\t{path_}\t{secure}\t{expiry}\t{name}\t{value}")
            with open(path, "w") as f:
                f.write("\n".join(lines) + "\n")
            return path
        except Exception as e:
            logger.warning(f"_write_cookies JSON parse error: {e}")

    # Formato Netscape normal
    lines = ["# Netscape HTTP Cookie File"]
    for line in stripped.splitlines():
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
        "twitter":       (TWITTER_COOKIES,   "twitter"),
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
# COBALT
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
        logger.warning(f"_download_direct_url: {e}")
    return None


def _try_cobalt(url: str) -> tuple[str | None, dict | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "MediaBot/2.0",
    }
    for instance in COBALT_INSTANCES:
        try:
            logger.info(f"→ cobalt [{instance}]...")
            r = requests.post(instance, json={"url": url}, headers=headers,
                              timeout=25, proxies=PROXIES, verify=False)
            if r.status_code != 200:
                continue
            data   = r.json()
            status = data.get("status", "")

            if status in ("stream", "tunnel", "redirect") and data.get("url"):
                fp = _download_direct_url(data["url"])
                if fp:
                    vid_id = _extract_video_id(url) or "video"
                    return fp, {
                        "id": vid_id, "title": data.get("filename", vid_id),
                        "webpage_url": url, "extractor_key": "Youtube",
                    }

            if status == "picker" and data.get("picker"):
                for item in data["picker"]:
                    if item.get("url"):
                        fp = _download_direct_url(item["url"])
                        if fp:
                            vid_id = _extract_video_id(url) or "video"
                            return fp, {
                                "id": vid_id, "title": vid_id,
                                "webpage_url": url, "extractor_key": "Youtube",
                            }
            if status == "error":
                logger.warning(f"cobalt {instance} → {data.get('error', {}).get('code','?')}")
        except Exception as e:
            logger.warning(f"cobalt {instance} → {e}")
    return None, None


# ═══════════════════════════════════════════════════════════════════
# yt-dlp
# ═══════════════════════════════════════════════════════════════════

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
        "outtmpl":             os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "extractor_args":      {"youtube": {"player_client": [client]}},
    }
    if PROXY:
        opts["proxy"] = PROXY
    if cookies:
        opts["cookiefile"] = cookies
    try:
        logger.info(f"→ yt-dlp [{client}]...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp) and os.path.getsize(fp) > 10_000:
                    return fp, info
    except Exception as e:
        err = str(e)
        if "DRM" in err or "private" in err.lower():
            return "PRIVATE", None
        logger.warning(f"yt-dlp [{client}]: {err[:120]}")
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
# Redgifs  ──  descarga nativa vía API (sin yt-dlp)
# ═══════════════════════════════════════════════════════════════════

def _redgifs_get_token(session: requests.Session) -> str | None:
    headers = {
        "User-Agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Origin":         "https://www.redgifs.com",
        "Referer":        "https://www.redgifs.com/",
        "Accept":         "application/json",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    try:
        r = session.get("https://api.redgifs.com/v2/auth/temporary",
                        headers=headers, timeout=20, proxies=PROXIES, verify=False)
        if r.status_code == 200:
            return r.json().get("token")
        logger.warning(f"redgifs auth: {r.status_code} {r.text[:100]}")
    except Exception as e:
        logger.warning(f"redgifs auth error: {e}")
    return None


def download_redgifs(url: str) -> tuple[str | None, dict | None]:
    """Descarga un video de redgifs.com/watch/<id> via API con proxy."""
    m = re.search(r"redgifs\.com/(?:watch|ifr)/([a-zA-Z0-9]+)", url)
    if not m:
        return None, None

    gif_id  = m.group(1).lower()
    session = requests.Session()

    # Cargar cookies de redgifs si existen
    for line in (REDGIFS_COOKIES or "").strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            session.cookies.set(parts[5], parts[6], domain=parts[0].lstrip("."))

    token = _redgifs_get_token(session)

    api_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Origin":     "https://www.redgifs.com",
        "Referer":    f"https://www.redgifs.com/watch/{gif_id}",
        "Accept":     "application/json",
    }
    if token:
        api_headers["Authorization"] = f"Bearer {token}"

    try:
        r = session.get(f"https://api.redgifs.com/v2/gifs/{gif_id}",
                        headers=api_headers, timeout=20,
                        proxies=PROXIES, verify=False)
        r.raise_for_status()
        gif   = r.json().get("gif", {})
        urls  = gif.get("urls", {})
        # title suele venir vacío; description tiene el texto real del post
        title = (gif.get("description") or gif.get("title") or "").strip()
        tags  = gif.get("tags", [])
        user  = gif.get("userName", "")

        video_url = urls.get("hd") or urls.get("sd") or urls.get("gif")
        if not video_url:
            logger.warning(f"redgifs: sin URL de video para {gif_id}")
            return None, None

        ext = "mp4" if ".mp4" in video_url else "gif"
        dl_headers = {**api_headers, "Accept": "*/*"}
        tmp = os.path.join(tempfile.mkdtemp(), f"redgifs.{ext}")
        with session.get(video_url, headers=dl_headers, stream=True,
                         timeout=120, proxies=PROXIES, verify=False) as rv:
            rv.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in rv.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)

        if os.path.getsize(tmp) > 10_000:
            info = {
                "id":            gif_id,
                "title":         title,
                "uploader":      user,
                "tags":          tags,
                "description":   " ".join(f"#{t}" for t in tags),
                "webpage_url":   f"https://www.redgifs.com/watch/{gif_id}",
                "extractor_key": "RedGifs",
                "ext":           ext,
            }
            return tmp, info

    except Exception as e:
        logger.error(f"download_redgifs error: {e}")

    return None, None


# ═══════════════════════════════════════════════════════════════════
# YouTube
# ═══════════════════════════════════════════════════════════════════

def download_youtube(url: str, platform: str) -> tuple[str | None, dict | None]:
    cookies = _cookies(platform)
    fp, info = _try_cobalt(url)
    if fp:
        return fp, info
    return _try_ytdlp_all(url, cookies)


# ═══════════════════════════════════════════════════════════════════
# Descargador genérico
# ═══════════════════════════════════════════════════════════════════

def download_media(url: str, platform: str = None) -> tuple[str | None, dict | None]:
    # Threads: forzar threads.net en todos los casos
    if "threads.com" in url or "threads.net" in url:
        url = re.sub(r"https?://(www\.)?threads\.(com|net)", "https://www.threads.net", url)

    if platform in ("youtube_short", "youtube_long"):
        return download_youtube(url, platform)

    # Redgifs: usa API nativa con proxy, no yt-dlp
    if platform == "redgifs" or "redgifs.com" in url:
        return download_redgifs(url)

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
    }
    if PROXY:
        opts["proxy"] = PROXY
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
        err = str(e)
        # Twitter: si no hay video intentar descargar imagen del tweet
        if platform == "twitter" and "No video" in err:
            return _download_twitter_image(url)
        logger.error(f"download_media error: {e}")
    return None, None


def _download_twitter_image(url: str) -> tuple[str | None, dict | None]:
    """Fallback para tweets que solo tienen imagen (sin video)."""
    # Extraer tweet_id de la URL
    m = re.search(r"status/(\d+)", url)
    tweet_id = m.group(1) if m else "tweet"

    base_info = {
        "id":            tweet_id,
        "title":         f"Tweet {tweet_id}",
        "webpage_url":   url,
        "extractor_key": "Twitter",
    }

    # ── Intento 1: yt-dlp con skip_download para obtener thumbnail ──
    try:
        opts = {
            "quiet":              True,
            "no_warnings":        True,
            "skip_download":      True,
            "nocheckcertificate": True,
        }
        if PROXY:
            opts["proxy"] = PROXY
        cookies = _cookies("twitter")
        if cookies:
            opts["cookiefile"] = cookies

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Buscar imágenes en entries (tweet con múltiples fotos)
        entries = info.get("entries") or []
        if not entries and info.get("thumbnail"):
            entries = [info]

        for entry in entries or [info]:
            thumbnail = entry.get("thumbnail") or ""
            if not thumbnail:
                continue
            ext = thumbnail.split(".")[-1].split("?")[0].lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                ext = "jpg"
            img_url = thumbnail
            if "pbs.twimg.com" in thumbnail:
                img_url = re.sub(r"\?.*$", "", thumbnail) + "?format=jpg&name=large"
            fp = _dl_image(img_url, ext)
            if fp:
                info["ext"] = ext
                return fp, info

    except Exception as e:
        logger.warning(f"_download_twitter_image (yt-dlp): {e}")

    # ── Intento 2: API pública de Twitter/X syndication ──────────────
    try:
        api_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&features=tfw_timeline_list%3A%3Btfw_follower_count_sunset%3Atrue&token=x"
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Accept":     "application/json",
            "Referer":    "https://platform.twitter.com/",
            "Origin":     "https://platform.twitter.com",
        }
        r = requests.get(api_url, headers=hdrs, timeout=20,
                         proxies=PROXIES, verify=False)
        if r.status_code == 200:
            data = r.json()
            # Buscar mediaDetails con fotos
            media_list = data.get("mediaDetails") or []
            photos = [m for m in media_list if m.get("type") == "photo"]
            if photos:
                # Tomar la de mayor resolución
                best = photos[0]
                sizes = best.get("sizes", {})
                # Preferir "large" > "medium" > "small"
                size_key = next((k for k in ("large", "medium", "small") if k in sizes), None)
                base_media_url = best.get("media_url_https", "")
                if base_media_url:
                    img_url = base_media_url + (f"?name={size_key}" if size_key else "?name=large")
                    ext = base_media_url.rsplit(".", 1)[-1].split("?")[0].lower()
                    if ext not in ("jpg", "jpeg", "png", "webp"):
                        ext = "jpg"
                    fp = _dl_image(img_url, ext)
                    if fp:
                        text = data.get("text", "")
                        info = {**base_info, "title": text[:100] if text else base_info["title"], "ext": ext}
                        return fp, info

            # Sin fotos pero hay video thumbnail
            for m_item in media_list:
                thumb = m_item.get("media_url_https", "")
                if thumb:
                    fp = _dl_image(thumb + "?name=large", "jpg")
                    if fp:
                        return fp, {**base_info, "ext": "jpg"}

    except Exception as e:
        logger.warning(f"_download_twitter_image (syndication API): {e}")

    logger.warning(f"_download_twitter_image: no se pudo obtener imagen del tweet {tweet_id}")
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Reddit  ──  descarga completa
#   Retorna (archivos, info)
#   archivos puede ser: str (1 archivo) o list[str] (galería)
# ═══════════════════════════════════════════════════════════════════

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".gifv"}
_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Accept":     "application/json",
}


def _fetch_post_json(url: str) -> dict | None:
    """Obtiene el dict de datos del primer post de la URL de Reddit."""
    clean = re.split(r"[?#]", url)[0].rstrip("/")
    try:
        resp = requests.get(clean + ".json", headers=_HEADERS,
                            proxies=PROXIES, timeout=20, verify=False)
        resp.raise_for_status()
        data = resp.json()
        return data[0]["data"]["children"][0]["data"]
    except Exception as e:
        logger.error(f"_fetch_post_json error: {e}")
    return None


def _dl_image(img_url: str, ext: str = "jpg") -> str | None:
    """Descarga una imagen/gif a un archivo temporal."""
    try:
        r = requests.get(img_url, headers=_HEADERS, proxies=PROXIES,
                         timeout=30, verify=False)
        r.raise_for_status()
        tmp = os.path.join(tempfile.mkdtemp(), f"reddit.{ext}")
        with open(tmp, "wb") as f:
            f.write(r.content)
        if os.path.getsize(tmp) > 1000:
            return tmp
    except Exception as e:
        logger.warning(f"_dl_image: {e}")
    return None


def download_reddit_post(url: str) -> tuple[str | list[str] | None, dict | None]:
    """
    Descarga un post de Reddit. Devuelve:
      - (str, info)       → imagen única / video / gif
      - (list[str], info) → galería de imágenes
      - (None, None)      → error
    """
    post = _fetch_post_json(url)
    if not post:
        return None, None

    title   = post.get("title", "Reddit post")
    post_url = post.get("url_overridden_by_dest", "")
    base_info = {"title": title, "webpage_url": url, "extractor_key": "Reddit"}

    # ── 1. Galería ────────────────────────────────────────────────
    if post.get("is_gallery") and post.get("media_metadata"):
        files = []
        # gallery_data tiene el orden correcto
        ordered_ids = []
        gd = post.get("gallery_data", {})
        if gd and gd.get("items"):
            ordered_ids = [item["media_id"] for item in gd["items"]]
        else:
            ordered_ids = list(post["media_metadata"].keys())

        for mid in ordered_ids:
            media = post["media_metadata"].get(mid, {})
            if media.get("status") != "valid":
                continue
            mime = media.get("m", "image/jpeg")
            ext  = mime.split("/")[-1] if "/" in mime else "jpg"
            # URL de máxima resolución
            img_url = (media.get("s", {}).get("u", "") or "").replace("&amp;", "&")
            if not img_url:
                continue
            fp = _dl_image(img_url, ext)
            if fp:
                files.append(fp)

        if files:
            info = {**base_info, "ext": "jpg", "count": len(files), "type": "gallery"}
            return files, info

    # ── 2. Imagen única (jpg/png/gif/webp directo) ─────────────────
    if post_url:
        url_low = post_url.lower().split("?")[0]
        ext = url_low.rsplit(".", 1)[-1] if "." in url_low else ""
        if ext in ("jpg", "jpeg", "png", "webp", "gif", "gifv"):
            actual_url = post_url
            # gifv → mp4 en imgur
            if ext == "gifv":
                actual_url = post_url.replace(".gifv", ".mp4")
                fp = _dl_image(actual_url, "mp4")
                if fp:
                    return fp, {**base_info, "ext": "mp4", "type": "video"}
            fp = _dl_image(actual_url, ext)
            if fp:
                tipo = "gif" if ext == "gif" else "image"
                return fp, {**base_info, "ext": ext, "type": tipo}

    # ── 3. Redgifs embebido en post de Reddit ─────────────────────
    media = post.get("media") or {}
    oembed = media.get("oembed", {})
    secure_media = post.get("secure_media") or {}
    redgif_url = None

    # Buscar URL de redgifs en distintos campos
    for field in [post.get("url_overridden_by_dest", ""),
                  media.get("reddit_video", {}).get("fallback_url", ""),
                  secure_media.get("reddit_video", {}).get("fallback_url", "")]:
        if "redgifs.com" in str(field):
            redgif_url = field
            break

    if not redgif_url:
        # También en media.type
        mt = media.get("type", "")
        if "redgifs" in mt:
            # extraer del embed html
            embed_html = oembed.get("html", "")
            m = re.search(r'redgifs\.com/ifr/([a-zA-Z0-9]+)', embed_html)
            if m:
                redgif_url = f"https://www.redgifs.com/watch/{m.group(1)}"

    if redgif_url:
        logger.info(f"Reddit post contiene redgifs: {redgif_url}")
        fp, rg_info = download_media(redgif_url, "redgifs")
        if fp and rg_info:
            # Enriquecer con info del post de Reddit
            rg_info["reddit_title"]   = title
            rg_info["reddit_post_url"] = url
            rg_info["redgif_url"]     = redgif_url
            rg_info["type"]           = "redgif_in_reddit"
            return fp, rg_info

    # ── 4. Video nativo de Reddit (v.redd.it) ─────────────────────
    rv = (post.get("media") or {}).get("reddit_video") or \
         (post.get("secure_media") or {}).get("reddit_video")
    if rv:
        video_url = rv.get("fallback_url", "").replace("?source=fallback", "")
        if video_url:
            fp, info = download_media(video_url, "reddit")
            if fp:
                if info:
                    info["title"] = title
                    info["webpage_url"] = url
                else:
                    info = {**base_info, "ext": "mp4", "type": "video"}
                return fp, info

    # ── 5. Intentar con yt-dlp directamente ───────────────────────
    fp, info = download_media(url, "reddit")
    if fp:
        if info:
            info.setdefault("title", title)
        return fp, info

    return None, None


# ── Alias para compatibilidad ────────────────────────────────────────
def download_reddit_image(url: str) -> tuple[str | None, dict | None]:
    """Wrapper de compatibilidad — usa download_reddit_post internamente."""
    result, info = download_reddit_post(url)
    if isinstance(result, list):
        # Devuelve primera imagen para compatibilidad con código viejo
        return result[0] if result else None, info
    return result, info


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
