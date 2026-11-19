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
# KONFIGURASI BOT & MODEL
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# User ID Telegram yang diizinkan (opsional)
allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = [int(uid.strip()) for uid in allowed_raw.split(",") if uid.strip().isdigit()]

# Model Profiles yang didukung
MODELS_CATALOG = {
    "gemini": {
        "id": "gemini-3.8-flash",
        "name": "Gemini 3.8 Flash ⚡",
        "desc": "Sangat cepat, ringan, optimal untuk scraping metrik, command k3s, dan monitoring.",
        "env_key": "GEMINI_API_KEY",
    },
    "claude": {
        "id": "claude-opus-4-6",
        "name": "Claude Opus 4.6 (Thinking) 🧠",
        "desc": "Deep reasoning, analisis arsitektur, pemecahan masalah rumit, logika self-healing XFSCI.",
        "env_key": "ANTHROPIC_API_KEY",
    },
}

# State Global
current_model_key = "gemini"  # Default awal
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
    """Memeriksa apakah pengirim memiliki otorisasi."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def init_agent(model_key: str):
    """Menginisialisasi atau mengganti sesi Agent dengan model pilihan."""
    global antigravity_agent, agent_context_manager, current_model_key

    if not ANTIGRAVITY_AVAILABLE:
        logger.warning("Antigravity SDK tidak terinstall.")
        return False, "Antigravity SDK tidak terpasang."

    target_config = MODELS_CATALOG.get(model_key, MODELS_CATALOG["gemini"])
    model_id = target_config["id"]
    required_key_name = target_config["env_key"]
    api_key_val = os.getenv(required_key_name, "").strip()

    # Fallback jika model Claude tapi ANTHROPIC_API_KEY belum ada, cek GEMINI_API_KEY
    if not api_key_val:
        if model_key == "claude" and not api_key_val:
            return False, f"Kunci `{required_key_name}` belum diset di `.env` untuk menggunakan Claude."
        elif model_key == "gemini" and not api_key_val:
            return False, f"Kunci `{required_key_name}` belum diset di `.env` untuk menggunakan Gemini."

    async with agent_lock:
        # Tutup sesi agent sebelumnya jika ada
        if agent_context_manager is not None:
            try:
                await agent_context_manager.__aexit__(None, None, None)
            except Exception as e:
                logger.error(f"Error menutup agent sebelumnya: {e}")
            agent_context_manager = None
            antigravity_agent = None

        try:
            logger.info(f"Menginisialisasi model: {target_config['name']} ({model_id})")
            agent_cfg = LocalAgentConfig(
                model=model_id,
                api_key=api_key_val,
                system_instructions=(
                    "Anda adalah asisten AI Antigravity untuk sistem XFSCI "
                    "(Explainable Federated Self-Healing Cloud Infrastructure). "
                    "Anda menguasai Kubernetes k3s, GNN topology, Prometheus, RL-based decision agent, "
                    "dan explainability XAI. Berikan respons yang terstruktur, jelas, dan solutif."
                ),
                capabilities=CapabilitiesConfig(),
            )
            agent_obj = Agent(agent_cfg)
            antigravity_agent = await agent_obj.__aenter__()
            agent_context_manager = agent_obj
            current_model_key = model_key
            logger.info(f"✅ Berhasil mengaktifkan model {target_config['name']}")
            return True, f"Model berhasil diubah ke *{target_config['name']}*"
        except Exception as e:
            logger.error(f"Gagal menginisialisasi agent: {e}")
            return False, str(e)


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

    active_model_info = MODELS_CATALOG.get(current_model_key, {})
    welcome_text = (
        f"👋 Halo *{user.first_name}*!\n\n"
        "🤖 *XFSCI Antigravity Agent Bot*\n"
        f"🧠 Model Aktif: *{active_model_info.get('name', 'None')}*\n"
        f"🆔 ID Telegram: `{user_id}`\n\n"
        "🛠 *Daftar Perintah:*\n"
        "• `/model` : Lihat atau ganti model AI (Gemini 3.8 Flash / Claude Opus 4.6)\n"
        "• `/start` : Info bot dan status\n\n"
        "Kirim pertanyaan atau instruksi langsung di chat ini untuk dijawab oleh AI!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani perintah /model untuk melihat atau mengganti model."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    args = context.args
    if not args:
        # Tampilkan status model saat ini dan cara ganti
        active_model = MODELS_CATALOG.get(current_model_key, {})
        text = (
            "🧠 *Pilihan Model Antigravity:*\n\n"
            f"📌 *Sedang Aktif:* {active_model.get('name')}\n"
            f"_{active_model.get('desc')}_\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Untuk mengganti model, ketik perintah:\n"
            "• `/model gemini`  → **Gemini 3.8 Flash** ⚡\n"
            "• `/model claude`  → **Claude Opus 4.6 (Thinking)** 🧠\n"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    target = args[0].lower().strip()
    if target in ["gemini", "flash", "3.8"]:
        target_key = "gemini"
    elif target in ["claude", "opus", "4.6"]:
        target_key = "claude"
    else:
        await update.message.reply_text(
            "❌ Model tidak dikenali. Pilih antara `/model gemini` atau `/model claude`."
        )
        return

    status_msg = await update.message.reply_text(
        f"⏳ Sedang mengalihkan ke *{MODELS_CATALOG[target_key]['name']}*...",
        parse_mode="Markdown",
    )

    success, msg = await init_agent(target_key)
    if success:
        await status_msg.edit_text(f"✅ {msg}\n_{MODELS_CATALOG[target_key]['desc']}_", parse_mode="Markdown")
    else:
        await status_msg.edit_text(
            f"❌ Gagal mengalihkan model:\n`{msg}`\n\n"
            f"Pastikan token `{MODELS_CATALOG[target_key]['env_key']}` sudah diisi di file `.env`.",
            parse_mode="Markdown",
        )


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

    active_model_name = MODELS_CATALOG.get(current_model_key, {}).get("name", "AI Agent")

    # Kirim status "Typing..."
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    status_msg = await update.message.reply_text(
        f"⏳ *{active_model_name}* sedang memproses...",
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
                full_reply = "(AI selesai memproses tanpa balasan teks)"
        else:
            active_info = MODELS_CATALOG.get(current_model_key, {})
            full_reply = (
                f"⚠️ *{active_info.get('name')} belum aktif!*\n\n"
                f"Kunci `{active_info.get('env_key')}` belum diset di file `.env`.\n"
                "Ketik `/model` untuk memilih model lain atau periksa konfigurasi `.env` Anda."
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
        await status_msg.edit_text(f"❌ Terjadi kesalahan saat memproses:\n`{str(e)}`")


async def run_bot_loop(app):
    """Menjalankan polling bot Telegram secara asynchronous."""
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("🚀 Bot Telegram siap dan mendengarkan chat...")

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
            print("\n[!] Menghentikan bot Telegram...")
            # Tutup agent
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
        print("❌ ERROR: TELEGRAM_BOT_TOKEN belum diatur!")
        print("Silakan buat file .env atau export TELEGRAM_BOT_TOKEN='token_anda'")
        return

    print("=" * 65)
    print("🤖 Memulai XFSCI Dual-Brain Antigravity Telegram Bot")
    print("⚡ Model 1: Gemini 3.8 Flash (Fast & Real-time)")
    print("🧠 Model 2: Claude Opus 4.6 (Deep Reasoning & Thinking)")
    print(f"Bot Token : {TELEGRAM_BOT_TOKEN[:10]}...[TERSEMBUNYI]")
    print("=" * 65)

    # Inisialisasi default model (Gemini 3.8 Flash terlebih dahulu)
    success, msg = await init_agent("gemini")
    if not success and ANTHROPIC_API_KEY:
        # Coba fallback ke Claude jika Gemini gagal tapi Claude ada
        logger.info("Mencoba fallback inisialisasi ke Claude Opus...")
        await init_agent("claude")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await run_bot_loop(app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Program selesai.")
