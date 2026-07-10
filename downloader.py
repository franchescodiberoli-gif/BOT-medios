import yt_dlp
import os
import re
import sys
import time
import shutil
import tempfile
import threading
import logging
import requests
import http.cookiejar
import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

logger = logging.getLogger(__name__)

INSTAGRAM_COOKIES = os.environ.get("INSTAGRAM_COOKIES", None)
YOUTUBE_COOKIES   = os.environ.get("YOUTUBE_COOKIES",   None)
REDDIT_COOKIES    = os.environ.get("REDDIT_COOKIES",    None)
REDGIFS_COOKIES   = os.environ.get("REDGIFS_COOKIES",   None)
TWITTER_COOKIES   = os.environ.get("TWITTER_COOKIES",   None)
FACEBOOK_COOKIES  = os.environ.get("FACEBOOK_COOKIES",  None)
COOKIES_FILE      = os.environ.get("COOKIES_FILE",      None)

# Credenciales opcionales de la API oficial de Reddit (crear app "script" gratis
# en reddit.com/prefs/apps). Reddit bloquea el .json anónimo desde IPs de
# datacenter; con esto el bot usa OAuth app-only, que sí está permitido.
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID",     "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_UA            = "script:mediabot:2.0 (bot personal de Telegram)"

PROXY   = os.environ.get("PROXY_URL", "")
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else {}

_PATHS = {
    "instagram": "/tmp/ig_cookies.txt",
    "youtube":   "/tmp/yt_cookies.txt",
    "reddit":    "/tmp/rd_cookies.txt",
    "redgifs":   "/tmp/rg_cookies.txt",
    "twitter":   "/tmp/tw_cookies.txt",
    "facebook":  "/tmp/fb_cookies.txt",
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
        "facebook":      (FACEBOOK_COOKIES,  "facebook"),
        "facebook_ads":  (FACEBOOK_COOKIES,  "facebook"),
    }
    if platform in mapping:
        content, key = mapping[platform]
        if content:
            return _write_cookies(content, _PATHS[key])
    return None


# ═══════════════════════════════════════════════════════════════════
# Clasificación de errores y estado de cookies
# ═══════════════════════════════════════════════════════════════════

class DownloadBlocked(Exception):
    """La descarga falló por una causa clasificada (.reason dice cuál).
    media_handler la usa para responder un mensaje específico en el chat
    en lugar del genérico "no pude descargar ese contenido"."""
    def __init__(self, platform: str, reason: str):
        super().__init__(f"{platform}: {reason}")
        self.platform = platform
        self.reason   = reason


def _classify_dl_error(msg: str) -> str:
    """Mapea el mensaje de error de una descarga a una clase corta y
    accionable (para los logs y para el mensaje que ve el usuario).
    El orden de los ifs importa: de lo más específico a lo más genérico."""
    e = (msg or "").lower()
    # yt-dlp escribe "you're" con apóstrofe tipográfico (U+2019): matchear
    # "sign in to confirm you" + "bot" esquiva el problema del apóstrofe.
    if "sign in to confirm you" in e and "bot" in e:
        return "bot_check"
    if "confirm your age" in e or "age-restricted" in e or "age restricted" in e:
        return "age_gate"
    if "cookies are no longer valid" in e:
        return "cookies_invalid"
    if "private video" in e or "this video is private" in e \
            or "this account is private" in e or "protected" in e:
        return "private"
    if "members-only" in e or "join this channel" in e:
        return "members"
    if "login required" in e or "requires authentication" in e \
            or "login page" in e or "log in" in e:
        return "auth_wall"
    if "video unavailable" in e or "has been removed" in e \
            or "no longer available" in e or "content isn't available" in e:
        return "unavailable"
    if "not available in your country" in e or "geo restriction" in e \
            or "geo-restricted" in e:
        return "geo"
    if "drm" in e:
        return "drm"
    if "http error 429" in e or "too many requests" in e \
            or "rate-limit reached" in e or "rate limit" in e:
        return "rate_limit"
    if "http error 401" in e or "http error 403" in e:
        return "auth"
    if "requested format is not available" in e:
        return "no_format"
    if "unable to extract" in e or "cannot parse data" in e \
            or "empty media response" in e or "ip address is blocked" in e:
        return "blocked"
    if "timed out" in e or "connection" in e or "unable to download webpage" in e:
        return "net"
    return "other"


# Clases que NO se arreglan reintentando con otro cliente/cookies:
_FATAL_REASONS = {"unavailable", "drm", "geo"}
# Clases que SOLO puede arreglar una sesión iniciada (cookies):
_NEEDS_LOGIN   = {"private", "age_gate", "members", "auth_wall"}
# Prioridad al elegir qué razón reportar al usuario si todos los pases fallan:
_REASON_PRIORITY = ["private", "members", "age_gate", "drm", "geo", "unavailable",
                    "cookies_invalid", "bot_check", "rate_limit", "auth_wall",
                    "auth", "no_format", "net", "blocked", "other"]


def _pick_reason(reasons: list) -> str:
    for r in _REASON_PRIORITY:
        if r in reasons:
            return r
    return "other"


# Estado de las cookies POR PLATAFORMA dentro de la corrida (proceso ≤5.5 h).
# Si las cookies fallan _COOKIE_MAX_FAILS veces seguidas con clase de
# autenticación (o el sitio dice explícitamente que ya no son válidas), se
# marcan muertas y se omiten el resto de la corrida: unas cookies muertas
# nunca deben dejar el resultado peor que el modo anónimo. Al relanzarse el
# workflow (cada 5.5 h) el estado se resetea y se reintentan solas.
_COOKIE_MAX_FAILS = 3
_cookie_state = {}
_cookie_lock  = threading.Lock()


def _cookies_dead(key: str) -> bool:
    with _cookie_lock:
        return _cookie_state.get(key, {}).get("dead", False)


def _cookie_ok(key: str):
    with _cookie_lock:
        _cookie_state.setdefault(key, {"fails": 0, "dead": False})["fails"] = 0


def _cookie_fail(key: str, reason: str):
    with _cookie_lock:
        st = _cookie_state.setdefault(key, {"fails": 0, "dead": False})
        if st["dead"]:
            return
        if reason == "cookies_invalid":
            st["dead"] = True
        elif reason in ("bot_check", "auth", "auth_wall"):
            st["fails"] += 1
            if st["fails"] >= _COOKIE_MAX_FAILS:
                st["dead"] = True
        if st["dead"]:
            logger.error(
                f"⚠️ COOKIES DE {key.upper()} MUERTAS (clase={reason}): se omiten "
                f"el resto de la corrida y el bot sigue en modo anónimo. "
                f"Re-exporta las cookies y actualiza el secret para recuperarlas."
            )


# ═══════════════════════════════════════════════════════════════════
# Descarga directa
# ═══════════════════════════════════════════════════════════════════

def _discard_tmp(tmp_dir: str):
    """Borra el directorio temporal de un intento fallido. El proceso corre
    horas y los restos parciales de yt-dlp van llenando el disco del runner."""
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _download_direct_url(url: str, ext: str = "mp4") -> str | None:
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp = os.path.join(tmp_dir, f"video.{ext}")
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
    _discard_tmp(tmp_dir)
    return None


# ═══════════════════════════════════════════════════════════════════
# yt-dlp
# ═══════════════════════════════════════════════════════════════════

