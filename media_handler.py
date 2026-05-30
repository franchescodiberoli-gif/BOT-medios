import os
import logging
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from telegram.error import BadRequest
from url_detector import extract_url, detect_platform
from downloader import download_media, download_reddit_post, get_clean_url
from formatter import format_message

logger = logging.getLogger(__name__)
MAX_FILE_SIZE_MB = 50

# Límites de Telegram
CAPTION_LIMIT = 1024   # caption de foto/video/animación
TEXT_LIMIT    = 4096   # mensaje de texto normal


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
            "📸 Instagram · 🎵 TikTok · 📘 Facebook · ▶️ YouTube · 👽 Reddit · 🐦 Twitter · 🧵 Threads"
        )
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    if platform in ("youtube_short", "youtube_long"):
        processing_msg = await update.message.reply_text(
            "⏳ Descargando video de YouTube...\n"
            "_(Puede tardar unos segundos, probando múltiples métodos)_",
            parse_mode="Markdown",
        )
    else:
        processing_msg = await update.message.reply_text("⏳ Descargando contenido...")

    try:
        # ── Reddit: flujo especial ─────────────────────────────────
        if platform == "reddit":
            await _handle_reddit(update, processing_msg, url)
            return

        # ── Redgifs: flujo especial ────────────────────────────────
        if platform == "redgifs":
            await _handle_redgifs(update, processing_msg, url)
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
        caption_text = format_message(platform, info, clean_url)

        # Twitter imagen: file_path puede ser "URL:https://..." para envío directo
        if isinstance(file_path, str) and file_path.startswith("URL:"):
            img_url = file_path[4:]
            await processing_msg.delete()
            media_caption, overflow = _split_caption(caption_text)
            await _safe_reply(update.message.reply_photo, photo=img_url, caption=media_caption)
            if overflow:
                await _send_long_text(update, overflow)
            return

        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        ext = os.path.splitext(file_path)[1].lower()

        await processing_msg.delete()
        await _send_single_file(update, file_path, ext, file_size_mb, caption_text)

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
        _cleanup(files)
        return

    # ── Galería ────────────────────────────────────────────────────
    if isinstance(files, list) and len(files) > 1:
        count   = len(files)
        caption = (
            f"👽 *Reddit* · 🖼️ Galería ({count} fotos)\n\n"
            f"📌 *Título:* {title}\n\n"
            f"🔗 [Ver post]({post_url})"
        )
        # Telegram acepta hasta 10 en un media group
        media_group = []
        for i, fp in enumerate(files[:10]):
            ext = os.path.splitext(fp)[1].lower()
            cap = caption if i == 0 else None
            with open(fp, "rb") as f:
                data = f.read()
            if ext in (".gif",):
                media_group.append(InputMediaAnimation(media=data, caption=cap, parse_mode="Markdown"))
            else:
                media_group.append(InputMediaPhoto(media=data, caption=cap, parse_mode="Markdown"))

        await update.message.reply_media_group(media=media_group)
        _cleanup(files)
        return

    # ── Archivo único ─────────────────────────────────────────────
    fp = files[0] if isinstance(files, list) else files
    ext = os.path.splitext(fp)[1].lower()
    file_size_mb = os.path.getsize(fp) / (1024 * 1024)
    caption = format_message("reddit", info, post_url)
    await _send_single_file(update, fp, ext, file_size_mb, caption)
    _cleanup(fp)


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

    await processing_msg.delete()

    clean_url    = get_clean_url(info)
    caption_text = format_message("redgifs", info, clean_url or url)
    ext          = os.path.splitext(file_path)[1].lower()
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    await _send_single_file(update, file_path, ext, file_size_mb, caption_text)
    _cleanup(file_path)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

async def _send_single_file(update, file_path: str, ext: str, size_mb: float, caption: str):
    # Si el caption excede el límite de Telegram para media (1024), lo enviamos
    # como mensaje de texto aparte (que admite hasta 4096) y dejamos la media sin caption.
    media_caption, overflow = _split_caption(caption)

    if ext in (".jpg", ".jpeg", ".png", ".webp"):
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
        with open(file_path, "rb") as f:
            await _safe_reply(
                update.message.reply_video,
                video=f, caption=media_caption, supports_streaming=True,
            )

    if overflow:
        await _send_long_text(update, overflow)


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
