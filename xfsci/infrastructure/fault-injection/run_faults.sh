#!/bin/bash
# ============================================================
# XFSCI Fault Injection Orchestrator
# ============================================================
# Script ini menjalankan skenario fault injection secara
# berurutan dengan jeda di antaranya agar data collection
# mendapat campuran data NORMAL dan ANOMALI yang baik.
#
# Cara pakai:
#   chmod +x run_faults.sh
#   ./run_faults.sh
#
# Urutan eksekusi:
#   1. Collect data NORMAL selama 10 menit
#   2. Jalankan CPU stress (2 menit)
#   3. Collect data NORMAL selama 5 menit (recovery)
#   4. Jalankan Memory leak (5 menit)
#   5. Collect data NORMAL selama 5 menit (recovery)
#   6. Jalankan Pod crash (aktif 15 menit via CronJob)
#   7. Collect data NORMAL selama 5 menit (recovery)
#   8. Jalankan Network latency (3 menit)
#   9. Collect data NORMAL selama 5 menit (recovery)
#
# Total durasi: ~55 menit data collection
# ============================================================

set -e

# Warna output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

NAMESPACE="demo"
FAULT_DIR="$(dirname "$0")"

log_info() {
    echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $1"
}

log_fault() {
    echo -e "${RED}[$(date '+%H:%M:%S')] 🔥 FAULT:${NC} $1"
}

log_normal() {
    echo -e "${GREEN}[$(date '+%H:%M:%S')] ✅ NORMAL:${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠️ WARNING:${NC} $1"
}

# Header
echo "============================================"
echo "  XFSCI Fault Injection Orchestrator"
echo "  $(date)"
echo "============================================"
echo ""

# Pre-check: pastikan cluster berjalan
log_info "Checking cluster status..."
if ! kubectl get nodes > /dev/null 2>&1; then
    log_warn "Kubernetes cluster not reachable! Make sure cluster is running."
    exit 1
fi

NODES=$(kubectl get nodes --no-headers | wc -l)
PODS=$(kubectl get pods -n $NAMESPACE --no-headers 2>/dev/null | wc -l)
log_info "Cluster OK: $NODES nodes, $PODS pods in namespace '$NAMESPACE'"
echo ""

# ===== FASE 1: BASELINE NORMAL DATA =====
log_normal "Collecting BASELINE data (10 minutes)..."
log_info "Pastikan metrics_scraper.py sudah berjalan di terminal lain!"
echo "  → Data label: NORMAL"
sleep 600  # 10 menit

# ===== FASE 2: CPU STRESS =====
log_fault "Deploying CPU Stress fault..."
echo "  → Target: Worker Node Bandung"
echo "  → Duration: 120 seconds"
echo "  → Data label: CRITICAL (cpu-stress)"
kubectl apply -f "$FAULT_DIR/cpu-stress.yaml"
sleep 150  # 2.5 menit (120s fault + 30s buffer)

# Cleanup CPU stress
kubectl delete -f "$FAULT_DIR/cpu-stress.yaml" --ignore-not-found
log_normal "CPU stress cleaned. Recovery period (5 minutes)..."
sleep 300  # 5 menit recovery

# ===== FASE 3: MEMORY LEAK =====
log_fault "Deploying Memory Leak fault..."
echo "  → Target: Worker Node Bandung"
echo "  → Duration: 300 seconds (gradual increase)"
echo "  → Data label: WARNING -> CRITICAL (memory-leak)"
kubectl apply -f "$FAULT_DIR/memory-leak.yaml"
sleep 330  # 5.5 menit

# Cleanup memory leak
kubectl delete -f "$FAULT_DIR/memory-leak.yaml" --ignore-not-found
log_normal "Memory leak cleaned. Recovery period (5 minutes)..."
sleep 300  # 5 menit recovery

# ===== FASE 4: POD CRASH =====
log_fault "Deploying Pod Crash CronJob..."
echo "  → Target: Random pods (frontend, cart, recommendation, payment)"
echo "  → Frequency: Every 3 minutes"
echo "  → Duration: 15 minutes"
echo "  → Data label: CRITICAL (pod-crash)"
kubectl apply -f "$FAULT_DIR/pod-crash.yaml"
sleep 900  # 15 menit

# Cleanup pod crash CronJob
kubectl delete -f "$FAULT_DIR/pod-crash.yaml" --ignore-not-found
log_normal "Pod crash CronJob cleaned. Recovery period (5 minutes)..."
sleep 300  # 5 menit recovery

# ===== FASE 5: NETWORK LATENCY =====
log_fault "Deploying Network Latency fault..."
echo "  → Target: Worker Node Surabaya"
echo "  → Latency: 200ms ± 50ms, 5% packet loss"
echo "  → Duration: 180 seconds"
echo "  → Data label: WARNING (network-latency)"
kubectl apply -f "$FAULT_DIR/network-latency.yaml"
sleep 210  # 3.5 menit

# Cleanup network latency
kubectl delete -f "$FAULT_DIR/network-latency.yaml" --ignore-not-found
log_normal "Network latency cleaned. Final recovery period (5 minutes)..."
sleep 300  # 5 menit recovery terakhir

# ===== SELESAI =====
echo ""
echo "============================================"
echo "  ✅ FAULT INJECTION COMPLETE"
echo "  $(date)"
echo "============================================"
echo ""
echo "Data collection summary:"
echo "  - ~10 min NORMAL baseline"
echo "  - ~2 min CPU stress + 5 min recovery"
echo "  - ~5 min Memory leak + 5 min recovery"
echo "  - ~15 min Pod crashes + 5 min recovery"
echo "  - ~3 min Network latency + 5 min recovery"
echo "  - Total: ~55 minutes of data"
echo ""
echo "Next steps:"
echo "  1. Stop metrics_scraper.py (Ctrl+C)"
echo "  2. Check data/raw/ for collected CSV files"
echo "  3. Run: python data/preprocessors/data_labeler.py"
echo "  4. Run: python data/preprocessors/data_cleaner.py"