def _ytdlp_download(url: str, cookies: str | None, client: str | None = None
                    ) -> tuple[str | None, dict | None, str | None]:
    """Un intento de descarga de YouTube. client=None deja que yt-dlp elija
    sus clientes por defecto: los actualiza en cada release (y el pip -U de
    cada corrida los hereda), y con cookies elige otros distintos que sin
    cookies — por eso NO se fija una lista de clientes aquí.
    Devuelve (filepath, info, clase_de_error)."""
    tmp_dir = tempfile.mkdtemp()
    opts = {
        "quiet":               True,
        "no_warnings":         True,
        "merge_output_format": "mp4",
        "noplaylist":          True,
        "socket_timeout":      60,
        "nocheckcertificate":  True,
        # Los formatos progresivos ("best" en un solo archivo) casi ya no existen
        # en YouTube: hay que bajar video+audio por separado y unirlos con ffmpeg.
        # Preferimos un formato que quepa en el límite de 50MB de Telegram
        # (40M video + 8M audio); si ninguno declara tamaño o no cabe, caemos
        # a la cadena de siempre y media_handler avisa que pesa demasiado.
        "format":              ("bv*[height<=720][filesize<40M]+ba[filesize<8M]/"
                                "bv*[height<=720][filesize_approx<40M]+ba[filesize_approx<8M]/"
                                "bv*[height<=720]+ba/b[height<=720]/b"),
        "outtmpl":             os.path.join(tmp_dir, "%(id)s.%(ext)s"),
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    if PROXY:
        opts["proxy"] = PROXY
    if cookies:
        opts["cookiefile"] = cookies
    pase = "cookies" if cookies else "anon"
    try:
        logger.info(f"[youtube] pase={pase} client={client or 'default'}...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp) and os.path.getsize(fp) > 10_000:
                    logger.info(f"[youtube] pase={pase} client={client or 'default'} "
                                f"→ OK ({os.path.getsize(fp) / 1e6:.1f}MB)")
                    return fp, info, None
        _discard_tmp(tmp_dir)
        return None, None, "no_format"
    except Exception as e:
        _discard_tmp(tmp_dir)
        err_class = _classify_dl_error(str(e))
        logger.warning(f"[youtube] pase={pase} client={client or 'default'} "
                       f"clase={err_class} err={str(e)[:300]}")
        return None, None, err_class


def _try_ytdlp_all(url: str, cookies: str | None
                   ) -> tuple[str | None, dict | None, str | None]:
    """Matriz de intentos, del más sostenible al más caro:
      1. Anónimo con los clientes por defecto de yt-dlp (el PO Token que
         piden lo genera el plugin bgutil con su servidor en 127.0.0.1:4416).
      2. Anónimo con web_embedded (los videos embebibles esquivan parte del
         veto anti-bot de las IPs de datacenter).
      3. Con cookies (si existen y no están marcadas muertas), clientes por
         defecto: yt-dlp elige solo la variante con-cookies.
    El anónimo va PRIMERO a propósito: usar las cookies acelera su rotación
    (YouTube las vence en días) y unas cookies muertas jamás deben dejar el
    resultado peor que el anónimo."""
    reasons = []

    for client in (None, "web_embedded"):
        fp, info, err = _ytdlp_download(url, None, client)
        if fp:
            return fp, info, None
        if err:
            reasons.append(err)
            if err in _FATAL_REASONS:
                return None, None, err
            if err in _NEEDS_LOGIN:
                break  # otro pase anónimo no ayuda; ir directo a cookies

    if cookies and not _cookies_dead("youtube"):
        fp, info, err = _ytdlp_download(url, cookies, None)
        if fp:
            _cookie_ok("youtube")
            return fp, info, None
        if err:
            _cookie_fail("youtube", err)
            reasons.append(err)

    return None, None, _pick_reason(reasons)


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


def _redgifs_ytdlp(gif_id: str) -> tuple[str | None, dict | None]:
    """Fallback: el extractor de Redgifs de yt-dlp se mantiene al día
    y renueva el token solo cuando la API devuelve 401/403."""
    tmp_dir = tempfile.mkdtemp()
    opts = {
        "outtmpl":            os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "quiet":              True,
        "no_warnings":        True,
        "noplaylist":         True,
        "socket_timeout":     30,
        "nocheckcertificate": True,
        "format":             "best",
    }
    if PROXY:
        opts["proxy"] = PROXY
    try:
        logger.info(f"→ redgifs yt-dlp [{gif_id}]...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.redgifs.com/watch/{gif_id}",
                                    download=True) or {}
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp) and os.path.getsize(fp) > 10_000:
                    tags = info.get("tags") or info.get("categories") or []
                    return fp, {
                        "id":            gif_id,
                        "title":         (info.get("title") or "").strip(),
                        "uploader":      info.get("uploader") or "",
                        "tags":          tags,
                        "description":   " ".join(f"#{t}" for t in tags),
                        "webpage_url":   f"https://www.redgifs.com/watch/{gif_id}",
                        "extractor_key": "RedGifs",
                        "ext":           os.path.splitext(fp)[1].lstrip("."),
                    }
    except Exception as e:
        logger.error(f"[redgifs] pase=anon clase={_classify_dl_error(str(e))} "
                     f"err={str(e)[:200]}")
    _discard_tmp(tmp_dir)
    return None, None


def download_redgifs(url: str) -> tuple[str | None, dict | None]:
    """Descarga un video de redgifs.com/watch/<id> via API con proxy."""
    m = re.search(r"redgifs\.com/(?:watch|ifr)/([a-zA-Z0-9]+)", url)
    if not m:
        return None, None

    gif_id = m.group(1).lower()
    # Sesión con huella TLS de Chrome real: Redgifs está tras Cloudflare y
    # suele rechazar (403) la huella de requests desde IPs de datacenter.
    if _HAS_CFFI:
        session = cffi_requests.Session(impersonate=FB_IMPERSONATE)
    else:
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
            return _redgifs_ytdlp(gif_id)

        ext = "mp4" if ".mp4" in video_url else "gif"
        dl_headers = {**api_headers, "Accept": "*/*"}
        tmp_dir = tempfile.mkdtemp()
        tmp = os.path.join(tmp_dir, f"redgifs.{ext}")
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
        _discard_tmp(tmp_dir)

    except Exception as e:
        logger.error(f"[redgifs] api-nativa clase={_classify_dl_error(str(e))} err={e}")

    # La API nativa falló (token, 401/403, tamaño): probar con yt-dlp
    return _redgifs_ytdlp(gif_id)


# ═══════════════════════════════════════════════════════════════════
# Twitter / X  ──  descarga completa (video + imagen)
# ═══════════════════════════════════════════════════════════════════

def _fxtwitter_info(tweet_id: str) -> dict | None:
    """Consulta fxtwitter/vxtwitter y devuelve el objeto tweet o None."""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Accept":     "application/json",
    }
    for fx_host in ("api.fxtwitter.com", "api.vxtwitter.com"):
        try:
            r = requests.get(
                f"https://{fx_host}/status/{tweet_id}",
                headers=hdrs, timeout=20, verify=False,
            )
            if r.status_code == 200:
                data = r.json()
                tweet = data.get("tweet") or data.get("data") or {}
                if not tweet and "media_extended" in data:
                    # vxtwitter responde un objeto plano (sin clave "tweet"):
                    # adaptarlo al formato de fxtwitter que espera el resto.
                    tweet = {
                        "text": data.get("text", ""),
                        "media": {"all": [
                            {"type": m.get("type"), "url": m.get("url")}
                            for m in (data.get("media_extended") or [])
                            if m.get("url")
                        ]},
                    }
                if tweet:
                    logger.info(f"fxtwitter [{fx_host}] OK para {tweet_id}")
                    return tweet
            else:
                logger.warning(f"fxtwitter [{fx_host}]: {r.status_code}")
        except Exception as e:
            logger.warning(f"fxtwitter [{fx_host}]: {e}")
    return None


