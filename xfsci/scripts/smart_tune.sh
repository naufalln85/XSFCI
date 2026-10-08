#!/bin/bash
# ============================================================
# XFSCI Smart Tune — Wrapper Script
# ============================================================
# Menjalankan Bayesian Optimization (Optuna TPE) untuk
# menemukan hyperparameter optimal secara CERDAS.
#
# Cara pakai:
#   chmod +x scripts/smart_tune.sh
#
#   # Foreground (lihat output real-time):
#   ./scripts/smart_tune.sh
#
#   # Background (recommended untuk optimasi lama):
#   nohup ./scripts/smart_tune.sh > smart_tune.log 2>&1 &
#   tail -f smart_tune.log
#
#   # Lanjutkan optimasi sebelumnya (resume):
#   ./scripts/smart_tune.sh --resume
# ============================================================

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Aktifkan virtual environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Install Optuna jika belum ada
echo "🔍 Checking Optuna installation..."
python3 -c "import optuna" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "📦 Installing Optuna..."
    pip install optuna --quiet
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Memulai Smart Tuner (Bayesian Optimization / Optuna TPE)   ║"
echo "║  Setiap trial BELAJAR dari trial sebelumnya                 ║"
echo "║  → Semakin banyak trial = semakin pintar & akurat           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Default: 30 trials. Pass args through (e.g. --n-trials 50 --resume)
python3 scripts/smart_tune.py "$@"
