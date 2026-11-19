import asyncio
import logging
import os
import signal
import sys
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
# KONFIGURASI BOT & MODEL
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# User ID Telegram yang diizinkan (opsional)
allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = [int(uid.strip()) for uid in allowed_raw.split(",") if uid.strip().isdigit()]

# Folder Workspace Server
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Folder Sesi Antigravity untuk Memory & Konteks Persisten
SESSION_DIR = BASE_DIR / ".antigravity_sessions"
SESSION_DIR.mkdir(exist_ok=True)

# Katalog Model
MODELS_CATALOG = {
    "claude": {
        "id": "claude-3-5-sonnet",
        "name": "Claude 3.5 / Opus (Antigravity Brain) 🧠",
        "desc": "Penalaran tingkat tinggi, analisis dokumen mendalam, riset lomba Huawei ICT.",
    },
    "gemini": {
        "id": "gemini-2.5-flash",
        "name": "Gemini 2.5 Flash ⚡",
        "desc": "Respons instan, efisien, optimal untuk query harian dan monitoring cluster.",
    },
}

# State Global Model yang sedang dipilih (Default Claude sesuai kebutuhan user!)
current_model_key = "claude"

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def get_agent_config(model_key: str) -> LocalAgentConfig:
    """
    Membuat konfigurasi Agent Antigravity yang selalu segar dan kuat.
    Menggunakan akun Antigravity server Anda + akses penuh ke file server.
    """
    target = MODELS_CATALOG.get(model_key, MODELS_CATALOG["claude"])
    model_id = target["id"]

    # Gunakan API key jika ada di .env (Gemini atau Claude)
    api_key = None
    if "gemini" in model_key and GEMINI_API_KEY:
        api_key = GEMINI_API_KEY
    elif "claude" in model_key:
        api_key = ANTHROPIC_API_KEY if ANTHROPIC_API_KEY else GEMINI_API_KEY

    config_kwargs = dict(
        model=model_id,
        system_instructions=(
            f"Anda adalah Antigravity AI Agent yang berjalan langsung di server {BASE_DIR}. "
            "Anda memiliki akses ke seluruh file project, data cluster k3s, script python, "
            "dan berkas lomba Huawei ICT di server ini. "
            "Jawablah pertanyaan dengan cerdas, mendalam, berbasis konteks file yang ada, "
            "terstruktur, dan gunakan bahasa Indonesia yang profesional serta solutif."
        ),
        capabilities=CapabilitiesConfig(),
    )
    if api_key:
        config_kwargs["api_key"] = api_key

    return LocalAgentConfig(**config_kwargs)