def download_twitter(url: str) -> tuple[str | None, dict | None]:
    """
    Descarga video o imagen de un tweet.
    Orden de intentos:
      1. fxtwitter API → video directo
      2. fxtwitter API → imagen
      3. yt-dlp con cookies (fallback)
      4. _download_twitter_image (último recurso)
    """
    m = re.search(r"status/(\d+)", url)
    if not m:
        return None, None
    tweet_id = m.group(1)

    base_info = {
        "id":            tweet_id,
        "title":         f"Tweet {tweet_id}",
        "description":   "",
        "webpage_url":   url,
        "extractor_key": "Twitter",
    }

    # ── 1. fxtwitter: intentar video primero ──────────────────────
    tweet = _fxtwitter_info(tweet_id)
    if tweet:
        text  = tweet.get("text", "")
        media = tweet.get("media", {}) or {}

        # Videos — fxtwitter devuelve lista en media.videos
        videos = media.get("videos") or []
        if not videos:
            # Algunos endpoints mezclan todo en media.all
            videos = [
                item for item in (media.get("all") or [])
                if item.get("type") in ("video", "gif")
            ]

        for vid in videos:
            # fxtwitter da la URL directa del mp4 en .url
            vid_url = vid.get("url") or vid.get("variants", [{}])[0].get("url", "")
            if not vid_url:
                continue
            logger.info(f"fxtwitter video URL: {vid_url[:80]}")
            fp = _download_direct_url(vid_url, "mp4")
            if fp:
                return fp, {
                    **base_info,
                    "title":       text[:100] or base_info["title"],
                    "description": text,
                    "ext":         "mp4",
                }

        # ── 2. fxtwitter: imagen ──────────────────────────────────
        photos = media.get("photos") or [
            i for i in (media.get("all") or [])
            if i.get("type") in ("photo", "image")
        ]
        if photos:
            img_url = photos[0].get("url", "")
            if img_url:
                all_images = [p.get("url", "") for p in photos if p.get("url")]
                fp = _dl_image(img_url, "jpg")
                if fp:
                    return fp, {
                        **base_info,
                        "title":       text[:100] or base_info["title"],
                        "description": text,
                        "ext":         "jpg",
                        "all_images":  all_images,
                    }

    # ── 3. yt-dlp: con cookies primero (si existen y están vivas); si el
    #      fallo es de autenticación, se reintenta una vez sin cookies ──
    #      (fxtwitter, que es anónimo, ya falló arriba: aquí las cookies
    #      son la vía fuerte, pero muertas no deben bloquear el anónimo).
    logger.info("fxtwitter sin resultado, probando yt-dlp para Twitter...")
    cookies = _cookies("twitter")
    if cookies and _cookies_dead("twitter"):
        cookies = None
    reasons = []
    for ck in ([cookies, None] if cookies else [None]):
        fp, info, err = _ytdlp_generic(url, "twitter", ck)
        if fp:
            if ck:
                _cookie_ok("twitter")
            return fp, info
        if err:
            reasons.append(err)
            if ck:
                _cookie_fail("twitter", err)
            if err in _FATAL_REASONS:
                break

    # ── 4. Último recurso: imagen vía todos los métodos disponibles ──
    logger.info("yt-dlp falló, intentando _download_twitter_image...")
    fp, info = _download_twitter_image(url)
    if fp:
        return fp, info
    raise DownloadBlocked("twitter", _pick_reason(reasons))


# ═══════════════════════════════════════════════════════════════════
# YouTube
# ═══════════════════════════════════════════════════════════════════

def download_youtube(url: str, platform: str) -> tuple[str | None, dict | None]:
    # yt-dlp directo. Cobalt se quitó de este flujo: sus instancias públicas
    # ya no aceptan peticiones sin API key y solo metían ~2 min de espera.
    cookies = _cookies(platform)
    fp, info, reason = _try_ytdlp_all(url, cookies)
    if fp:
        return fp, info
    raise DownloadBlocked("youtube", reason or "other")


# ═══════════════════════════════════════════════════════════════════
# Facebook Ads Library  ──  descarga por ID de anuncio
# ═══════════════════════════════════════════════════════════════════

def _extract_fb_ad_id(url: str) -> str | None:
    """Extrae el ID del anuncio de una URL de Facebook Ads Library."""
    m = re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else None


# curl_cffi imita la huella TLS de un navegador real (Chrome). Facebook bloquea
# (403) las peticiones de `requests` por su huella de cliente, así que cuando esté
# disponible la usamos para el scraping; si no, caemos a requests normal.
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except Exception:
    _HAS_CFFI = False

FB_IMPERSONATE = os.environ.get("FB_IMPERSONATE", "chrome124")

# Proxy dedicado para Facebook. Tiene prioridad sobre PROXY_URL global.
#   - Una URL de proxy  → se usa esa (ideal: residencial de México)
#   - "direct"/"none"   → conexión directa, SIN proxy (IP nativa de Streamlit)
#   - vacío             → usa PROXY_URL global (comportamiento anterior)
FB_PROXY_URL = os.environ.get("FB_PROXY_URL", "").strip()


def _fb_proxy() -> str:
    if FB_PROXY_URL:
        if FB_PROXY_URL.lower() in ("direct", "none", "off"):
            return ""
        return FB_PROXY_URL
    return PROXY


def _fb_proxies_dict() -> dict:
    p = _fb_proxy()
    return {"http": p, "https": p} if p else {}


def _fb_cookie_dict() -> dict:
    """Lee el archivo de cookies de Facebook (Netscape) a un dict {nombre: valor}."""
    path = _cookies("facebook_ads")
    cookies = {}
    if path and os.path.exists(path):
        try:
            cj = http.cookiejar.MozillaCookieJar()
            cj.load(path, ignore_discard=True, ignore_expires=True)
            for c in cj:
                cookies[c.name] = c.value
        except Exception as e:
            logger.warning(f"_fb_cookie_dict: {e}")
    return cookies


def _fb_get(url: str, cookies: dict, headers: dict, timeout: int = 30):
    """
    GET a Facebook imitando un navegador real con curl_cffi (si está disponible).
    Devuelve un objeto respuesta con .status_code, .text y .content.
    """
    if _HAS_CFFI:
        kw = {
            "headers":     headers,
            "timeout":     timeout,
            "impersonate": FB_IMPERSONATE,
            "verify":      False,
        }
        if cookies:
            kw["cookies"] = cookies
        proxies = _fb_proxies_dict()
        if proxies:
            kw["proxies"] = proxies
        return cffi_requests.get(url, **kw)
    # Fallback: requests normal (más propenso al 403)
    sess = requests.Session()
    if cookies:
        sess.cookies.update(cookies)
    return sess.get(url, headers=headers, timeout=timeout,
                    proxies=_fb_proxies_dict(), verify=False)


def _fb_session() -> tuple[dict, str | None]:
    """
    Carga las cookies de Facebook y devuelve (cookie_dict, ruta).
    El cookie_dict se usa con _fb_get (curl_cffi). Las cookies de una sesión
    iniciada + huella de navegador real son lo que normalmente evita el 403.
    """
    cookies_path = _cookies("facebook_ads")
    cookies = _fb_cookie_dict()
    if cookies:
        logger.info(f"fb_ads: cookies cargadas ({len(cookies)} entradas) "
                    f"| curl_cffi={'sí' if _HAS_CFFI else 'no'}")
    else:
        logger.info(f"fb_ads: sin cookies de login | curl_cffi={'sí' if _HAS_CFFI else 'no'}")
    return cookies, cookies_path


def _fb_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def _fb_unescape(u: str) -> str:
    return u.replace("\\u0026", "&").replace("\\/", "/").replace("&amp;", "&")


