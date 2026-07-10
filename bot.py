import logging
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from media_handler import handle_link
from start_handler import start, help_command

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Cualquier excepción no manejada queda loggeada con traceback completo
    (sin esto PTB la imprime a medias y el fallo es invisible en los logs)."""
    logger.error("Excepción no manejada procesando un update", exc_info=context.error)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("❌ TELEGRAM_BOT_TOKEN no está configurado en las variables de entorno.")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_error_handler(on_error)

    logger.info("✅ Bot iniciado y escuchando...")
    # drop_pending_updates: el workflow se relanza cada 5.5 h con un hueco de
    # 1-2 min; los links encolados en ese hueco llegan rancios y una ráfaga
    # de descargas al arrancar retrasa los mensajes frescos. Quien mandó un
    # link durante el hueco simplemente lo reenvía.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
