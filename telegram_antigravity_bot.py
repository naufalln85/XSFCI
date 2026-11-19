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

# API Key opsional — jika kosong, SDK akan pakai sesi `agy` login yang sudah ada
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# User ID Telegram yang diizinkan (opsional)
allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = [int(uid.strip()) for uid in allowed_raw.split(",") if uid.strip().isdigit()]

# Model Profiles
MODELS_CATALOG = {
    "gemini": {
        "id": "gemini-3.8-flash",
        "name": "Gemini 3.8 Flash ⚡",
        "desc": "Sangat cepat, optimal untuk monitoring, query k3s, dan respons real-time.",
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


def _build_agent_config(model_key: str) -> LocalAgentConfig:
    """
    Membuat konfigurasi agent.
    - Jika ada API key di .env, gunakan itu (mode standalone).
    - Jika TIDAK ada API key, SDK otomatis pakai sesi login `agy` (mode Antigravity account).
      Ini memungkinkan akses ke SEMUA model (Claude, Gemini, dll) tanpa API key terpisah.
    """
    target = MODELS_CATALOG.get(model_key, MODELS_CATALOG["gemini"])
    model_id = target["id"]

    # Cek apakah ada API key eksplisit di environment
    api_key = None
    if model_key == "gemini" and GEMINI_API_KEY:
        api_key = GEMINI_API_KEY
    elif model_key == "claude" and ANTHROPIC_API_KEY:
        api_key = ANTHROPIC_API_KEY

    # Buat konfigurasi — tanpa api_key berarti pakai sesi agy login
    config_kwargs = dict(
        model=model_id,
        system_instructions=(
            "Anda adalah asisten AI Antigravity untuk sistem XFSCI "
            "(Explainable Federated Self-Healing Cloud Infrastructure). "
            "Anda menguasai Kubernetes k3s, GNN topology, Prometheus, "
            "AI Agent RL-based, Federated Learning, dan XAI. "
            "Berikan respons yang terstruktur, jelas, dan solutif. "
            "Anda memiliki akses ke terminal server untuk menjalankan perintah."
        ),
        capabilities=CapabilitiesConfig(),
    )
    if api_key:
        config_kwargs["api_key"] = api_key

    return LocalAgentConfig(**config_kwargs)


async def init_agent(model_key: str):
    """Menginisialisasi atau mengganti sesi Agent dengan model pilihan."""
    global antigravity_agent, agent_context_manager, current_model_key

    if not ANTIGRAVITY_AVAILABLE:
        return False, "Antigravity SDK tidak terpasang. Jalankan: pip install google-antigravity"

    target_config = MODELS_CATALOG.get(model_key, MODELS_CATALOG["gemini"])

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
            logger.info(f"Menginisialisasi model: {target_config['name']}")
            agent_cfg = _build_agent_config(model_key)
            agent_obj = Agent(agent_cfg)
            antigravity_agent = await agent_obj.__aenter__()
            agent_context_manager = agent_obj
            current_model_key = model_key
            logger.info(f"✅ Berhasil mengaktifkan {target_config['name']}")
            return True, f"Model berhasil diubah ke *{target_config['name']}*"
        except Exception as e:
            logger.error(f"Gagal menginisialisasi agent: {e}")
            hint = ""
            err_str = str(e).lower()
            if "api key" in err_str or "authentication" in err_str or "credential" in err_str:
                hint = (
                    "\n\n💡 *Solusi:*\n"
                    "1. Jalankan `agy` di terminal server dan login dengan akun Google Anda, ATAU\n"
                    "2. Tambahkan `GEMINI_API_KEY` (gratis) ke file `.env`"
                )
            return False, f"{str(e)}{hint}"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if not is_authorized(user_id):
        await update.message.reply_text(
            f"⛔ Akses ditolak.\nUser ID Anda: `{user_id}`",
            parse_mode="Markdown",
        )
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
        "• `/model` — Lihat/ganti model (Gemini ⚡ / Claude 🧠)\n"
        "• `/start` — Info dan status bot\n\n"
        "Kirim pertanyaan atau instruksi langsung di sini!"
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
        auth_mode = "🔑 API Key" if (GEMINI_API_KEY or ANTHROPIC_API_KEY) else "👤 Sesi Antigravity (agy login)"
        text = (
            "🧠 *Pilihan Model:*\n\n"
            f"📌 *Aktif:* {active_model.get('name')}\n"
            f"_{active_model.get('desc')}_\n"
            f"🔐 Autentikasi: {auth_mode}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Ganti model:\n"
            "• `/model gemini` → Gemini 3.8 Flash ⚡\n"
            "• `/model claude` → Claude Opus 4.6 🧠\n"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    target = args[0].lower().strip()
    if target in ["gemini", "flash", "3.8"]:
        target_key = "gemini"
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
        await status_msg.edit_text(
            f"✅ {msg}\n_{MODELS_CATALOG[target_key]['desc']}_",
            parse_mode="Markdown",
        )
    else:
        await status_msg.edit_text(f"❌ Gagal:\n{msg}", parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    user_text = update.message.text
    if not user_text:
        return

    active_model_name = MODELS_CATALOG.get(current_model_key, {}).get("name", "AI")

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    status_msg = await update.message.reply_text(
        f"⏳ *{active_model_name}* sedang memproses...",
        parse_mode="Markdown",
    )

    try:
        if antigravity_agent is not None:
            response = await antigravity_agent.chat(user_text)
            full_reply = ""
            async for token in response:
                full_reply += token
            if not full_reply.strip():
                full_reply = "(Selesai tanpa balasan teks)"
        else:
            full_reply = (
                "⚠️ *AI Agent belum aktif.*\n\n"
                "Pastikan Anda sudah login `agy` di server,\n"
                "atau tambahkan `GEMINI_API_KEY` ke file `.env`.\n"
                "Ketik `/model` untuk info lebih lanjut."
            )

        if len(full_reply) > 4000:
            for i in range(0, len(full_reply), 4000):
                await update.message.reply_text(full_reply[i : i + 4000])
            await status_msg.delete()
        else:
            await status_msg.edit_text(
                full_reply,
                parse_mode="Markdown" if antigravity_agent is None else None,
            )

    except Exception as e:
        logger.error(f"Error: {e}")
        await status_msg.edit_text(f"❌ Error:\n`{str(e)}`")


async def run_bot_loop(app):
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("🚀 Bot Telegram siap dan mendengarkan chat!")
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
    print("🤖 XFSCI Antigravity Telegram Bot — Dual Brain")
    print("⚡ Gemini 3.8 Flash  |  🧠 Claude Opus 4.6 (Thinking)")
    print(f"🔑 Token: {TELEGRAM_BOT_TOKEN[:10]}...[TERSEMBUNYI]")

    # Info autentikasi
    if GEMINI_API_KEY or ANTHROPIC_API_KEY:
        print("🔐 Mode: API Key dari .env")
    else:
        print("👤 Mode: Sesi Antigravity (agy login)")
        print("   Pastikan sudah menjalankan `agy` dan login terlebih dahulu!")
    print("=" * 65)

    # Coba inisialisasi default model
    success, msg = await init_agent("gemini")
    if not success:
        print(f"⚠️ Gemini: {msg}")
        # Fallback ke Claude
        success2, msg2 = await init_agent("claude")
        if not success2:
            print(f"⚠️ Claude: {msg2}")
            print("Bot berjalan tanpa AI Agent (akan memberi instruksi setup saat di-chat)")

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