def _fb_download(cookies: dict, url: str, ext: str = "mp4") -> str | None:
    """Descarga un recurso (video/imagen) del CDN de Facebook."""
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp = os.path.join(tmp_dir, f"fbad.{ext}")
        hdrs = {
            "User-Agent": _fb_headers()["User-Agent"],
            "Accept": "*/*",
            "Referer": "https://www.facebook.com/ads/library/",
        }
        if _HAS_CFFI:
            kw = {"headers": hdrs, "timeout": 120, "impersonate": FB_IMPERSONATE, "verify": False}
            if cookies:
                kw["cookies"] = cookies
            proxies = _fb_proxies_dict()
            if proxies:
                kw["proxies"] = proxies
            r = cffi_requests.get(url, **kw)
            r.raise_for_status()
            with open(tmp, "wb") as f:
                f.write(r.content)
        else:
            sess = requests.Session()
            if cookies:
                sess.cookies.update(cookies)
            with sess.get(url, headers=hdrs, stream=True, timeout=120,
                          proxies=_fb_proxies_dict(), verify=False) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        f.write(chunk)
        min_size = 10_000   # 10 KB mínimo para video e imagen
        if os.path.getsize(tmp) > min_size:
            return tmp
    except Exception as e:
        logger.warning(f"_fb_download: {e}")
    _discard_tmp(tmp_dir)
    return None


def _scrape_fb_ads_html(ad_id: str, cookies: dict) -> dict | None:
    """
    Descarga el HTML de la página del anuncio y extrae del JSON embebido TODAS las
    URLs de video e imagen disponibles, además de metadatos básicos.
    Devuelve un dict {videos: [...], images: [...], snapshot: {...}} o None.
    """
    page_url = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country=ALL&id={ad_id}"
    )
    try:
        r = _fb_get(page_url, cookies, _fb_headers(), timeout=30)
        if r.status_code != 200:
            via = _fb_proxy() or "directo (sin proxy)"
            snippet = (r.text or "")[:400].replace("\n", " ").replace("\r", " ")
            logger.warning(f"fb_ads scrape: status {r.status_code} | vía {via}")
            logger.warning(f"fb_ads 403 body[:400]: {snippet}")
            return None
        html = r.text

        # ── Videos: hd primero, sd después ────────────────────────────
        videos = []
        for pat in (r'"video_hd_url"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"',
                    r'"video_sd_url"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"'):
            for m in re.findall(pat, html):
                videos.append(_fb_unescape(m))

        # ── Imágenes: preferir original; usar resized solo si no hay original ──
        # (Facebook da original_image_url y resized_image_url de la MISMA foto,
        #  así que tomar ambas duplicaría cada imagen.)
        originals = [_fb_unescape(m) for m in
                     re.findall(r'"original_image_url"\s*:\s*"(https:[^"]+?)"', html)]
        resized   = [_fb_unescape(m) for m in
                     re.findall(r'"resized_image_url"\s*:\s*"(https:[^"]+?)"', html)]
        images = [u for u in (originals or resized) if ".mp4" not in u]

        # Dedupe conservando el orden
        videos = list(dict.fromkeys(videos))
        images = list(dict.fromkeys(images))

        # ── Metadatos ─────────────────────────────────────────────────
        snapshot = {}
        m = re.search(r'"page_name"\s*:\s*"([^"]+)"', html)
        if m:
            snapshot["page_name"] = _fb_unescape(m.group(1))
        m = re.search(r'"body"\s*:\s*\{\s*"text"\s*:\s*"([^"]*)"', html)
        if m:
            snapshot["body_text"] = _fb_unescape(m.group(1)).replace("\\n", "\n")

        if not videos and not images:
            logger.warning(f"fb_ads scrape: HTML OK pero sin media para {ad_id}")
            return None

        logger.info(f"fb_ads scrape: {len(videos)} video(s), {len(images)} imagen(es)")
        return {"videos": videos, "images": images, "snapshot": snapshot}

    except Exception as e:
        logger.warning(f"_scrape_fb_ads_html: {e}")
        return None


# ── Instala Chromium una sola vez si no está ──────────────────────────────────
def _ensure_playwright():
    """Instala Chromium si no existe en disco. Solo se ejecuta una vez por container."""
    import glob, pathlib
    # Verificar si ya existe el ejecutable en disco (persiste durante la sesión)
    cache = pathlib.Path.home() / ".cache" / "ms-playwright"
    shells = list(cache.glob("chromium*/chrome-headless-shell-linux64/chrome-headless-shell"))
    chromes = list(cache.glob("chromium*/chrome-linux64/chrome"))
    if shells or chromes:
        return   # Ya instalado, no re-descargar
    logger.info("fb_ads: instalando Chromium (sin --with-deps)...")
    ret = os.system(f'"{sys.executable}" -m playwright install chromium 2>&1')
    logger.info(f"fb_ads: playwright install terminó con código {ret}")


def _playwright_worker(ad_id: str, cookies: dict) -> dict | None:
    """
    Ejecuta el scraping con Playwright Async API dentro de su propio event loop.
    Debe llamarse desde un hilo (ThreadPoolExecutor), nunca desde el loop de asyncio.
    """
    import asyncio
    from playwright.async_api import async_playwright

    async def _run():
        videos, images, snapshot = [], [], {}
        net_videos = []
        page_url = (
            f"https://www.facebook.com/ads/library/"
            f"?active_status=active&ad_type=all&country=ALL&id={ad_id}"
        )

        def _on_response(response):
            url = response.url
            # Solo capturar videos de la red; las imágenes las extraemos del HTML
            if "fbcdn.net" in url and ".mp4" in url:
                net_videos.append(url)

        async with async_playwright() as p:
            launch_opts = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            fb_proxy = _fb_proxy()
            if fb_proxy:
                launch_opts["proxy"] = {"server": fb_proxy}

            browser = await p.chromium.launch(**launch_opts)
            ctx = await browser.new_context(
                user_agent=_fb_headers()["User-Agent"],
                locale="es-MX",
                viewport={"width": 1280, "height": 800},
            )
            if cookies:
                await ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".facebook.com", "path": "/"}
                    for k, v in cookies.items()
                ])

            page = await ctx.new_page()
            page.on("response", _on_response)

            logger.info(f"fb_ads playwright: navegando a {page_url[:80]}...")
            # "networkidle" nunca se dispara en facebook.com (long-polling):
            # usamos domcontentloaded + espera fija, y si el goto expira
            # seguimos con lo que haya cargado en vez de abortar.
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as nav_err:
                logger.warning(f"fb_ads playwright goto: {nav_err}")
            await page.wait_for_timeout(5_000)

            html = await page.content()
            await browser.close()

        # Extraer del HTML renderizado (específico del anuncio, no UI)
        for pat in (r'"video_hd_url"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"',
                    r'"video_sd_url"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"'):
            for m in re.findall(pat, html):
                videos.append(_fb_unescape(m))
        # Si HTML no dio videos, usar los capturados de la red como respaldo
        if not videos:
            videos = net_videos

        # ── Extraer SOLO del bloque JSON del anuncio específico ──────────────
        # La página muestra TODOS los anuncios del anunciante; usamos el ad_id
        # para encontrar el bloque JSON de este anuncio específico y extraer
        # solo su imagen/video, no los de todos los demás.
        def _extract_for_ad(html: str, ad_id: str):
            vid, img = [], []
            # Encontrar posición del ad_id en el HTML y tomar ventana de 8000 chars
            pos = html.find(f'"{ad_id}"')
            if pos == -1:
                pos = html.find(ad_id)
            if pos != -1:
                window = html[max(0, pos - 500): pos + 8000]
            else:
                window = html   # fallback: buscar en todo el HTML

            for pat in (r'"video_hd_url"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"',
                        r'"video_sd_url"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"'):
                for m in re.findall(pat, window):
                    vid.append(_fb_unescape(m))
            originals = [_fb_unescape(m) for m in
                         re.findall(r'"original_image_url"\s*:\s*"(https:[^"]+?)"', window)]
            resized   = [_fb_unescape(m) for m in
                         re.findall(r'"resized_image_url"\s*:\s*"(https:[^"]+?)"', window)]
            img = [u for u in (originals or resized) if ".mp4" not in u]
            return list(dict.fromkeys(vid)), list(dict.fromkeys(img))

        videos, images = _extract_for_ad(html, ad_id)
        if not videos:
            videos = list(dict.fromkeys(net_videos))

        meta = re.search(r'"page_name"\s*:\s*"([^"]+)"', html)
        if meta:
            snapshot["page_name"] = _fb_unescape(meta.group(1))

        if not videos and not images:
            logger.warning("fb_ads playwright: página cargó pero sin media en el JSON")
            return None
        logger.info(f"fb_ads playwright: {len(videos)} video(s), {len(images)} imagen(es)")
        return {"videos": videos, "images": images, "snapshot": snapshot}

    return asyncio.run(_run())


