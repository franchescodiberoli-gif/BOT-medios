import streamlit as st
import threading
import os
import sys

st.set_page_config(page_title="MediaBot", page_icon="🤖", layout="centered")

st.title("🤖 MediaBot - Telegram")
st.markdown("""
Este es el panel de control del **MediaBot**.  
El bot está corriendo en segundo plano y escuchando mensajes en Telegram.

---

### ¿Cómo usarlo?
1. Abre Telegram y busca tu bot
2. Envía cualquier link de:
   - 📸 Instagram
   - 🎵 TikTok
   - 📘 Facebook
   - ▶️ YouTube (corto o largo)
   - 👽 Reddit (video, gif, foto, redgif)
   - 🐦 Twitter / X
   - 🧵 Threads

3. El bot te responde con el contenido descargado + info

---
""")

token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if token:
    st.success("✅ Bot token detectado — el bot está activo.")
else:
    st.error("❌ TELEGRAM_BOT_TOKEN no configurado. Agrégalo en Secrets de Streamlit Cloud.")

st.markdown("---")
st.caption("Powered by yt-dlp · python-telegram-bot · Streamlit")


def run_bot():
    try:
        import subprocess
        subprocess.Popen([sys.executable, "bot.py"])
    except Exception as e:
        st.error(f"Error al iniciar el bot: {e}")


if "bot_started" not in st.session_state:
    st.session_state["bot_started"] = True
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
