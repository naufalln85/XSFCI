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

# Folder untuk menyimpan file upload dari user
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Model Profiles dengan Fallback Stabil
MODELS_CATALOG = {
    "gemini": {
        "id": "gemini-2.5-flash",
        "name": "Gemini 2.5 Flash ⚡",
        "desc": "Sangat cepat, kapasitas tinggi & stabil, optimal untuk monitoring & analisis.",
    },
    "gemini-preview": {
        "id": "gemini-3.8-flash",
        "name": "Gemini 3.8 Flash (Preview) 🚀",
        "desc": "Versi eksperimental terbaru, performa tinggi.",
    },
    "claude": {
        "id": "claude-opus-4-6",
        "name": "Claude Opus 4.6 (Thinking) 🧠",
        "desc": "Deep reasoning, analisis arsitektur, logika self-healing, dan evaluasi riset.",
    },
}

# State Global
current_model_key = "gemini"
antigravity_agent = None
agent_context_manager = None
agent_lock = asyncio.Lock()

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


def _build_agent_config(model_key: str, override_model_id: str = None) -> LocalAgentConfig:
    target = MODELS_CATALOG.get(model_key, MODELS_CATALOG["gemini"])
    model_id = override_model_id or target["id"]

    api_key = None
    if "gemini" in model_key and GEMINI_API_KEY:
        api_key = GEMINI_API_KEY
    elif model_key == "claude" and ANTHROPIC_API_KEY:
        api_key = ANTHROPIC_API_KEY

    config_kwargs = dict(
        model=model_id,
        system_instructions=(
            "Anda adalah asisten AI Antigravity untuk sistem XFSCI "
            "(Explainable Federated Self-Healing Cloud Infrastructure) dan persiapan Lomba Huawei ICT. "
            "Anda menguasai Kubernetes k3s, GNN topology, Prometheus, "
            "AI Agent RL-based, Federated Learning, dan XAI. "
            "Berikan respons yang terstruktur, jelas, to-the-point, dan solutif. "
            "Jika pengguna mengunggah dokumen lomba atau file arsitektur, pelajari dan buatkan ringkasan serta strategi terbaik."
        ),
        capabilities=CapabilitiesConfig(),
    )
    if api_key:
        config_kwargs["api_key"] = api_key

    return LocalAgentConfig(**config_kwargs)