def _scrape_fb_ads_playwright(ad_id: str, cookies: dict) -> dict | None:
    """
    Lanza _playwright_worker en un hilo dedicado para evitar el conflicto
    'Sync API inside asyncio loop' que ocurre cuando el bot ya tiene un loop activo.
    """
    _ensure_playwright()
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_playwright_worker, ad_id, cookies)
            return future.result(timeout=90)
    except Exception as e:
        logger.warning(f"_scrape_fb_ads_playwright: {e}")
        return None

def _try_fb_ads_ytdlp(ad_id: str, cookies: str | None) -> tuple[str | None, dict | None]:
    """Fallback: intenta descargar el video con yt-dlp (solo sirve para video)."""
    page_url = f"https://www.facebook.com/ads/library/?id={ad_id}"
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
    }
    if PROXY:
        opts["proxy"] = PROXY
    if cookies:
        opts["cookiefile"] = cookies
    try:
        logger.info(f"→ yt-dlp fb_ads [{ad_id}]...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=True)
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp) and os.path.getsize(fp) > 10_000:
                    return fp, info
    except Exception as e:
        logger.warning(f"_try_fb_ads_ytdlp: {str(e)[:140]}")
    _discard_tmp(tmp_dir)
    return None, None


def download_facebook_ads(url: str) -> tuple[str | list[str] | None, dict | None]:
    """
    Descarga el contenido (video O imágenes) de un anuncio de Facebook Ads Library.

    Devuelve:
      - (str, info)        → un solo video o una sola imagen
      - (list[str], info)  → varias imágenes (anuncio carrusel)
      - (None, None)       → error

    Orden de intentos:
      1. Scraping HTML con sesión + cookies → URLs directas de video/imagen
      2. yt-dlp (solo video)
      3. Cobalt (solo video, fallback genérico)
    """
    ad_id = _extract_fb_ad_id(url)
    if not ad_id:
        logger.warning(f"download_facebook_ads: no se pudo extraer ID de {url}")
        return None, None

    base_info = {
        "id":            ad_id,
        "title":         f"Anuncio Facebook #{ad_id}",
        "description":   "",
        "webpage_url":   f"https://www.facebook.com/ads/library/?id={ad_id}",
        "extractor_key": "FacebookAds",
    }

    cookies, cookies_path = _fb_session()

    # ── Intento 1: yt-dlp (solo video) ────────────────────────────────
    # Va primero: desde ene-2026 Facebook exige un challenge de JS en
    # /ads/library que rompe el scraping por requests; el extractor
    # FacebookAds de yt-dlp actualizado ya resuelve ese challenge.
    logger.info(f"→ fb_ads yt-dlp para ID {ad_id}...")
    fp, info = _try_fb_ads_ytdlp(ad_id, cookies_path)
    if fp:
        if info:
            info.setdefault("extractor_key", "FacebookAds")
            info.setdefault("type", "video")
        return fp, info or {**base_info, "type": "video"}

    # ── Intento 2: Scraping HTML con curl_cffi ────────────────────────
    logger.info(f"→ fb_ads scraping HTML para ID {ad_id}...")
    scraped = _scrape_fb_ads_html(ad_id, cookies)

    # ── Intento 3: Playwright (navegador real, resuelve JS challenge) ──
    # Único camino para anuncios de imagen/carrusel si el scraping falla.
    if not scraped:
        logger.info(f"→ fb_ads Playwright para ID {ad_id}...")
        scraped = _scrape_fb_ads_playwright(ad_id, cookies)

    if scraped:
        snap = scraped.get("snapshot", {})
        if snap.get("page_name"):
            base_info["title"]     = f"Anuncio de {snap['page_name']} #{ad_id}"
            base_info["page_name"] = snap["page_name"]
        if snap.get("body_text"):
            base_info["description"] = snap["body_text"]

        # 1a. Video (preferimos hd, ya viene ordenado)
        for v_url in scraped["videos"]:
            logger.info(f"fb_ads: video URL → {v_url[:80]}...")
            fp = _fb_download(cookies, v_url, "mp4")
            if fp:
                return fp, {**base_info, "ext": "mp4", "type": "video"}

        # 1b. Imágenes (anuncio de foto estática o carrusel)
        files = []
        for i_url in scraped["images"]:
            fp = _fb_download(cookies, i_url, "jpg")
            if fp:
                files.append(fp)
        if files:
            info = {**base_info, "ext": "jpg",
                    "type": "gallery" if len(files) > 1 else "image",
                    "count": len(files)}
            return (files if len(files) > 1 else files[0]), info

    logger.error(f"download_facebook_ads: todos los métodos fallaron para {ad_id}")
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Threads  ──  yt-dlp NO tiene extractor de Threads: descarga propia
# ═══════════════════════════════════════════════════════════════════

def download_threads(url: str) -> tuple[str | None, dict | None]:
    """Baja el HTML del post (huella de Chrome real) y extrae el video o la
    imagen del JSON embebido. threads.com es el dominio canónico desde 2025."""
    url = re.sub(r"https?://(www\.)?threads\.(com|net)", "https://www.threads.com", url)
    info = {
        "id": "", "title": "Threads post", "description": "",
        "webpage_url": url, "extractor_key": "Threads",
    }
    try:
        if _HAS_CFFI:
            r = cffi_requests.get(url, impersonate=FB_IMPERSONATE, timeout=30,
                                  headers={"Accept-Language": "en"}, verify=False)
        else:
            r = requests.get(url, headers=_fb_headers(), timeout=30,
                             proxies=PROXIES, verify=False)
        html = r.text

        cap = re.search(r'"caption"\s*:\s*\{[^}]*?"text"\s*:\s*"([^"]*)"', html)
        if cap:
            info["description"] = _fb_unescape(cap.group(1)).replace("\\n", "\n")

        m = re.search(r'"video_versions"\s*:\s*\[\s*\{[^}]*?"url"\s*:\s*"([^"]+)"', html)
        if m:
            fp = _download_direct_url(_fb_unescape(m.group(1)), "mp4")
            if fp:
                info["ext"] = "mp4"
                return fp, info

        m = re.search(r'"image_versions2"\s*:\s*\{\s*"candidates"\s*:\s*\[\s*\{[^}]*?"url"\s*:\s*"([^"]+)"', html)
        if m:
            fp = _dl_image(_fb_unescape(m.group(1)), "jpg")
            if fp:
                info["ext"] = "jpg"
                return fp, info
        logger.warning("download_threads: sin media en el HTML (¿post privado o login wall?)")
    except Exception as e:
        logger.error(f"download_threads: {e}")
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Instagram  ──  fallback de fotos vía página de embed
# ═══════════════════════════════════════════════════════════════════

