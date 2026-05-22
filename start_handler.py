from telegram import Update
from telegram.ext import ContextTypes

WELCOME_MSG = """
👋 ¡Hola! Soy *MediaBot*.

Mándame un link y te descargo el contenido al instante.

📱 Redes que soporto:
• 📸 Instagram (video, foto, reels)
• 🎵 TikTok
• 📘 Facebook
• ▶️ YouTube (Shorts y videos largos)
• 👽 Reddit (video, gif, foto, redgif)
• 🐦 Twitter / X
• 🧵 Threads

Solo pega el link y listo 🚀
"""

HELP_MSG = """
ℹ️ *¿Cómo usar MediaBot?*

1. Copia el link de cualquier post/video
2. Pégalo aquí directamente
3. Espera unos segundos ⏳
4. Recibirás el contenido + la info del post
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_MSG, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MSG, parse_mode="Markdown")