async def init_agent(model_key: str, override_model_id: str = None):
    """Menginisialisasi atau mengganti sesi Agent dengan model pilihan."""
    global antigravity_agent, agent_context_manager, current_model_key

    if not ANTIGRAVITY_AVAILABLE:
        return False, "Antigravity SDK tidak terpasang."

    target_config = MODELS_CATALOG.get(model_key, MODELS_CATALOG["gemini"])

    async with agent_lock:
        if agent_context_manager is not None:
            try:
                await agent_context_manager.__aexit__(None, None, None)
            except Exception as e:
                logger.error(f"Error menutup agent sebelumnya: {e}")
            agent_context_manager = None
            antigravity_agent = None

        try:
            active_id = override_model_id or target_config['id']
            logger.info(f"Menginisialisasi model: {target_config['name']} ({active_id})")
            agent_cfg = _build_agent_config(model_key, active_id)
            agent_obj = Agent(agent_cfg)
            antigravity_agent = await agent_obj.__aenter__()
            agent_context_manager = agent_obj
            current_model_key = model_key
            logger.info(f"✅ Berhasil mengaktifkan {target_config['name']}")
            return True, f"Model berhasil diubah ke *{target_config['name']}*"
        except Exception as e:
            logger.error(f"Gagal menginisialisasi agent: {e}")
            return False, str(e)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if not is_authorized(user_id):
        await update.message.reply_text(f"⛔ Akses ditolak. ID: `{user_id}`", parse_mode="Markdown")
        return

    active_model_info = MODELS_CATALOG.get(current_model_key, {})
    agent_status = "✅ Aktif" if antigravity_agent else "⏸️ Belum Aktif"

    welcome_text = (
        f"👋 Halo *{user.first_name}*!\n\n"
        "🤖 *XFSCI Antigravity Telegram Agent*\n"
        f"🧠 Model: *{active_model_info.get('name', 'N/A')}*\n"
        f"📡 Status: *{agent_status}*\n"
        f"🆔 ID: `{user_id}`\n\n"
        "🛠 *Perintah:*\n"
        "• `/model` — Ganti model (Gemini ⚡ / Claude 🧠)\n"
        "• `/start` — Info status bot\n\n"
        "💡 *Tips:* Anda bisa langsung *mengirimkan file dokumen lomba Huawei ICT* (PDF, Word, TXT) ke bot ini!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    args = context.args
    if not args:
        active_model = MODELS_CATALOG.get(current_model_key, {})
        text = (
            "🧠 *Pilihan Model:*\n\n"
            f"📌 *Aktif:* {active_model.get('name')}\n"
            f"_{active_model.get('desc')}_\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Ganti model:\n"
            "• `/model gemini` → Gemini 2.5 Flash ⚡ (Paling Stabil & Cepat)\n"
            "• `/model claude` → Claude Opus 4.6 🧠 (Thinking)\n"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    target = args[0].lower().strip()
    if target in ["gemini", "flash", "stable"]:
        target_key = "gemini"
    elif target in ["preview", "3.8"]:
        target_key = "gemini-preview"
    elif target in ["claude", "opus", "4.6", "thinking"]:
        target_key = "claude"
    else:
        await update.message.reply_text("❌ Pilih `/model gemini` atau `/model claude`.")
        return

    status_msg = await update.message.reply_text(
        f"⏳ Mengalihkan ke *{MODELS_CATALOG[target_key]['name']}*...",
        parse_mode="Markdown",
    )

    success, msg = await init_agent(target_key)
    if success:
        await status_msg.edit_text(f"✅ {msg}\n_{MODELS_CATALOG[target_key]['desc']}_", parse_mode="Markdown")
    else:
        await status_msg.edit_text(f"❌ Gagal:\n`{msg}`", parse_mode="Markdown")


async def stream_chat_to_telegram(prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengirim prompt ke Agent dengan auto-retry saat 503 high demand."""
    active_model_name = MODELS_CATALOG.get(current_model_key, {}).get("name", "AI")

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    status_msg = await update.message.reply_text(
        f"⏳ *{active_model_name}* sedang menganalisis...",
        parse_mode="Markdown",
    )

    if antigravity_agent is None:
        await status_msg.edit_text("⚠️ AI Agent belum siap. Coba ganti model dengan `/model gemini`.")
        return

    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(f"[Attempt {attempt+1}] Mengirim prompt ke {active_model_name}...")
            response = await antigravity_agent.chat(prompt)

            full_reply = ""
            last_edit_time = time.time()

            async for token in response:
                full_reply += token

                now = time.time()
                if now - last_edit_time > 2.0 and len(full_reply.strip()) > 10:
                    last_edit_time = now
                    preview_text = full_reply + " ▌"
                    if len(preview_text) <= 4000:
                        try:
                            await status_msg.edit_text(preview_text)
                        except Exception:
                            pass

            logger.info(f"Selesai streaming. Total karakter: {len(full_reply)}")

            if not full_reply.strip():
                full_reply = "✅ (Instruksi selesai diproses oleh AI Agent)"

            if len(full_reply) > 4000:
                for i in range(0, len(full_reply), 4000):
                    await update.message.reply_text(full_reply[i : i + 4000])
                await status_msg.delete()
            else:
                await status_msg.edit_text(full_reply)

            return  # Berhasil, keluar dari fungsi!

        except Exception as e:
            err_str = str(e)
            logger.warning(f"Error chat pada percobaan {attempt+1}: {err_str}")

            # Jika terkena 503 (Server High Demand), lakukan auto-retry dengan jeda
            if "503" in err_str or "high demand" in err_str.lower() or "unavailable" in err_str.lower():
                if attempt < max_retries - 1:
                    wait_sec = (attempt + 1) * 3
                    await status_msg.edit_text(
                        f"⏳ Server Google sedang padat traffic (503). Mencoba kembali otomatis dalam {wait_sec} detik... ({attempt+1}/{max_retries})"
                    )
                    await asyncio.sleep(wait_sec)
                    continue
                else:
                    # Percobaan terakhir gagal, coba fallback otomatis ke model stabil
                    await status_msg.edit_text(
                        "⚠️ Server model ini sedang penuh antrian. Sedang mengalihkan ke model cadangan stabil..."
                    )
                    await init_agent("gemini", override_model_id="gemini-2.5-flash")
                    try:
                        resp_fallback = await antigravity_agent.chat(prompt)
                        reply_fb = ""
                        async for t in resp_fallback:
                            reply_fb += t
                        await status_msg.edit_text(reply_fb if reply_fb.strip() else "✅ Selesai.")
                        return
                    except Exception as fb_err:
                        await status_msg.edit_text(f"❌ Server Google saat ini sedang sangat sibuk:\n`{str(fb_err)}`\nSilakan coba lagi dalam beberapa saat.")
                        return
            else:
                # Error lain yang bukan 503
                await status_msg.edit_text(f"❌ Terjadi kesalahan:\n`{err_str}`")
                return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    user_text = update.message.text
    if not user_text:
        return

    await stream_chat_to_telegram(user_text, update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    doc = update.message.document
    if not doc:
        return

    file_name = doc.file_name or "uploaded_file"
    file_path = UPLOAD_DIR / file_name

    download_msg = await update.message.reply_text(f"📥 Mengunduh file `{file_name}`...", parse_mode="Markdown")

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(file_path))
        await download_msg.edit_text(f"📄 File `{file_name}` berhasil diterima!\nMeminta Antigravity mempelajari isi file...", parse_mode="Markdown")

        prompt = (
            f"Pengguna baru saja mengunggah file bernama '{file_name}' yang disimpan di path '{file_path}'. "
            f"Buka dan pelajari isi file tersebut secara mendalam. "
            f"Jika file berkaitan dengan Lomba Huawei ICT atau arsitektur sistem, berikan analisis komprehensif, "
            f"ringkasan poin penting, serta rekomendasi langkah strategis selanjutnya."
        )

        await stream_chat_to_telegram(prompt, update, context)

    except Exception as e:
        logger.error(f"Gagal memproses dokumen: {e}")
        await download_msg.edit_text(f"❌ Gagal memproses file:\n`{str(e)}`")


async def run_bot_loop(app):
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("🚀 Bot Telegram siap dan mendengarkan chat & file!")
        print("   Buka: https://t.me/xsfci_antigravity_bot")

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
            print("\n[!] Menghentikan bot...")
            global agent_context_manager, antigravity_agent
            if agent_context_manager is not None:
                try:
                    await agent_context_manager.__aexit__(None, None, None)
                except Exception:
                    pass
            await app.updater.stop()
            await app.stop()


async def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN belum diatur di .env!")
        return

    print("=" * 65)
    print("🤖 XFSCI Antigravity Telegram Bot — Dual Brain (Auto-Retry & Fallback)")
    print("⚡ Gemini Flash (Stabil & Cepat) | 🧠 Claude Opus 4.6 (Thinking)")
    print(f"📁 Folder Upload : {UPLOAD_DIR}")
    print("=" * 65)

    success, msg = await init_agent("gemini")
    if not success:
        print(f"⚠️ Inisialisasi awal: {msg}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    await run_bot_loop(app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Program selesai.")
