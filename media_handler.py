import os
import logging
from telegram import Update, InputMediaPhoto
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from url_detector import extract_url, detect_platform
from downloader import download_media, download_reddit_image, get_clean_url
from formatter import format_message

logger = logging.getLogger(__name__)
MAX_FILE_SIZE_MB = 50

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text("🔗 Mándame un link válido de Instagram, TikTok, YouTube, Reddit, Twitter, Facebook o Threads.")
        return

    platform = detect_platform(url)

    if platform == "unknown":
        await update.message.reply_text("❓ No reconozco esa red social. Las que soporto son:\n📸 Instagram · 🎵 TikTok · 📘 Facebook · ▶️ YouTube · 👽 Reddit · 🐦 Twitter · 🧵 Threads")
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    if platform in ("youtube_short", "youtube_long"):
        processing_msg = await update.message.reply_text(
            "⏳ Descargando video de YouTube...\n_(Puede tardar unos segundos, probando múltiples métodos)_",
            parse_mode="Markdown",
        )
    else:
        processing_msg = await update.message.reply_text("⏳ Descargando contenido...")

    try:
        file_path, info = download_media(url, platform)

        if (not file_path) and platform in ("reddit",):
            file_path, info = download_reddit_image(url)

        if not file_path or not info:
            await processing_msg.edit_text("❌ No pude descargar ese contenido.\nPuede que sea privado, requiera login, o la red social lo esté bloqueando.")
            return

        clean_url = get_clean_url(info)
        formatter_key = "reddit" if platform == "redgifs" else platform
        caption_text = format_message(formatter_key, info, clean_url)

        # LÓGICA DE GALERÍA (MÚLTIPLES ARCHIVOS)
        if isinstance(file_path, list):
            media_group = []
            open_files = []
            
            for i, fp in enumerate(file_path):
                f = open(fp, "rb")
                open_files.append(f)
                # Solo ponemos el caption en la primera foto
                if i == 0:
                    media_group.append(InputMediaPhoto(media=f, caption=caption_text, parse_mode="Markdown"))
                else:
                    media_group.append(InputMediaPhoto(media=f))
                    
            await update.message.reply_media_group(media=media_group)
            
            # Limpiar memoria y borrar archivos temporales de la galería
            for f in open_files:
                f.close()
            for fp in file_path:
                try:
                    os.remove(fp)
                except Exception:
                    pass

        # LÓGICA NORMAL (UN SOLO ARCHIVO: VIDEO, FOTO, GIF)
        else:
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            ext = os.path.splitext(file_path)[1].lower()

            if ext in (".jpg", ".jpeg", ".png", ".webp"):
                with open(file_path, "rb") as f:
                    await update.message.reply_photo(photo=f, caption=caption_text, parse_mode="Markdown")
            elif ext in (".gif",):
                with open(file_path, "rb") as f:
                    await update.message.reply_animation(animation=f, caption=caption_text, parse_mode="Markdown")
            elif file_size_mb > MAX_FILE_SIZE_MB:
                await update.message.reply_text(
                    f"⚠️ El archivo pesa {file_size_mb:.1f}MB (máx {MAX_FILE_SIZE_MB}MB), no puedo enviarlo directamente.\n\n" + caption_text,
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                )
            else:
                with open(file_path, "rb") as f:
                    await update.message.reply_video(video=f, caption=caption_text, parse_mode="Markdown", supports_streaming=True)

            try:
                os.remove(file_path)
            except Exception:
                pass

        # Borramos el mensaje de procesamiento SOLAMENTE si todo salió bien
        await processing_msg.delete()

    except Exception as e:
        logger.error(f"Error procesando {url}: {e}")
        try:
            await processing_msg.edit_text("❌ Ocurrió un error al procesar el link o el formato del archivo.")
        except Exception as edit_error:
            logger.error(f"No se pudo editar el estado: {edit_error}")