async def ask_antigravity(prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Eksekusi chat ke Antigravity dengan mekanisme Fresh Session per-request:
    - Tidak akan error 'received 1000 OK' karena koneksi selalu segar.
    - Auto-retry jika ada 503 high demand.
    - Live streaming text ke chat Telegram.
    """
    active_info = MODELS_CATALOG.get(current_model_key, MODELS_CATALOG["claude"])
    active_name = active_info["name"]

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    status_msg = await update.message.reply_text(
        f"⏳ *{active_name}* sedang membaca dan menganalisis...",
        parse_mode="Markdown",
    )

    if not ANTIGRAVITY_AVAILABLE:
        await status_msg.edit_text("❌ Antigravity SDK belum terpasang di Python server.")
        return

    # Daftar model fallback jika model pertama sibuk
    models_to_try = [current_model_key]
    if current_model_key != "gemini":
        models_to_try.append("gemini")

    last_error = None
    for model_try in models_to_try:
        try:
            logger.info(f"Menjalankan Antigravity Agent dengan model: {model_try}...")
            agent_cfg = get_agent_config(model_try)

            # Pola Resmi Antigravity SDK: async with Agent(...) as agent
            # Selalu membuat sesi yang bersih dan tahan banting
            async with Agent(agent_cfg) as agent:
                response = await agent.chat(prompt)

                full_reply = ""
                last_edit_time = time.time()

                async for token in response:
                    full_reply += token

                    # Update tampilan Telegram berkala agar terlihat mengetik live
                    now = time.time()
                    if now - last_edit_time > 2.0 and len(full_reply.strip()) > 15:
                        last_edit_time = now
                        preview = full_reply + " ▌"
                        if len(preview) <= 4000:
                            try:
                                await status_msg.edit_text(preview)
                            except Exception:
                                pass

                if not full_reply.strip():
                    full_reply = "✅ Antigravity selesai memproses instruksi Anda di server."

                # Kirim hasil final
                if len(full_reply) > 4000:
                    for i in range(0, len(full_reply), 4000):
                        await update.message.reply_text(full_reply[i : i + 4000])
                    await status_msg.delete()
                else:
                    await status_msg.edit_text(full_reply)

                logger.info(f"Sukses dijawab oleh {model_try} ({len(full_reply)} karakter)")
                return  # Berhasil 100%!

        except Exception as e:
            err_str = str(e)
            last_error = err_str
            logger.warning(f"Percobaan dengan {model_try} gagal: {err_str}")
            if "503" in err_str or "high demand" in err_str.lower() or "1000" in err_str:
                # Coba model berikutnya
                continue
            else:
                break

    # Jika semua model gagal
    await status_msg.edit_text(
        f"❌ Terjadi kendala saat memproses:\n`{last_error}`\n\n"
        "💡 *Tips:* Ketik `/model gemini` atau coba ajukan kembali pertanyaan Anda."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    active_info = MODELS_CATALOG.get(current_model_key, {})
    welcome_text = (
        f"👋 Halo *{user.first_name}*!\n\n"
        "🤖 *Antigravity Server Bridge aktif di Telegram*\n"
        f"🧠 Otak Aktif: *{active_info.get('name')}*\n"
        f"📂 Path Server: `{BASE_DIR}`\n\n"
        "🎯 *Kemampuan:*\n"
        "1. **Tanya Jawab & Deep Search**: Tanyakan apa saja tentang project XFSCI, k3s, GNN, atau lomba Huawei ICT.\n"
        "2. **Analisis Berkas / File**: Kirim file dokumen (PDF, TXT, DOCX, ZIP) langsung ke chat ini!\n"
        "3. **Kontrol Model**: Ketik `/model` untuk beralih antara Claude dan Gemini.\n\n"
        "Silakan ketik pertanyaan atau kirimkan file Anda sekarang!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_model_key
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    args = context.args
    if not args:
        active_info = MODELS_CATALOG.get(current_model_key, {})
        text = (
            "🧠 *Pilihan Model Antigravity:*\n\n"
            f"📌 *Sedang Aktif:* {active_info.get('name')}\n"
            f"_{active_info.get('desc')}_\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Pilih model:\n"
            "• `/model claude` → **Claude (Antigravity Thinking)** 🧠 (Direkomendasikan untuk analisis)\n"
            "• `/model gemini` → **Gemini 2.5 Flash** ⚡ (Super cepat)\n"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    target = args[0].lower().strip()
    if target in ["claude", "opus", "sonnet", "thinking"]:
        current_model_key = "claude"
        await update.message.reply_text(
            f"✅ Beralih ke *{MODELS_CATALOG['claude']['name']}*!\n"
            "Siap untuk penalaran tingkat tinggi dan analisis berkas lomba.",
            parse_mode="Markdown",
        )
    elif target in ["gemini", "flash"]:
        current_model_key = "gemini"
        await update.message.reply_text(
            f"✅ Beralih ke *{MODELS_CATALOG['gemini']['name']}*!\n"
            "Siap untuk respons super cepat dan monitoring.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("❌ Pilihan tidak dikenal. Ketik `/model claude` atau `/model gemini`.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima pertanyaan teks biasa dan menjalankannya di Antigravity server."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    text = update.message.text
    if not text:
        return

    await ask_antigravity(text, update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima file (PDF, TXT, DOCX, dll) dari Telegram dan menganalisisnya di server."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    doc = update.message.document
    if not doc:
        return

    file_name = doc.file_name or "dokumen"
    save_path = UPLOAD_DIR / file_name

    download_msg = await update.message.reply_text(f"📥 Mengunduh `{file_name}` ke server...", parse_mode="Markdown")

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(save_path))
        await download_msg.edit_text(
            f"✅ File `{file_name}` berhasil disimpan di server!\n"
            f"🔍 *Antigravity sedang membaca dan mempelajari isinya...*",
            parse_mode="Markdown",
        )

        # Instruksi ke Antigravity untuk menganalisis file tersebut
        prompt = (
            f"Pengguna telah mengunggah file bernama '{file_name}' ke server pada direktori '{save_path}'. "
            f"Buka dan baca seluruh isi dokumen tersebut. "
            f"Berikan analisis lengkap, rangkuman poin-poin krusialnya, "
            f"dan kaitkan dengan persiapan lomba Huawei ICT serta arsitektur cloud XFSCI."
        )

        await ask_antigravity(prompt, update, context)

    except Exception as e:
        logger.error(f"Gagal download file: {e}")
        await download_msg.edit_text(f"❌ Gagal menerima file: `{str(e)}`")


async def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN belum diset di .env")
        return

    print("=" * 65)
    print("🤖 Antigravity Server Bridge for Telegram")
    print(f"📁 Workspace Server : {BASE_DIR}")
    print(f"🧠 Default Model    : {MODELS_CATALOG[current_model_key]['name']}")
    print("=" * 65)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("🚀 Bot Telegram siap! Buka: https://t.me/xsfci_antigravity_bot")

        stop_event = asyncio.Event()
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
            print("\n[!] Bot dihentikan.")
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
