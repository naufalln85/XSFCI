import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load file .env jika ada
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Coba import Google Antigravity SDK
try:
    from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig
    ANTIGRAVITY_AVAILABLE = True
except ImportError:
    ANTIGRAVITY_AVAILABLE = False

# ==============================================================================
# KONFIGURASI BOT
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# User ID Telegram yang diizinkan (ambil dari env ALLOWED_USER_IDS atau list kosong)
allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = [int(uid.strip()) for uid in allowed_raw.split(",") if uid.strip().isdigit()]

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# State Antigravity Agent
antigravity_agent = None


def is_authorized(user_id: int) -> bool:
    """Memeriksa apakah pengirim memiliki otorisasi."""
    if not ALLOWED_USER_IDS:
        return True  # Terbuka jika belum diset
    return user_id in ALLOWED_USER_IDS


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani perintah /start."""
    user = update.effective_user
    user_id = user.id

    if not is_authorized(user_id):
        await update.message.reply_text(
            f"⛔ Akses ditolak.\nUser ID Anda: `{user_id}` belum terdaftar di whitelist bot.",
            parse_mode="Markdown",
        )
        return

    status_engine = "✅ Antigravity Pro Aktif" if antigravity_agent else "⚠️ Standalone (Belum Ada Gemini API Key)"
    welcome_text = (
        f"👋 Halo *{user.first_name}*!\n\n"
        "🤖 *XFSCI Antigravity Agent Bot* siap digunakan.\n"
        f"⚡ Status AI: *{status_engine}*\n"
        f"🆔 ID Telegram Anda: `{user_id}`\n\n"
        "Kirim pertanyaan atau instruksi troubleshooting cluster Anda di sini."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani pesan teks yang dikirimkan user."""
    user_id = update.effective_user.id

    if not is_authorized(user_id):
        await update.message.reply_text(
            f"⛔ Akses ditolak. User ID `{user_id}` tidak diizinkan.",
            parse_mode="Markdown",
        )
        return

    user_text = update.message.text
    if not user_text:
        return

    # Kirim status "Typing..."
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    status_msg = await update.message.reply_text(
        "⏳ *Antigravity sedang memproses instruksi Anda...*",
        parse_mode="Markdown",
    )

    try:
        if antigravity_agent is not None:
            # Panggil Antigravity Agent
            response = await antigravity_agent.chat(user_text)

            full_reply = ""
            async for token in response:
                full_reply += token

            if not full_reply.strip():
                full_reply = "(Antigravity selesai memproses tanpa pesan balasan teks)"
        else:
            # Fallback jika Antigravity belum terhubung karena belum ada API Key
            full_reply = (
                "⚠️ *Antigravity AI Agent belum aktif!*\n\n"
                f"Pesan Anda diterima: \"_{user_text}_\"\n\n"
                "📌 *Penyebab:* GEMINI_API_KEY belum diset di file `.env`.\n"
                "👉 Dapatkan kunci gratis di: [Google AI Studio](https://aistudio.google.com/app/apikey)\n"
                "Lalu tambahkan ke `.env`:\n`GEMINI_API_KEY=AIzaSy...`"
            )

        # Telegram limit 4096 karakter per pesan
        if len(full_reply) > 4000:
            for i in range(0, len(full_reply), 4000):
                await update.message.reply_text(full_reply[i : i + 4000])
            await status_msg.delete()
        else:
            await status_msg.edit_text(full_reply, parse_mode="Markdown" if antigravity_agent is None else None)

    except Exception as e:
        logger.error(f"Error memproses pesan: {e}")
        await status_msg.edit_text(
            f"❌ Terjadi kesalahan saat memproses:\n`{str(e)}`",
        )


async def run_bot_loop(app):
    """Menjalankan polling bot Telegram secara asynchronous."""
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("🚀 Bot Telegram siap dan mendengarkan chat...")

        stop_event = asyncio.Event()

        # Handle SIGINT / SIGTERM gracefully jika didukung
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError):
            pass

        try:
            await stop_event.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            print("\n[!] Menghentikan bot Telegram...")
            await app.updater.stop()
            await app.stop()


async def main():
    global antigravity_agent

    if not TELEGRAM_BOT_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN belum diatur!")
        print("Silakan buat file .env atau export TELEGRAM_BOT_TOKEN='token_anda'")
        return

    print("=" * 60)
    print("🤖 Memulai XFSCI Antigravity Telegram Bot...")
    print(f"Bot Token: {TELEGRAM_BOT_TOKEN[:10]}...[TERSEMBUNYI]")
    print("=" * 60)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Cek apakah GEMINI_API_KEY tersedia untuk mengaktifkan Agent
    if ANTIGRAVITY_AVAILABLE and GEMINI_API_KEY:
        try:
            print("Menginisialisasi Google Antigravity Agent dengan Gemini API Key...")
            agent_config = LocalAgentConfig(
                api_key=GEMINI_API_KEY,
                system_instructions=(
                    "Anda adalah asisten AI Antigravity untuk sistem XFSCI "
                    "(Explainable Federated Self-Healing Cloud Infrastructure). "
                    "Bantu pengguna mengelola, mendiagnosis, dan memonitor cluster."
                ),
                capabilities=CapabilitiesConfig(),
            )
            async with Agent(agent_config) as agent:
                antigravity_agent = agent
                print("✅ Antigravity Agent berhasil terhubung!")
                await run_bot_loop(app)
                return
        except Exception as e:
            print(f"⚠️ Gagal menghubungkan Antigravity Agent: {e}")
            print("Menjalankan bot dalam mode standalone...")
    elif not GEMINI_API_KEY:
        print("⚠️ Catatan: GEMINI_API_KEY belum diset di .env")
        print("👉 Bot tetap berjalan untuk menerima perintah.")
        print("👉 Tambahkan GEMINI_API_KEY ke .env untuk mengaktifkan kecerdasan Antigravity.")

    await run_bot_loop(app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Program selesai.")
