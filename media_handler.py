import os
import json
import logging
import subprocess
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from telegram.error import BadRequest
from url_detector import extract_url, detect_platform
from downloader import (download_media, download_reddit_post, download_facebook_ads,
                        get_clean_url, DownloadBlocked)
from formatter import format_message

logger = logging.getLogger(__name__)
MAX_FILE_SIZE_MB = 50

# Límites de Telegram
CAPTION_LIMIT = 1024   # caption de foto/video/animación
TEXT_LIMIT    = 4096   # mensaje de texto normal

# YouTube: ≤ 60s = Short, > 60s = formato largo (por duración real del video)
SHORT_MAX_SECONDS = 60

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# YouTube y TikTok: además del video, extraer y enviar también el audio.
AUDIO_PLATFORMS = ("youtube_short", "youtube_long", "tiktok")

# Mensaje por clase de bloqueo (DownloadBlocked.reason). Redacción neutral
# de plataforma: la misma clase puede venir de YouTube, Instagram, etc.
_BLOCKED_MSGS = {
    "bot_check":       ("❌ La plataforma está bloqueando al servidor con su verificación "
                        "anti-bot.\nNo es culpa del video: suele ser temporal. Reintenta en "
                        "un rato. Si pasa seguido, la solución de fondo es un proxy "
                        "residencial (secret PROXY_URL)."),
    "cookies_invalid": ("❌ Las cookies configuradas expiraron. El bot sigue funcionando en "
                        "modo anónimo; para renovar el acceso, re-exporta las cookies y "
                        "actualiza el secret."),
    "auth_wall":       ("❌ Esa red social exige iniciar sesión para ver ese contenido y no "
                        "hay sesión válida (cookies ausentes o vencidas)."),
    "private":         "❌ Ese contenido es privado o de cuenta protegida.",
    "members":         "❌ Ese video es solo para miembros del canal.",
    "age_gate":        "❌ Ese video tiene restricción de edad y no hay sesión que la pase.",
    "drm":             "❌ Ese contenido tiene DRM: no se puede descargar.",
    "geo":             "❌ Ese contenido no está disponible en el país del servidor.",
    "unavailable":     "❌ Ese contenido no existe o fue eliminado.",
    "rate_limit":      ("❌ La red social limitó las peticiones del servidor (rate limit). "
                        "Espera unos minutos y reintenta."),
    "auth":            ("❌ La red social rechazó la petición (401/403): veto de IP o sesión "
                        "vencida."),
    "no_format":       "❌ No se ofreció ningún formato descargable (posible DRM o bloqueo).",
    "net":             "❌ Falla de red durante la descarga. Intenta de nuevo.",
    "blocked":         "❌ La red social está bloqueando la descarga desde el servidor.",
}
_BLOCKED_DEFAULT = ("❌ No pude descargar ese contenido.\n"
                    "Puede que sea privado, requiera login, o la red social lo esté "
                    "bloqueando.")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text(
            "🔗 Mándame un link válido de Instagram, TikTok, YouTube, Reddit, Twitter, Facebook o Threads."
        )
        return

    platform = detect_platform(url)

    if platform == "unknown":
        await update.message.reply_text(
            "❓ No reconozco esa red social. Las que soporto son:\n"
            "📸 Instagram · 🎵 TikTok · 📘 Facebook · 📢 Facebook Ads · ▶️ YouTube · 👽 Reddit · 🐦 Twitter"
        )
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    if platform in ("youtube_short", "youtube_long"):
        processing_msg = await update.message.reply_text(
            "⏳ Descargando video de YouTube...\n"
            "_(Puede tardar unos segundos, probando múltiples métodos)_",
            parse_mode="Markdown",
        )
    elif platform == "facebook_ads":
        processing_msg = await update.message.reply_text(
            "⏳ Descargando video del anuncio de Facebook Ads Library...\n"
            "_(Probando múltiples métodos)_",
            parse_mode="Markdown",
        )
    else:
        processing_msg = await update.message.reply_text("⏳ Descargando contenido...")

    file_path = None
    try:
        # ── Reddit: flujo especial ─────────────────────────────────
        if platform == "reddit":
            await _handle_reddit(update, processing_msg, url)
            return

        # ── Redgifs: flujo especial ────────────────────────────────
        if platform == "redgifs":
            await _handle_redgifs(update, processing_msg, url)
            return

        # ── Facebook Ads Library: flujo especial ──────────────────
        if platform == "facebook_ads":
            await _handle_facebook_ads(update, processing_msg, url)
            return

        # ── Resto de plataformas ───────────────────────────────────
        file_path, info = download_media(url, platform)

        if not file_path or not info:
            await processing_msg.edit_text(
                "❌ No pude descargar ese contenido.\n"
                "Puede que sea privado, requiera login, o la red social lo esté bloqueando."
            )
            return

        clean_url    = get_clean_url(info)

        # Twitter con varias fotos: mandar el álbum completo, no solo la 1ª
        all_images = (info.get("all_images") or []) if isinstance(info, dict) else []
        if platform == "twitter" and len(all_images) > 1:
            caption_text = format_message(platform, info, clean_url)
            await processing_msg.delete()
            media_caption, overflow = _split_caption(caption_text)
            group = [
                InputMediaPhoto(media=u,
                                caption=(media_caption if i == 0 else None),
                                parse_mode="Markdown")
                for i, u in enumerate(all_images[:10])
            ]
            try:
                await update.message.reply_media_group(media=group)
            except BadRequest:
                # Markdown inválido en el caption: reintento en texto plano
                plain = [InputMediaPhoto(media=u,
                                         caption=(caption_text[:CAPTION_LIMIT] if i == 0 else None))
                         for i, u in enumerate(all_images[:10])]
                await update.message.reply_media_group(media=plain)
            if overflow:
                await _send_long_text(update, overflow)
            return

        # Twitter imagen: file_path puede ser "URL:https://..." para envío directo
        if isinstance(file_path, str) and file_path.startswith("URL:"):
            img_url = file_path[4:]
            caption_text = format_message(platform, info, clean_url)
            await processing_msg.delete()
            media_caption, overflow = _split_caption(caption_text)
            await _safe_reply(update.message.reply_photo, photo=img_url, caption=media_caption)
            if overflow:
                await _send_long_text(update, overflow)
            return

        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        ext = os.path.splitext(file_path)[1].lower()

        # Metadatos reales del video (ancho/alto/duración) para que Telegram
        # respete el aspect ratio y no lo muestre cuadrado o estirado.
        width = height = duration = 0
        if ext not in IMAGE_EXTS and ext != ".gif":
            width, height, duration = _video_metadata(file_path, info)

        # Reclasificar YouTube por DURACIÓN real: ≤60s = Short, >60s = largo.
        # (youtu.be y watch?v= pueden ser cualquiera de los dos.)
        if platform in ("youtube_short", "youtube_long") and duration:
            platform = "youtube_short" if duration <= SHORT_MAX_SECONDS else "youtube_long"

        caption_text = format_message(platform, info, clean_url)

        await processing_msg.delete()
        await _send_single_file(
            update, file_path, ext, file_size_mb, caption_text,
            width=width, height=height, duration=duration,
        )

        # YouTube y TikTok: además del video, enviar también el audio extraído.
        if platform in AUDIO_PLATFORMS and ext not in IMAGE_EXTS and ext != ".gif":
            await _send_audio(update, file_path, info)

    except DownloadBlocked as e:
        # Bloqueo clasificado: responder el motivo específico, no el genérico.
        logger.warning(f"[{e.platform}] bloqueado: clase={e.reason}")
        msg = _BLOCKED_MSGS.get(e.reason, _BLOCKED_DEFAULT)
        try:
            await processing_msg.edit_text(msg)
        except Exception:
            try:
                await update.message.reply_text(msg)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error procesando {url}: {e}")
        # processing_msg pudo haber sido borrado ya (p.ej. tras delete()),
        # así que intentamos editar y, si falla, mandamos un mensaje nuevo.
        err_text = (
            "❌ Ocurrió un error al procesar el link. "
            "Intenta de nuevo o verifica que el link sea público."
        )
        try:
            await processing_msg.edit_text(err_text)
        except Exception:
            try:
                await update.message.reply_text(err_text)
            except Exception:
                pass
    finally:
        # Borrar el temporal pase lo que pase (también si el ENVÍO falló):
        # el proceso corre horas y los archivos huérfanos llenan el disco.
        if file_path and not (isinstance(file_path, str) and file_path.startswith("URL:")):
            _cleanup(file_path)


