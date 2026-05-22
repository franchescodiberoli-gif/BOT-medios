import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from url_detector import extract_url, detect_platform
from downloader import download_media, get_clean_url
from formatter import format_message

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_MB = 50


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
    processing_msg = await update.message.reply_text("⏳ Descargando contenido...")

    try:
        file_path, info = download_media(url)

        if not file_path or not info:
            await processing_msg.edit_text(
                "❌ No pude descargar ese contenido.\n"
                "Puede que sea privado o que la red social lo esté bloqueando."
            )
            return

        clean_url = get_clean_url(info)
        caption_text = format_message(platform, info, clean_url)

        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        ext = os.path.splitext(file_path)[1].lower()

        await processing_msg.delete()

        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            with open(file_path, "rb") as f:
                await update.message.reply_photo(photo=f, caption=caption_text, parse_mode="Markdown")
        elif ext in (".gif",):
            with open(file_path, "rb") as f:
                await update.message.reply_animation(animation=f, caption=caption_text, parse_mode="Markdown")
        elif file_size_mb > MAX_FILE_SIZE_MB:
            await update.message.reply_text(
                f"⚠️ El archivo pesa más de {MAX_FILE_SIZE_MB}MB, no puedo enviarlo directamente.\n\n" + caption_text,
                parse_mode="Markdown",
                disable_web_page_preview=False
            )
        else:
            with open(file_path, "rb") as f:
                await update.message.reply_video(video=f, caption=caption_text, parse_mode="Markdown", supports_streaming=True)

        try:
            os.remove(file_path)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error procesando {url}: {e}")
        await processing_msg.edit_text(
            "❌ Ocurrió un error al procesar el link. Intenta de nuevo o verifica que el link sea público."
        )
