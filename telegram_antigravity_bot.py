import asyncio
import logging
import os
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
                os.environ.setdefault(k.strip(), v.strip())

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

    welcome_text = (
        f"👋 Halo *{user.first_name}*!\n\n"
        "🤖 *XFSCI Antigravity Agent Bot* siap digunakan.\n"
        f"🆔 ID Telegram Anda: `{user_id}`\n\n"
        "Kirim pertanyaan, perintah coding, atau instruksi troubleshooting cluster Anda di sini."
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
                full_reply = "(Antigravity selesai memproses tanpa pesan teks balasan)"
        else:
            # Fallback jika SDK Antigravity belum terinstall/berjalan
            full_reply = (
                "⚠️ *Antigravity SDK belum aktif di server.*\n"
                f"Pesan Anda diterima: \"{user_text}\"\n\n"
                "Pastikan dependensi `google-antigravity` sudah terpasang."
            )

        # Telegram limit 4096 karakter per pesan
        if len(full_reply) > 4000:
            for i in range(0, len(full_reply), 4000):
                await update.message.reply_text(full_reply[i : i + 4000])
            await status_msg.delete()
        else:
            await status_msg.edit_text(full_reply)

    except Exception as e:
        logger.error(f"Error memproses pesan: {e}")
        await status_msg.edit_text(
            f"❌ Terjadi kesalahan saat memproses:\n`{str(e)}`",
            parse_mode="Markdown",
        )


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

    # Inisialisasi Antigravity jika tersedia
    if ANTIGRAVITY_AVAILABLE:
        try:
            print("Menginisialisasi Google Antigravity Agent...")
            agent_config = LocalAgentConfig(
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

                app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
                app.add_handler(CommandHandler("start", start_command))
                app.add_handler(
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
                )

                print("🚀 Bot Telegram siap dan mendengarkan chat...")
                await app.run_polling()
                return
        except Exception as e:
            print(f"⚠️ Gagal menghubungkan Antigravity Agent: {e}")
            print("Menjalankan bot dalam mode standalone...")

    # Fallback jika Antigravity belum terkonfigurasi
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Bot Telegram berjalan...")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Bot dihentikan oleh pengguna.")