# ═══════════════════════════════════════════════════════════════════
# Reddit handler
# ═══════════════════════════════════════════════════════════════════

async def _handle_reddit(update, processing_msg, url: str):
    files, info = download_reddit_post(url)

    if not files or not info:
        await processing_msg.edit_text(
            "❌ No pude descargar ese contenido de Reddit.\n"
            "Puede que sea privado, eliminado o no compatible."
        )
        return

    # La limpieza va en finally: si el envío falla, el archivo huérfano
    # se acumula en el disco del runner durante toda la corrida.
    try:
        await processing_msg.delete()

        post_type = info.get("type", "")
        title     = info.get("title", "Reddit post")
        post_url  = info.get("webpage_url", url)

        # ── Redgif embebido en post de Reddit ─────────────────────────
        if post_type == "redgif_in_reddit":
            redgif_url = info.get("redgif_url", post_url)
            caption    = format_message("redgif_in_reddit", info, redgif_url)
            ext = os.path.splitext(files)[1].lower() if isinstance(files, str) else ".mp4"
            file_size_mb = os.path.getsize(files) / (1024 * 1024) if isinstance(files, str) else 0
            await _send_single_file(update, files, ext, file_size_mb, caption)
            return

        # ── Galería ────────────────────────────────────────────────────
        if isinstance(files, list) and len(files) > 1:
            count   = len(files)
            # Caption SIN Markdown: los títulos de Reddit traen *, _ y [ que
            # rompen el parseo de Telegram y tumbaban el álbum completo.
            caption = (
                f"👽 Reddit · 🖼️ Galería ({count} fotos)\n\n"
                f"📌 {title}\n\n"
                f"🔗 {post_url}"
            )[:CAPTION_LIMIT]
            # sendMediaGroup no acepta animaciones: los .gif/.mp4 van aparte.
            # Telegram acepta hasta 10 items por media group.
            media_group, animated = [], []
            for fp in files[:10]:
                ext = os.path.splitext(fp)[1].lower()
                if ext in (".gif", ".mp4"):
                    animated.append(fp)
                    continue
                with open(fp, "rb") as f:
                    data = f.read()
                media_group.append(
                    InputMediaPhoto(media=data, caption=(caption if not media_group else None))
                )
            if media_group:
                await update.message.reply_media_group(media=media_group)
            for j, fp in enumerate(animated):
                cap = caption if (not media_group and j == 0) else None
                with open(fp, "rb") as f:
                    await update.message.reply_animation(animation=f, caption=cap)
            return

        # ── Archivo único ─────────────────────────────────────────────
        fp = files[0] if isinstance(files, list) else files
        ext = os.path.splitext(fp)[1].lower()
        file_size_mb = os.path.getsize(fp) / (1024 * 1024)
        caption = format_message("reddit", info, post_url)
        await _send_single_file(update, fp, ext, file_size_mb, caption)
    finally:
        _cleanup(files)