def _instagram_image_fallback(url: str) -> tuple[str | None, dict | None]:
    """El extractor de Instagram de yt-dlp solo saca VIDEO; para posts de foto
    intentamos la página de embed, que es menos estricta con el login."""
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None, None
    shortcode = m.group(1)
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        if _HAS_CFFI:
            r = cffi_requests.get(embed_url, impersonate=FB_IMPERSONATE,
                                  timeout=30, verify=False)
        else:
            r = requests.get(embed_url, headers=_fb_headers(), timeout=30,
                             proxies=PROXIES, verify=False)
        html = r.text
        img_url = None
        m2 = re.search(r'"display_url"\s*:\s*"([^"]+)"', html)
        if m2:
            img_url = _fb_unescape(m2.group(1))
        else:
            m2 = re.search(r'class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html)
            if m2:
                img_url = _fb_unescape(m2.group(1))
        if img_url:
            fp = _dl_image(img_url, "jpg")
            if fp:
                return fp, {
                    "id": shortcode, "title": "Instagram post", "description": "",
                    "webpage_url": f"https://www.instagram.com/p/{shortcode}/",
                    "extractor_key": "Instagram", "ext": "jpg",
                }
    except Exception as e:
        logger.warning(f"_instagram_image_fallback: {e}")
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Descargador genérico
# ═══════════════════════════════════════════════════════════════════

def download_media(url: str, platform: str = None) -> tuple[str | None, dict | None]:
    # Threads no tiene extractor en yt-dlp: descargador propio
    if platform == "threads" or "threads.com" in url or "threads.net" in url:
        return download_threads(url)

    # Links /share/ de Instagram (botón compartir de la app): yt-dlp los
    # rechaza; hay que resolver el redirect a la URL canónica primero.
    if "instagram.com/share/" in url:
        try:
            if _HAS_CFFI:
                rr = cffi_requests.get(url, impersonate=FB_IMPERSONATE,
                                       timeout=20, allow_redirects=True, verify=False)
            else:
                rr = requests.get(url, headers=_HEADERS, timeout=20,
                                  allow_redirects=True, verify=False)
            url = str(rr.url)
            logger.info(f"IG share resuelto a: {url[:80]}")
        except Exception as e:
            logger.warning(f"resolviendo share de IG: {e}")

    if platform in ("youtube_short", "youtube_long"):
        return download_youtube(url, platform)

    # Facebook Ads Library: flujo especial por scraping + yt-dlp
    if platform == "facebook_ads" or "facebook.com/ads/library" in url:
        return download_facebook_ads(url)

    # Twitter/X: usa fxtwitter primero (evita bloqueos de API)
    if platform == "twitter" or "x.com" in url or "twitter.com" in url:
        return download_twitter(url)

    # Redgifs: usa API nativa con proxy, no yt-dlp
    if platform == "redgifs" or "redgifs.com" in url:
        return download_redgifs(url)

    return _ytdlp_generic_matrix(url, platform)


# Plataformas donde el intento CON cookies va primero: desde una IP de
# datacenter el anónimo casi siempre topa con el login wall, así que probar
# cookies primero ahorra un intento inútil. La garantía se conserva igual:
# si el pase con cookies falla por autenticación, SIEMPRE se reintenta
# anónimo (unas cookies muertas nunca dejan el resultado peor que sin ellas).
_COOKIE_FIRST = {"instagram", "facebook"}


def _ytdlp_generic(url: str, platform: str, cookies: str | None
                   ) -> tuple[str | None, dict | None, str | None]:
    """Un intento del camino genérico (Instagram/TikTok/Facebook/otros).
    Devuelve (filepath, info, clase_de_error)."""
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
    if cookies:
        opts["cookiefile"] = cookies

    # Huella TLS de Chrome real: sin esto Facebook responde "Cannot parse
    # data" y v.redd.it da 403 (fingerprinting de Cloudflare).
    if _HAS_CFFI:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception:
            pass

    pase = "cookies" if cookies else "anon"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp):
                    logger.info(f"[{platform}] pase={pase} → OK")
                    return fp, info, None
        _discard_tmp(tmp_dir)
        return None, None, "no_format"
    except Exception as e:
        _discard_tmp(tmp_dir)
        err_class = _classify_dl_error(str(e))
        logger.warning(f"[{platform}] pase={pase} clase={err_class} err={str(e)[:300]}")
        return None, None, err_class


def _ytdlp_generic_matrix(url: str, platform: str) -> tuple[str | None, dict | None]:
    """Matriz anónimo/cookies del camino genérico + fallbacks históricos.
    Si todo falla, lanza DownloadBlocked con la razón clasificada."""
    cookies = _cookies(platform)
    if cookies and _cookies_dead(platform):
        cookies = None

    if not cookies:
        order = [None]
    elif platform in _COOKIE_FIRST:
        order = [cookies, None]
    else:
        order = [None, cookies]

    reasons = []
    for ck in order:
        fp, info, err = _ytdlp_generic(url, platform, ck)
        if fp:
            if ck:
                _cookie_ok(platform)
            return fp, info
        if err:
            reasons.append(err)
            if ck:
                _cookie_fail(platform, err)
            if err in _FATAL_REASONS:
                break

    # Twitter: si no hay video, intentar la URL directa de la imagen
    if platform == "twitter":
        fp, info = _get_twitter_image_url(url)
        if fp:
            return fp, info
        fp, info = _download_twitter_image(url)
        if fp:
            return fp, info

    # Instagram: yt-dlp solo saca video; para posts de foto probamos el embed
    if platform == "instagram":
        fp, info = _instagram_image_fallback(url)
        if fp:
            return fp, info

    raise DownloadBlocked(platform or "media", _pick_reason(reasons))


def _get_twitter_image_url(url: str) -> tuple[str | None, dict | None]:
    """
    Obtiene la imagen de un tweet usando fxtwitter (no requiere auth)
    como método principal, con syndication como fallback.
    Retorna ("URL:https://...", info) cuando tiene éxito.
    """
    m = re.search(r"status/(\d+)", url)
    tweet_id = m.group(1) if m else None
    if not tweet_id:
        return None, None

    base_info = {
        "id":            tweet_id,
        "title":         f"Tweet {tweet_id}",
        "description":   "",
        "webpage_url":   url,
        "extractor_key": "Twitter",
    }

    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Accept":     "application/json",
    }

    # ── Método 1: fxtwitter/vxtwitter — no requiere cookies ni query IDs ──
    tweet = _fxtwitter_info(tweet_id)
    if tweet:
        text  = tweet.get("text", "")
        media = tweet.get("media", {}) or {}
        photos = media.get("photos") or [
            item for item in (media.get("all") or [])
            if item.get("type") in ("photo", "image")
        ]
        if photos:
            img_url = photos[0].get("url", "")
            if img_url:
                all_images = [p.get("url", "") for p in photos if p.get("url")]
                info = {
                    **base_info,
                    "title":       text[:100] or base_info["title"],
                    "description": text,
                    "ext":         "jpg",
                    "all_images":  all_images,
                }
                return f"URL:{img_url}", info

    # ── Método 2: Syndication API (fallback) ────────────────────────
    try:
        import math as _math
        val   = (int(tweet_id) / 1e15) * _math.pi
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        ip    = int(abs(val))
        is_   = ""
        if ip == 0:
            is_ = "0"
        else:
            t = ip
            while t:
                is_ = chars[t % 36] + is_
                t //= 36
        fp_   = abs(val) - ip
        fs_   = ""
        for _ in range(8):
            fp_ *= 36
            d    = min(int(fp_), 35)
            fs_ += chars[d]
            fp_ -= d
        token = (is_ + "." + fs_).rstrip("0").rstrip(".")

        r = requests.get(
            f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token={token}",
            headers={**hdrs, "Referer": "https://platform.twitter.com/",
                     "Origin": "https://platform.twitter.com"},
            timeout=20, verify=False,
        )
        if r.status_code == 200:
            data = r.json()
            for item in (data.get("mediaDetails") or []):
                if item.get("type") == "photo":
                    img_url = item.get("media_url_https", "")
                    if img_url:
                        text = data.get("text", "")
                        info = {
                            **base_info,
                            "title":       text[:100] or base_info["title"],
                            "description": text,
                            "ext":         "jpg",
                        }
                        return f"URL:{img_url}?name=large", info
        else:
            logger.warning(f"syndication fallback: {r.status_code}")
    except Exception as e:
        logger.warning(f"_get_twitter_image_url syndication: {e}")

    return None, None


