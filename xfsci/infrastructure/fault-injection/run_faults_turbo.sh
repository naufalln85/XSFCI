#!/bin/bash
# ============================================================
# XFSCI Turbo Fault Injection - Sesi 2 (Cepat ~15 menit)
# ============================================================
# Sesi cepat dengan konfigurasi BERBEDA dari sesi 1:
#   - Intensitas berbeda (ringan/berat)
#   - Target node berbeda (Jakarta/Surabaya bukan Bandung)
#   - Durasi fault lebih singkat (45 detik)
#   - Jeda recovery lebih singkat (60 detik)
#
# Tujuan: Memberikan distribusi data kedua agar model AI
# tidak overfitting ke satu pola tunggal.
#
# Cara pakai:
#   chmod +x run_faults_turbo.sh
#   ./run_faults_turbo.sh
#
# PENTING: Pastikan metrics_scraper.py sudah berjalan
#   python3 data/collectors/metrics_scraper.py --interval 2
#
# Estimasi durasi total: ~15 menit
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

NAMESPACE="demo"
FAULT_DIR="$(dirname "$0")"

log_info()   { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $1"; }
log_fault()  { echo -e "${RED}[$(date '+%H:%M:%S')] FAULT:${NC} $1"; }
log_normal() { echo -e "${GREEN}[$(date '+%H:%M:%S')] NORMAL:${NC} $1"; }
log_turbo()  { echo -e "${CYAN}[$(date '+%H:%M:%S')] TURBO:${NC} $1"; }

echo ""
echo "============================================"
echo "  XFSCI TURBO Fault Injection - Sesi 2"
echo "  $(date)"
echo "  Estimasi: ~15 menit"
echo "============================================"
echo ""

# Pre-check
log_info "Checking cluster status..."
if ! kubectl get nodes > /dev/null 2>&1; then
    echo -e "${RED}Kubernetes cluster not reachable!${NC}"
    exit 1
fi

NODES=$(kubectl get nodes --no-headers | wc -l)
PODS=$(kubectl get pods -n $NAMESPACE --no-headers 2>/dev/null | wc -l)
log_info "Cluster OK: $NODES nodes, $PODS pods in '$NAMESPACE'"
echo ""

# Cleanup dari sesi sebelumnya (jika ada)
log_info "Cleaning up any previous turbo fault pods..."
kubectl delete pod fault-cpu-stress-turbo -n $NAMESPACE --ignore-not-found 2>/dev/null || true
kubectl delete pod fault-memory-leak-turbo -n $NAMESPACE --ignore-not-found 2>/dev/null || true
kubectl delete pod fault-network-latency-turbo -n $NAMESPACE --ignore-not-found 2>/dev/null || true
kubectl delete cronjob fault-pod-crash -n $NAMESPACE --ignore-not-found 2>/dev/null || true
sleep 5

# ===== FASE 1: BASELINE (3 menit) =====
log_normal "Collecting BASELINE data (3 minutes)..."
log_info "  Pastikan metrics_scraper.py sudah berjalan dengan --interval 2"
echo "  -> Data label: NORMAL"
sleep 180

# ===== FASE 2: CPU STRESS - RINGAN di JAKARTA =====
log_fault "Deploying CPU Stress TURBO (ringan, Jakarta)..."
log_turbo "  Intensitas: 40% CPU, 2 workers (sesi 1: 90%, 4 workers)"
log_turbo "  Target: Worker Jakarta (sesi 1: Bandung)"
log_turbo "  Durasi: 45 detik (sesi 1: 120s)"
echo "  -> Data label: FAULT_CPU_STRESS"
kubectl apply -f "$FAULT_DIR/cpu-stress-turbo.yaml"
sleep 60  # 45s fault + 15s buffer

# Cleanup
kubectl delete pod fault-cpu-stress-turbo -n $NAMESPACE --force --grace-period=0 2>/dev/null || true
log_normal "CPU stress cleaned. Quick recovery (60s)..."
sleep 60

# ===== FASE 3: MEMORY LEAK - BERAT di SURABAYA =====
log_fault "Deploying Memory Leak TURBO (berat, Surabaya)..."
log_turbo "  Intensitas: 512MB x2 (sesi 1: 256MB x2)"
log_turbo "  Target: Worker Surabaya (sesi 1: Bandung)"
log_turbo "  Durasi: 45 detik (sesi 1: 300s) - leak cepat"
echo "  -> Data label: FAULT_MEMORY_LEAK"
kubectl apply -f "$FAULT_DIR/memory-leak-turbo.yaml"
sleep 60

# Cleanup
kubectl delete pod fault-memory-leak-turbo -n $NAMESPACE --force --grace-period=0 2>/dev/null || true
log_normal "Memory leak cleaned. Quick recovery (60s)..."
sleep 60

# ===== FASE 4: POD CRASH - AGRESIF =====
log_fault "Deploying Pod Crash (reuse CronJob, agresif)..."
log_turbo "  Frekuensi: setiap 1 menit (sesi 1: setiap 3 menit)"
log_turbo "  Durasi: 5 menit (sesi 1: 15 menit)"
echo "  -> Data label: FAULT_POD_CRASH"

# Buat CronJob dengan frekuensi lebih tinggi
cat <<'EOF' | kubectl apply -f -
apiVersion: batch/v1
kind: CronJob
metadata:
  name: fault-pod-crash-turbo
  namespace: demo
  labels:
    app: fault-injection
    fault-type: pod-crash
    session: turbo
spec:
  schedule: "*/1 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: fault-injector
          containers:
            - name: pod-killer
              image: bitnami/kubectl:latest
              command:
                - /bin/sh
                - -c
                - |
                  TARGETS=("frontend" "cartservice" "recommendationservice" "paymentservice" "currencyservice" "emailservice")
                  TARGET=${TARGETS[$RANDOM % ${#TARGETS[@]}]}
                  POD=$(kubectl get pods -n demo -l app=$TARGET -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
                  if [ -n "$POD" ]; then
                    kubectl delete pod $POD -n demo --force --grace-period=0
                    echo "Pod $POD deleted (crash simulation)"
                  fi
          restartPolicy: OnFailure
EOF

sleep 300  # 5 menit pod crash aktif

# Cleanup
kubectl delete cronjob fault-pod-crash-turbo -n $NAMESPACE --ignore-not-found 2>/dev/null || true
log_normal "Pod crash cleaned. Quick recovery (60s)..."
sleep 60

# ===== FASE 5: NETWORK LATENCY - BERAT di JAKARTA =====
log_fault "Deploying Network Latency TURBO (berat, Jakarta)..."
log_turbo "  Latency: 500ms +/- 100ms (sesi 1: 200ms +/- 50ms)"
log_turbo "  Packet loss: 15% (sesi 1: 5%)"
log_turbo "  Target: Worker Jakarta (sesi 1: Surabaya)"
log_turbo "  Durasi: 45 detik (sesi 1: 180s)"
echo "  -> Data label: FAULT_NETWORK_LATENCY"
kubectl apply -f "$FAULT_DIR/network-latency-turbo.yaml"
sleep 60

# Cleanup
kubectl delete pod fault-network-latency-turbo -n $NAMESPACE --force --grace-period=0 2>/dev/null || true
log_normal "Network latency cleaned. Final recovery (60s)..."
sleep 60

# ===== SELESAI =====
echo ""
echo "============================================"
echo "  TURBO FAULT INJECTION COMPLETE!"
echo "  $(date)"
echo "============================================"
echo ""
echo "Sesi Turbo - Data collection summary:"
echo "  - ~3 min NORMAL baseline"
echo "  - ~1 min CPU stress (40%, Jakarta)  + 1 min recovery"
echo "  - ~1 min Memory leak (512MB, Sby)   + 1 min recovery"
echo "  - ~5 min Pod crashes (agresif)      + 1 min recovery"
echo "  - ~1 min Network latency (500ms, Jkt) + 1 min recovery"
echo "  - Total: ~15 menit"
echo ""
echo "Perbedaan dengan Sesi 1:"
echo "  - Intensitas: Ringan (CPU) & Berat (Memory, Network)"
echo "  - Target node: Jakarta & Surabaya (bukan Bandung)"
echo "  - Durasi fault: 45 detik (bukan 120-300s)"
echo "  - Pod crash: setiap 1 menit (bukan 3 menit)"
echo ""
echo "Next steps:"
echo "  1. Stop metrics_scraper.py (Ctrl+C)"
echo "  2. Jalankan pipeline preprocessing multi-sesi:"
echo "     python3 data/preprocessors/data_labeler.py --session turbo"
echo "     python3 data/preprocessors/data_cleaner.py --merge-all"
echo "     python3 data/preprocessors/synthetic_generator.py --target-per-class 5000"
echo "     python3 data/preprocessors/feature_engineer.py"