# ═══════════════════════════════════════════════════════════════════
# Redgifs directo (URL redgifs.com/watch/...)
# ═══════════════════════════════════════════════════════════════════

async def _handle_redgifs(update, processing_msg, url: str):
    file_path, info = download_media(url, "redgifs")

    if not file_path or not info:
        await processing_msg.edit_text(
            "❌ No pude descargar ese GIF de Redgifs.\n"
            "Puede ser privado o estar bloqueando la descarga."
        )
        return

    try:
        await processing_msg.delete()

        clean_url    = get_clean_url(info)
        caption_text = format_message("redgifs", info, clean_url or url)
        ext          = os.path.splitext(file_path)[1].lower()
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        await _send_single_file(update, file_path, ext, file_size_mb, caption_text)
    finally:
        _cleanup(file_path)


# ═══════════════════════════════════════════════════════════════════
# Facebook Ads Library handler
# ═══════════════════════════════════════════════════════════════════

async def _handle_facebook_ads(update, processing_msg, url: str):
    files, info = download_facebook_ads(url)

    if not files or not info:
        await processing_msg.edit_text(
            "❌ No pude descargar ese anuncio de Facebook Ads Library.\n\n"
            "Posibles causas:\n"
            "• Facebook está bloqueando la descarga (error 403)\n"
            "• El ID del anuncio no existe o fue eliminado\n"
            "• El anuncio no tiene media descargable\n\n"
            "💡 Si ves error 403 en los logs, configura `FACEBOOK_COOKIES` "
            "con las cookies de una sesión iniciada de Facebook.\n"
            "Asegúrate de enviar el link completo con el `?id=XXXXXXXXXX`"
        )
        return

    try:
        await processing_msg.delete()

        ad_id     = info.get("id", "")
        clean_url = f"https://www.facebook.com/ads/library/?id={ad_id}"
        caption_text = format_message("facebook_ads", info, clean_url)

        # ── Carrusel de imágenes (varias fotos) ───────────────────────────
        if isinstance(files, list) and len(files) > 1:
            media_caption, overflow = _split_caption(caption_text)
            media_group = []
            for i, fp in enumerate(files[:10]):
                cap = media_caption if i == 0 else None
                with open(fp, "rb") as f:
                    data = f.read()
                media_group.append(
                    InputMediaPhoto(media=data, caption=cap, parse_mode="Markdown")
                )
            try:
                await update.message.reply_media_group(media=media_group)
            except BadRequest:
                # Reintento sin Markdown si el caption tiene entidades inválidas
                plain = [InputMediaPhoto(media=open(fp, "rb").read(),
                                         caption=(caption_text if i == 0 else None))
                         for i, fp in enumerate(files[:10])]
                await update.message.reply_media_group(media=plain)
            if overflow:
                await _send_long_text(update, overflow)
            return

        # ── Archivo único (1 video o 1 imagen) ────────────────────────────
        fp = files[0] if isinstance(files, list) else files
        ext          = os.path.splitext(fp)[1].lower()
        file_size_mb = os.path.getsize(fp) / (1024 * 1024)

        width = height = duration = 0
        if ext not in IMAGE_EXTS and ext != ".gif":
            width, height, duration = _video_metadata(fp, info)

        await _send_single_file(
            update, fp, ext, file_size_mb, caption_text,
            width=width, height=height, duration=duration,
        )
    finally:
        _cleanup(files)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