def _download_twitter_image(url: str) -> tuple[str | None, dict | None]:
    """Fallback para tweets que solo tienen imagen (sin video)."""
    m = re.search(r"status/(\d+)", url)
    tweet_id = m.group(1) if m else "tweet"

    base_info = {
        "id":            tweet_id,
        "title":         f"Tweet {tweet_id}",
        "description":   "",
        "webpage_url":   url,
        "extractor_key": "Twitter",
    }

    # ── Intento 0: fxtwitter/vxtwitter — descarga directa de la imagen ──
    # Este es el método más confiable y no requiere cookies ni auth
    tweet = _fxtwitter_info(tweet_id)
    if tweet:
        text   = tweet.get("text", "")
        media  = tweet.get("media", {}) or {}
        photos = media.get("photos") or [
            i for i in (media.get("all") or [])
            if i.get("type") in ("photo", "image")
        ]
        if photos:
            img_url = photos[0].get("url", "")
            if img_url:
                fp = _dl_image(img_url, "jpg")
                if fp:
                    return fp, {
                        **base_info,
                        "title":       text[:100] or base_info["title"],
                        "description": text,
                        "ext":         "jpg",
                        "all_images":  [p.get("url", "") for p in photos if p.get("url")],
                    }

    cookies = _cookies("twitter")

    # ── Intento 1: yt-dlp con write_thumbnail ─────────────────────────
    # yt-dlp puede escribir el thumbnail al disco aunque no haya video
    try:
        tmp_dir = tempfile.mkdtemp()
        opts = {
            "quiet":              True,
            "no_warnings":        True,
            "skip_download":      True,
            "writethumbnail":     True,
            "outtmpl":            os.path.join(tmp_dir, "%(id)s.%(ext)s"),
            "nocheckcertificate": True,
        }
        if PROXY:
            opts["proxy"] = PROXY
        if cookies:
            opts["cookiefile"] = cookies

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
            except Exception:
                info = None

        # Buscar cualquier imagen descargada en tmp_dir
        for fname in os.listdir(tmp_dir):
            fpath = os.path.join(tmp_dir, fname)
            ext = fname.rsplit(".", 1)[-1].lower()
            if ext in ("jpg", "jpeg", "png", "webp") and os.path.getsize(fpath) > 1000:
                result_info = info or base_info
                result_info["ext"] = ext
                # Asegurar que description está presente para el formatter
                if "description" not in result_info:
                    result_info["description"] = result_info.get("title", "")
                return fpath, result_info
        _discard_tmp(tmp_dir)

    except Exception as e:
        logger.warning(f"_download_twitter_image (writethumbnail): {e}")

    # ── Intento 2: yt-dlp extract_info ignorando error No video ───────
    # El error se lanza DESPUÉS de obtener el info, así que lo capturamos
    try:
        captured_info = {}

        class _InfoCapture(yt_dlp.YoutubeDL):
            def extract_info(self, url, download=True, **kw):
                try:
                    return super().extract_info(url, download=download, **kw)
                except Exception as exc:
                    # captured_info se llena en process_ie_result; YoutubeDL
                    # no tiene ningún atributo _last_info.
                    if "No video" in str(exc) and captured_info:
                        return captured_info
                    raise
            def process_ie_result(self, ie_result, download=True, extra_info=None):
                captured_info.update(ie_result)
                return super().process_ie_result(ie_result, download=download, extra_info=extra_info)

        opts = {
            "quiet":              True,
            "no_warnings":        True,
            "skip_download":      True,
            "nocheckcertificate": True,
        }
        if PROXY:
            opts["proxy"] = PROXY
        if cookies:
            opts["cookiefile"] = cookies

        try:
            with _InfoCapture(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            info = captured_info if captured_info else None

        if info:
            thumbnails = info.get("thumbnails") or []
            thumbnail  = info.get("thumbnail", "")
            # Buscar la imagen más grande en thumbnails
            best_url = ""
            best_w   = 0
            for t in thumbnails:
                w = t.get("width", 0) or 0
                u = t.get("url", "")
                if u and w >= best_w:
                    best_w   = w
                    best_url = u
            if not best_url:
                best_url = thumbnail

            if best_url:
                img_url = best_url
                if "pbs.twimg.com" in best_url:
                    img_url = re.sub(r"\?.*$", "", best_url) + "?format=jpg&name=large"
                fp = _dl_image(img_url, "jpg")
                if fp:
                    info["ext"] = "jpg"
                    # Asegurar que description está presente para el formatter
                    if "description" not in info:
                        info["description"] = info.get("title", "") or info.get("fulltitle", "")
                    return fp, info

    except Exception as e:
        logger.warning(f"_download_twitter_image (extract_info): {e}")

    # ── Intento 3: API de syndication via proxy ────────────────────────
    try:
        # El token se calcula con la fórmula de Twitter embed.js:
        # (BigInt(id) / 1e15 * Math.PI).toString(36).replace(/(0+|\.)$/g, "")
        import math as _math
        def _syndication_token(tid: str) -> str:
            val = (int(tid) / 1e15) * _math.pi
            chars = "0123456789abcdefghijklmnopqrstuvwxyz"
            int_p = int(abs(val))
            int_s = ""
            if int_p == 0:
                int_s = "0"
            else:
                tmp = int_p
                while tmp:
                    int_s = chars[tmp % 36] + int_s
                    tmp //= 36
            frac_p = abs(val) - int_p
            frac_s = ""
            for _ in range(8):
                frac_p *= 36
                d = min(int(frac_p), 35)
                frac_s += chars[d]
                frac_p -= d
            result = (int_s + "." + frac_s).rstrip("0").rstrip(".")
            return result

        token = _syndication_token(tweet_id)
        api_url = (
            f"https://cdn.syndication.twimg.com/tweet-result"
            f"?id={tweet_id}&lang=en&token={token}"
        )
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Accept":     "application/json",
            "Referer":    "https://platform.twitter.com/",
            "Origin":     "https://platform.twitter.com",
        }
        r = requests.get(api_url, headers=hdrs, timeout=20,
                         proxies=PROXIES, verify=False)
        if r.status_code == 200:
            data       = r.json()
            media_list = data.get("mediaDetails") or []
            photos     = [md for md in media_list if md.get("type") == "photo"]
            targets    = photos if photos else media_list
            for item in targets:
                base_media_url = item.get("media_url_https", "")
                if base_media_url:
                    img_url = base_media_url + "?name=large"
                    fp = _dl_image(img_url, "jpg")
                    if fp:
                        text = data.get("text", "")
                        return fp, {
                            **base_info,
                            "title":       text[:100] or base_info["title"],
                            "description": text,
                            "ext":         "jpg",
                        }
        else:
            logger.warning(f"_download_twitter_image syndication: {r.status_code}")
    except Exception as e:
        logger.warning(f"_download_twitter_image (syndication): {e}")

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


