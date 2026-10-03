#!/usr/bin/env bash
# ============================================================
# XFSCI End-to-End Live Test
# ============================================================
# Script ini menjalankan test lengkap pipeline XFSCI:
#   1. Verifikasi koneksi Kubernetes
#   2. Verifikasi koneksi Prometheus
#   3. Cek status pod di namespace demo
#   4. (Opsional) Inject chaos
#   5. Jalankan Orchestrator LIVE (tanpa --dry-run)
#   6. Verifikasi recovery
#
# Penggunaan:
#   ./test_live_pipeline.sh [--chaos <tipe>] [--deployment <nama>]
#
# Contoh:
#   ./test_live_pipeline.sh                              # Test tanpa chaos
#   ./test_live_pipeline.sh --chaos cpu_stress           # Test dengan CPU stress
#   ./test_live_pipeline.sh --chaos memory_leak --deployment frontend
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Default values
CHAOS_TYPE=""
DEPLOYMENT="cartservice"
NAMESPACE="demo"
CHAOS_DURATION=60

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --chaos) CHAOS_TYPE="$2"; shift 2 ;;
        --deployment|-d) DEPLOYMENT="$2"; shift 2 ;;
        --namespace|-n) NAMESPACE="$2"; shift 2 ;;
        --duration) CHAOS_DURATION="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║    🧪 XFSCI End-to-End LIVE Pipeline Test              ║${NC}"
echo -e "${CYAN}║    Deployment: ${YELLOW}${DEPLOYMENT}${CYAN} | Namespace: ${YELLOW}${NAMESPACE}${CYAN}         ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ──────────────────────────────────────────────────────
# Step 1: Cek koneksi Kubernetes
# ──────────────────────────────────────────────────────
echo -e "${CYAN}[1/5] 🔗 Verifikasi koneksi Kubernetes...${NC}"
if kubectl cluster-info &>/dev/null; then
    CONTEXT=$(kubectl config current-context 2>/dev/null || echo "unknown")
    echo -e "${GREEN}   ✅ Kubernetes terhubung (context: ${CONTEXT})${NC}"
    echo -e "   Nodes:"
    kubectl get nodes -o wide 2>/dev/null | head -10 | sed 's/^/   /'
else
    echo -e "${RED}   ❌ Kubernetes TIDAK tersambung!${NC}"
    echo -e "${YELLOW}   Cek kubeconfig: export KUBECONFIG=~/.kube/config${NC}"
    exit 1
fi
echo ""

# ──────────────────────────────────────────────────────
# Step 2: Cek koneksi Prometheus
# ──────────────────────────────────────────────────────
echo -e "${CYAN}[2/5] 📡 Verifikasi koneksi Prometheus...${NC}"
PROM_URL=$(python3 -c "
import yaml
with open('${PROJECT_DIR}/configs/config.yaml') as f:
    c = yaml.safe_load(f)
print(c['monitoring']['prometheus']['url'])
" 2>/dev/null || echo "http://172.20.0.104:30090")

if curl -sf "${PROM_URL}/api/v1/status/runtimeinfo" &>/dev/null; then
    echo -e "${GREEN}   ✅ Prometheus OK (${PROM_URL})${NC}"
else
    echo -e "${YELLOW}   ⚠️ Prometheus tidak merespons di ${PROM_URL}${NC}"
    echo -e "${YELLOW}   Pipeline akan tetap berjalan dengan fallback metrics${NC}"
fi
echo ""

# ──────────────────────────────────────────────────────
# Step 3: Cek status pod target
# ──────────────────────────────────────────────────────
echo -e "${CYAN}[3/5] 📦 Status pod di namespace '${NAMESPACE}'...${NC}"
kubectl get pods -n "$NAMESPACE" 2>/dev/null | sed 's/^/   /' || \
    echo -e "${YELLOW}   ⚠️ Namespace '${NAMESPACE}' tidak ditemukan atau kosong${NC}"
echo ""

# ──────────────────────────────────────────────────────
# Step 4: Inject chaos (jika diminta)
# ──────────────────────────────────────────────────────
if [[ -n "$CHAOS_TYPE" ]]; then
    echo -e "${CYAN}[4/5] 🔥 Injecting chaos: ${YELLOW}${CHAOS_TYPE}${CYAN} → ${DEPLOYMENT}...${NC}"
    echo ""
    bash "${SCRIPT_DIR}/inject_chaos.sh" "$CHAOS_TYPE" "$DEPLOYMENT" "$NAMESPACE" "$CHAOS_DURATION"
    echo ""
    echo -e "${CYAN}   ⏳ Menunggu 15 detik agar metrik Prometheus terupdate...${NC}"
    sleep 15
    echo ""
else
    echo -e "${CYAN}[4/5] ⏭️ Chaos injection dilewati (gunakan --chaos <tipe> untuk mengaktifkan)${NC}"
    echo ""
fi

# ──────────────────────────────────────────────────────
# Step 5: Jalankan Orchestrator LIVE
# ──────────────────────────────────────────────────────
echo -e "${CYAN}[5/5] 🚀 Menjalankan XFSCI Orchestrator (LIVE MODE)...${NC}"
echo -e "${RED}   ⚡ LIVE MODE — Aksi akan dieksekusi langsung ke Kubernetes!${NC}"
echo ""

cd "$PROJECT_DIR"
python3 -m agent.orchestrator -d "$DEPLOYMENT" -n "$(kubectl get nodes --no-headers | head -1 | awk '{print $1}')" 2>&1

echo ""
echo -e "${CYAN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ End-to-End test selesai!${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════════${NC}"

# Post-test: Tampilkan status pod setelah test
echo ""
echo -e "${CYAN}📊 Status pod SETELAH test:${NC}"
kubectl get pods -n "$NAMESPACE" 2>/dev/null | sed 's/^/   /'
echo ""