async def _send_single_file(update, file_path: str, ext: str, size_mb: float, caption: str,
                            width: int = 0, height: int = 0, duration: int = 0):
    # Si el caption excede el límite de Telegram para media (1024), lo enviamos
    # como mensaje de texto aparte (que admite hasta 4096) y dejamos la media sin caption.
    media_caption, overflow = _split_caption(caption)

    if ext in IMAGE_EXTS:
        with open(file_path, "rb") as f:
            await _safe_reply(update.message.reply_photo, photo=f, caption=media_caption)
    elif ext in (".gif",):
        with open(file_path, "rb") as f:
            await _safe_reply(update.message.reply_animation, animation=f, caption=media_caption)
    elif size_mb > MAX_FILE_SIZE_MB:
        # No se puede enviar el archivo: avisamos y mandamos el caption como texto.
        warn = (
            f"⚠️ El archivo pesa {size_mb:.1f}MB (máx {MAX_FILE_SIZE_MB}MB), "
            f"no puedo enviarlo directamente.\n\n{caption}"
        )
        await _send_long_text(update, warn)
        return
    else:
        # Asegurar dimensiones para que Telegram respete el aspect ratio.
        if not (width and height):
            w, h, d = _ffprobe(file_path)
            width    = width or w
            height   = height or h
            duration = duration or d
        vkwargs = {"caption": media_caption, "supports_streaming": True}
        if width:    vkwargs["width"]    = width
        if height:   vkwargs["height"]   = height
        if duration: vkwargs["duration"] = duration
        with open(file_path, "rb") as f:
            await _safe_reply(update.message.reply_video, video=f, **vkwargs)

    if overflow:
        await _send_long_text(update, overflow)


