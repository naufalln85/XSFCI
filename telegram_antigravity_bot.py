import asyncio
import logging
import os
import signal
import time
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

# Load file .env
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

try:
    from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig
    ANTIGRAVITY_AVAILABLE = True
except ImportError:
    ANTIGRAVITY_AVAILABLE = False

# ==============================================================================
# KONFIGURASI
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = [int(uid.strip()) for uid in allowed_raw.split(",") if uid.strip().isdigit()]

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Model ID yang BENAR dan TERSEDIA via Gemini API
MODEL_ID = "gemini-3.8-flash"
MODEL_NAME = "Gemini 3.8 Flash ⚡"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def ask_antigravity(prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim pertanyaan ke Antigravity Agent dan streaming jawaban ke Telegram."""

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    status_msg = await update.message.reply_text(
        f"⏳ *{MODEL_NAME}* sedang menganalisis...", parse_mode="Markdown"
    )

    if not ANTIGRAVITY_AVAILABLE:
        await status_msg.edit_text("❌ Antigravity SDK belum terpasang.")
        return

    if not GEMINI_API_KEY:
        await status_msg.edit_text("❌ GEMINI_API_KEY belum diatur di .env")
        return

    max_retries = 2
    for attempt in range(max_retries):
        try:
            logger.info(f"[Percobaan {attempt+1}] Mengirim ke {MODEL_NAME}...")

            config = LocalAgentConfig(
                model=MODEL_ID,
                api_key=GEMINI_API_KEY,
                system_instructions=(
                    f"Anda adalah Antigravity AI Agent yang berjalan di server '{BASE_DIR}'. "
                    "Anda memiliki akses penuh ke file di server ini termasuk project XFSCI, "
                    "data cluster k3s, script Python, dan berkas lomba Huawei ICT. "
                    "Jawab dengan cerdas, mendalam, terstruktur, dan berbahasa Indonesia."
                ),
                capabilities=CapabilitiesConfig(),
            )

            async with Agent(config) as agent:
                response = await agent.chat(prompt)

                full_reply = ""
                last_edit = time.time()

                async for token in response:
                    full_reply += token
                    now = time.time()
                    if now - last_edit > 2.5 and len(full_reply.strip()) > 20:
                        last_edit = now
                        preview = full_reply[:3900] + " ▌"
                        try:
                            await status_msg.edit_text(preview)
                        except Exception:
                            pass

                if not full_reply.strip():
                    full_reply = "✅ Selesai diproses."

                if len(full_reply) > 4000:
                    await status_msg.delete()
                    for i in range(0, len(full_reply), 4000):
                        await update.message.reply_text(full_reply[i : i + 4000])
                else:
                    await status_msg.edit_text(full_reply)

                logger.info(f"Berhasil! {len(full_reply)} karakter.")
                return

        except Exception as e:
            err = str(e)
            logger.warning(f"Percobaan {attempt+1} gagal: {err}")
            if ("503" in err or "high demand" in err.lower()) and attempt < max_retries - 1:
                await status_msg.edit_text(f"⏳ Server sedang sibuk, mencoba lagi... ({attempt+2}/{max_retries})")
                await asyncio.sleep(3)
                continue
            else:
                await status_msg.edit_text(f"❌ Error:\n`{err[:500]}`")
                return


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text(f"⛔ Akses ditolak. ID: `{user.id}`", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"👋 Halo *{user.first_name}*!\n\n"
        f"🤖 *Antigravity Server Bot*\n"
        f"🧠 Model: *{MODEL_NAME}*\n"
        f"📂 Server: `{BASE_DIR}`\n\n"
        "✨ *Yang bisa Anda lakukan:*\n"
        "• Tanyakan apa saja (deep search, analisis, kode)\n"
        "• Kirimkan file (PDF, TXT, DOCX) untuk dianalisis\n"
        "• Minta bantuan persiapan lomba Huawei ICT\n\n"
        "Langsung ketik pertanyaan atau kirim file!",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return
    text = update.message.text
    if text:
        await ask_antigravity(text, update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    doc = update.message.document
    if not doc:
        return

    file_name = doc.file_name or "dokumen"
    save_path = UPLOAD_DIR / file_name

    msg = await update.message.reply_text(f"📥 Mengunduh `{file_name}`...", parse_mode="Markdown")

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(save_path))
        await msg.edit_text(f"✅ `{file_name}` tersimpan! Sedang dianalisis...", parse_mode="Markdown")

        caption = update.message.caption or ""
        prompt = (
            f"File '{file_name}' telah diupload ke '{save_path}'. "
            f"Buka dan baca seluruh isinya. "
            f"{'Instruksi tambahan: ' + caption + '. ' if caption else ''}"
            f"Berikan analisis lengkap, ringkasan poin penting, "
            f"dan kaitkan dengan project XFSCI atau lomba Huawei ICT jika relevan."
        )
        await ask_antigravity(prompt, update, context)
    except Exception as e:
        await msg.edit_text(f"❌ Gagal: `{str(e)}`")


async def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN belum diset!")
        return

    print("=" * 55)
    print(f"🤖 Antigravity Telegram Bot")
    print(f"🧠 Model : {MODEL_NAME} ({MODEL_ID})")
    print(f"📂 Server: {BASE_DIR}")
    print("=" * 55)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("🚀 Bot siap! https://t.me/xsfci_antigravity_bot")

        stop = asyncio.Event()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):
            pass

        try:
            await stop.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
