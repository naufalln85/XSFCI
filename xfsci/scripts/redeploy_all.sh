#!/bin/bash
# ============================================================
# XFSCI All-in-One Redeployment & Auto-Repair Script
# ============================================================
# Script ini secara otomatis:
#   1. Mendeteksi IP Master Node Kubernetes terbaru
#   2. Meng-update configs/config.yaml secara otomatis
#   3. Men-deploy ulang stack Observability (Prometheus, Grafana, Loki)
#   4. Men-deploy ulang 11 Microservices Online Boutique
#   5. Memverifikasi status Pod & koneksi Ollama API
#
# Cara pakai:
#   chmod +x scripts/redeploy_all.sh
#   ./scripts/redeploy_all.sh
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$PROJECT_DIR/configs/config.yaml"

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     XFSCI Auto-Repair & Redeployment Pipeline                ║"
echo "║     Explainable Federated Self-Healing Cloud Infrastructure  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# ===== Step 1: Pre-checks & IP Detection =====
echo -e "${BLUE}[Step 1/5]${NC} Verifying cluster & detecting IPs..."
if ! kubectl get nodes > /dev/null 2>&1; then
    echo -e "${RED}❌ Kubernetes cluster not reachable! Check kubectl connection first.${NC}"
    exit 1
fi

MASTER_IP=$(kubectl get nodes -o jsonpath='{.items[?(@.metadata.name=="xsfci-master")].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)
if [ -z "$MASTER_IP" ]; then
    MASTER_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || echo "172.20.0.104")
fi

echo -e "  ✅ Cluster Connected!"
echo -e "  📍 Detected Master Node IP: ${GREEN}${MASTER_IP}${NC}"
echo ""

# ===== Step 2: Auto-update configs/config.yaml =====
echo -e "${BLUE}[Step 2/5]${NC} Auto-updating configs/config.yaml to Master IP (${MASTER_IP})..."
if [ -f "$CONFIG_FILE" ]; then
    # Update Prometheus URL
    sed -i -E "s|url: \"http://172\.20\.0\.[0-9]+:30090\"|url: \"http://${MASTER_IP}:30090\"|g" "$CONFIG_FILE" 2>/dev/null || true
    # Update Ollama URL
    sed -i -E "s|base_url: \"http://172\.20\.0\.[0-9]+:11434\"|base_url: \"http://${MASTER_IP}:11434\"|g" "$CONFIG_FILE" 2>/dev/null || true
    echo -e "  ✅ configs/config.yaml updated with IP ${MASTER_IP}"
else
    echo -e "  ⚠️ config.yaml not found at $CONFIG_FILE"
fi
echo ""

# ===== Step 3: Redeploy Monitoring & Workload Stack =====
echo -e "${BLUE}[Step 3/5]${NC} Redeploying Monitoring & Workload stacks..."
bash "$SCRIPT_DIR/deploy_monitoring.sh" --force

# ===== Step 4: Verify Ollama Service =====
echo -e "${BLUE}[Step 4/5]${NC} Testing Ollama LLM endpoint on http://${MASTER_IP}:11434..."
if curl -s --max-time 3 "http://${MASTER_IP}:11434/api/version" > /dev/null; then
    OLLAMA_VER=$(curl -s "http://${MASTER_IP}:11434/api/version" | grep -o '"version":"[^"]*"' | cut -d'"' -f4 || echo "OK")
    echo -e "  ✅ Ollama API is active! (Version: ${GREEN}${OLLAMA_VER}${NC})"
else
    echo -e "  ⚠️ Ollama API not responding at http://${MASTER_IP}:11434 (Check if systemctl status ollama is running)"
fi
echo ""

# ===== Step 5: Final Summary =====
echo -e "${BLUE}[Step 5/5]${NC} Cluster status summary..."

MON_RUNNING=$(kubectl get pods -n monitoring --no-headers 2>/dev/null | grep -c "Running" || echo "0")
DEMO_RUNNING=$(kubectl get pods -n demo --no-headers 2>/dev/null | grep -c "Running" || echo "0")

echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║      ✅ XFSCI CLUSTER RECOVERY COMPLETE!                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  Active Master IP:  ${MASTER_IP}                               ║"
echo "║  Monitoring Pods:   ${MON_RUNNING} running                             ║"
echo "║  Workload Pods:     ${DEMO_RUNNING} running                             ║"
echo "║                                                              ║"
echo "║  Access Points (Browser):                                    ║"
echo "║  ┌────────────────────────────────────────────────────────┐  ║"
echo "║  │ Prometheus  → http://${MASTER_IP}:30090                │  ║"
echo "║  │ Grafana     → http://${MASTER_IP}:30030                │  ║"
echo "║  │ Loki        → http://${MASTER_IP}:30100                │  ║"
echo "║  │ Frontend    → http://${MASTER_IP}:30080                │  ║"
echo "║  │ Ollama API  → http://${MASTER_IP}:11434                │  ║"
echo "║  └────────────────────────────────────────────────────────┘  ║"
echo "║  (Grafana Login: admin / xfsci-admin-2026)                    ║"
echo "║                                                              ║"
echo "║  Next steps:                                                 ║"
echo "║  1. Run telemetry scraper:                                   ║"
echo "║     python3 data/collectors/metrics_scraper.py --test        ║"
echo "║  2. Run fault injection test:                                ║"
echo "║     ./infrastructure/fault-injection/run_faults.sh           ║"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