def _extract_audio(video_path: str) -> str | None:
    """Extrae la pista de audio de un video a un .mp3 con ffmpeg.
    Devuelve la ruta del .mp3, o None si el video no tiene audio o ffmpeg falla."""
    audio_path = os.path.splitext(video_path)[0] + ".mp3"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2",
             audio_path],
            capture_output=True, text=True, timeout=120,
        )
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            return audio_path
    except Exception as e:
        logger.warning(f"_extract_audio: {e}")
    return None


async def _send_audio(update, video_path: str, info: dict):
    """Extrae el audio del video ya descargado y lo envía como archivo de audio.
    Silencioso ante fallos: el video ya se envió, el audio es solo un extra."""
    audio_path = _extract_audio(video_path)
    if not audio_path:
        return
    try:
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            logger.info(f"_send_audio: audio {size_mb:.1f}MB > {MAX_FILE_SIZE_MB}MB, omitido")
            return
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        kwargs = {"title": (info.get("title") or "audio").strip()[:64]}
        performer = (info.get("uploader") or info.get("channel") or "").strip()[:64]
        if performer:
            kwargs["performer"] = performer
        duration = int(info.get("duration") or 0)
        if duration:
            kwargs["duration"] = duration
        with open(audio_path, "rb") as f:
            await update.message.reply_audio(audio=f, **kwargs)
    except Exception as e:
        logger.warning(f"_send_audio: {e}")
    finally:
        _cleanup(audio_path)


def _ffprobe(file_path: str):
    """Devuelve (width, height, duration_segundos) leyendo el archivo con ffprobe.
    Si falla, devuelve (0, 0, 0)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", file_path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        duration = int(float(data.get("format", {}).get("duration", 0) or 0))
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return (
                    int(s.get("width", 0) or 0),
                    int(s.get("height", 0) or 0),
                    duration,
                )
        return 0, 0, duration
    except Exception as e:
        logger.warning(f"_ffprobe: {e}")
        return 0, 0, 0


def _video_metadata(file_path: str, info: dict):
    """Combina los metadatos de yt-dlp (info) con ffprobe como respaldo.
    Devuelve (width, height, duration)."""
    width    = int(info.get("width") or 0)
    height   = int(info.get("height") or 0)
    duration = int(info.get("duration") or 0)
    if not (width and height and duration):
        w, h, d = _ffprobe(file_path)
        width    = width or w
        height   = height or h
        duration = duration or d
    return width, height, duration


def _split_caption(caption: str):
    """Devuelve (caption_para_media, texto_sobrante).

    Si el caption cabe en el límite de media, va completo y no hay sobrante.
    Si no, la media se envía sin caption y todo el texto se manda aparte.
    """
    if not caption:
        return None, None
    if len(caption) <= CAPTION_LIMIT:
        return caption, None
    return None, caption


async def _safe_reply(reply_func, **kwargs):
    """Envía con parse_mode Markdown; si Telegram rechaza el formato
    (entidades mal formadas), reintenta en texto plano."""
    try:
        await reply_func(parse_mode="Markdown", **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "parse" in msg or "entit" in msg or "markdown" in msg:
            logger.warning(f"Markdown inválido, reintentando sin formato: {e}")
            await reply_func(**kwargs)
        else:
            raise


async def _send_long_text(update, text: str):
    """Envía texto respetando el límite de 4096, partiéndolo si hace falta."""
    for i in range(0, len(text), TEXT_LIMIT):
        chunk = text[i:i + TEXT_LIMIT]
        try:
            await update.message.reply_text(
                chunk, parse_mode="Markdown", disable_web_page_preview=False
            )
        except BadRequest as e:
            logger.warning(f"Markdown inválido en texto, enviando plano: {e}")
            await update.message.reply_text(chunk, disable_web_page_preview=False)


def _cleanup(files):
    if isinstance(files, list):
        for fp in files:
            try:
                os.remove(fp)
            except Exception:
                pass
    elif files:
        try:
            os.remove(files)
        except Exception:
            pass