_reddit_token = {"value": None, "exp": 0.0}


def _reddit_oauth_token() -> str | None:
    """Token app-only de la API oficial de Reddit (si hay credenciales)."""
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return None
    if _reddit_token["value"] and time.time() < _reddit_token["exp"] - 60:
        return _reddit_token["value"]
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": REDDIT_UA}, timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        _reddit_token["value"] = j.get("access_token")
        _reddit_token["exp"]   = time.time() + int(j.get("expires_in", 3600))
        return _reddit_token["value"]
    except Exception as e:
        logger.warning(f"_reddit_oauth_token: {e}")
    return None


def _resolve_reddit_url(url: str) -> str:
    """Resuelve share-links (/r/<sub>/s/<token>) y acortadores redd.it
    al link real del post (son un redirect)."""
    if "/s/" not in url and not re.match(r"https?://(www\.)?redd\.it/", url):
        return url
    try:
        if _HAS_CFFI:
            r = cffi_requests.get(url, impersonate=FB_IMPERSONATE, timeout=20,
                                  allow_redirects=True, verify=False)
        else:
            r = requests.get(url, headers=_HEADERS, proxies=PROXIES,
                             timeout=20, allow_redirects=True, verify=False)
        final = str(r.url)
        if "reddit.com" in final:
            logger.info(f"reddit share resuelto a: {final[:80]}")
            return final
    except Exception as e:
        logger.warning(f"_resolve_reddit_url: {e}")
    return url


def _fetch_post_json(url: str) -> dict | None:
    """Obtiene el dict de datos del primer post de la URL de Reddit."""
    url   = _resolve_reddit_url(url)
    clean = re.split(r"[?#]", url)[0].rstrip("/")

    # 1) API oficial con OAuth: la vía confiable desde IPs de datacenter
    token = _reddit_oauth_token()
    if token:
        m = re.search(r"/comments/([a-z0-9]+)", clean, re.IGNORECASE)
        if m:
            try:
                r = requests.get(
                    f"https://oauth.reddit.com/comments/{m.group(1)}?raw_json=1",
                    headers={"Authorization": f"Bearer {token}",
                             "User-Agent": REDDIT_UA},
                    timeout=20,
                )
                r.raise_for_status()
                data = r.json()
                return data[0]["data"]["children"][0]["data"]
            except Exception as e:
                logger.warning(f"_fetch_post_json oauth: {e}")

    # 2) .json público — Reddit lo bloquea (403) desde muchas IPs de
    #    datacenter; con huella de Chrome real a veces pasa.
    try:
        if _HAS_CFFI:
            resp = cffi_requests.get(clean + ".json", headers=_HEADERS,
                                     impersonate=FB_IMPERSONATE,
                                     timeout=20, verify=False)
        else:
            resp = requests.get(clean + ".json", headers=_HEADERS,
                                proxies=PROXIES, timeout=20, verify=False)
        resp.raise_for_status()
        data = resp.json()
        return data[0]["data"]["children"][0]["data"]
    except Exception as e:
        logger.error(
            f"_fetch_post_json error: {e} — si esto es un 403 en GitHub Actions, "
            "crea una app gratis en reddit.com/prefs/apps y configura los secrets "
            "REDDIT_CLIENT_ID y REDDIT_CLIENT_SECRET"
        )
    return None


def _download_vreddit(video_id: str) -> tuple[str | None, dict | None]:
    """Baja un video v.redd.it CON audio. El fallback_url es SOLO la pista de
    video (Reddit sirve DASH con video y audio separados): hay que darle el
    manifiesto a yt-dlp para que baje ambos y los una con ffmpeg."""
    dash = f"https://v.redd.it/{video_id}/DASHPlaylist.mpd"
    tmp_dir = tempfile.mkdtemp()
    opts = {
        "outtmpl":             os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "quiet":               True,
        "no_warnings":         True,
        "merge_output_format": "mp4",
        "noplaylist":          True,
        "socket_timeout":      30,
        "nocheckcertificate":  True,
        "format":              "bv*+ba/b",
        "http_headers":        {"Referer": "https://www.reddit.com/"},
    }
    if PROXY:
        opts["proxy"] = PROXY
    if _HAS_CFFI:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception:
            pass
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(dash, download=True)
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp) and os.path.getsize(fp) > 10_000:
                    return fp, {
                        "title":         "Reddit video",
                        "webpage_url":   f"https://v.redd.it/{video_id}",
                        "extractor_key": "Reddit",
                        "ext":           "mp4",
                        "type":          "video",
                        "duration":      (info or {}).get("duration"),
                    }
    except Exception as e:
        logger.warning(f"_download_vreddit: {str(e)[:200]}")
    _discard_tmp(tmp_dir)
    return None, None


def _dl_image(img_url: str, ext: str = "jpg") -> str | None:
    """Descarga una imagen/gif a un archivo temporal."""
    tmp_dir = tempfile.mkdtemp()
    try:
        r = requests.get(img_url, headers=_HEADERS, proxies=PROXIES,
                         timeout=30, verify=False)
        r.raise_for_status()
        tmp = os.path.join(tmp_dir, f"reddit.{ext}")
        with open(tmp, "wb") as f:
            f.write(r.content)
        if os.path.getsize(tmp) > 1000:
            return tmp
    except Exception as e:
        logger.warning(f"_dl_image: {e}")
    _discard_tmp(tmp_dir)
    return None


def download_reddit_post(url: str) -> tuple[str | list[str] | None, dict | None]:
    """
    Descarga un post de Reddit. Devuelve:
      - (str, info)       → imagen única / video / gif
      - (list[str], info) → galería de imágenes
      - (None, None)      → error
    """
    # Links directos de media: no son posts, no tienen .json
    m = re.match(r"https?://i\.redd\.it/([^?#]+)", url)
    if m:
        fname = m.group(1)
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "jpg"
        fp = _dl_image(url, ext)
        if fp:
            tipo = "gif" if ext == "gif" else ("video" if ext == "mp4" else "image")
            return fp, {"title": "Reddit", "webpage_url": url,
                        "extractor_key": "Reddit", "ext": ext, "type": tipo}
        return None, None
    m = re.match(r"https?://v\.redd\.it/([a-zA-Z0-9]+)", url)
    if m:
        fp, info = _download_vreddit(m.group(1))
        if fp:
            return fp, info

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
            # URL de máxima resolución. Los items animados de galería no
            # traen "u": traen "gif"/"mp4".
            s = media.get("s", {}) or {}
            img_url = (s.get("u") or s.get("gif") or s.get("mp4") or "").replace("&amp;", "&")
            if not img_url:
                continue
            if not s.get("u"):
                ext = "gif" if s.get("gif") else "mp4"
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
        # El DASH trae video y audio SEPARADOS: bajar solo fallback_url
        # entrega el video mudo. _download_vreddit une las dos pistas.
        m = re.search(r"v\.redd\.it/([a-zA-Z0-9]+)",
                      f"{rv.get('dash_url') or ''} {rv.get('fallback_url') or ''}")
        if m:
            fp, info = _download_vreddit(m.group(1))
            if fp:
                info["title"] = title
                info["webpage_url"] = url
                return fp, info
        # Último recurso: fallback_url directo (puede venir sin audio)
        video_url = (rv.get("fallback_url") or "").replace("?source=fallback", "")
        if video_url:
            fp = _download_direct_url(video_url, "mp4")
            if fp:
                return fp, {**base_info, "ext": "mp4", "type": "video"}

    # ── 5. Intentar con yt-dlp directamente ───────────────────────
    # (el flujo Reddit conserva su propio mensaje de error genérico)
    try:
        fp, info = download_media(url, "reddit")
    except DownloadBlocked:
        fp, info = None, None
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
