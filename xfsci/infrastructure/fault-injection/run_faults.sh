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
#   6. Hapus lima pod pilihan, catat UID dan event, jeda 3 menit
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
SESSION_ID="${XFSCI_SESSION_ID:-manual}"

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

emit_fault_event() {
    local fault="$1" phase="$2" targets="$3" node="${4:--}"
    printf 'XFSCI_FAULT_EVENT session_id=%s fault=%s phase=%s timestamp=%s targets=%s node=%s\n' \
        "$SESSION_ID" "$fault" "$phase" "$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')" "$targets" "$node"
}

fault_pod_node() {
    local pod="$1"
    kubectl get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true
}

inject_observed_pod_crashes() {
    local targets=(frontend cartservice recommendationservice paymentservice)
    local attempt service pod uid node phase candidate candidate_pod candidate_uid candidate_node timestamp event_reasons
    for attempt in 1 2 3 4 5; do
        service="${targets[$((RANDOM % ${#targets[@]}))]}"
        mapfile -t candidates < <(
            kubectl get pods -n "$NAMESPACE" -l "app=$service" \
                -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.uid}{"|"}{.spec.nodeName}{"|"}{.status.phase}{"\n"}{end}'
        )
        local running=()
        for candidate in "${candidates[@]}"; do
            IFS='|' read -r candidate_pod candidate_uid candidate_node phase <<< "$candidate"
            if [[ "$phase" == "Running" && -n "$candidate_pod" ]]; then
                running+=("$candidate_pod|$candidate_uid|$candidate_node")
            fi
        done
        if ((${#running[@]} == 0)); then
            log_warn "No Running pod found for selected crash target '$service'; aborting rather than recording a false event."
            return 1
        fi
        IFS='|' read -r pod uid node <<< "${running[$((RANDOM % ${#running[@]}))]}"
        if [[ -z "$uid" || -z "$node" ]]; then
            log_warn "Pod identity is incomplete for $pod; refusing an untraceable crash injection."
            return 1
        fi
        log_fault "Deleting one pod: service=$service pod=$pod uid=$uid node=$node"
        if ! kubectl delete pod "$pod" -n "$NAMESPACE" --wait=false; then
            log_warn "Delete failed for $pod; no crash event will be recorded."
            return 1
        fi
        timestamp="$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')"
        sleep 1
        event_reasons="$(kubectl get events -n "$NAMESPACE" \
            --field-selector "involvedObject.uid=$uid" \
            -o jsonpath='{range .items[*]}{.reason}{","}{end}' 2>/dev/null | tr -cd '[:alnum:]_,.-')"
        event_reasons="${event_reasons%,}"
        if [[ -z "$event_reasons" ]]; then event_reasons="DeleteRequested"; fi
        printf 'XFSCI_POD_EVENT session_id=%s fault=pod_crash timestamp=%s service=%s pod=%s uid=%s node=%s reason=%s\n' \
            "$SESSION_ID" "$timestamp" "$service" "$pod" "$uid" "${node:--}" "$event_reasons"
        if ((attempt < 5)); then sleep 180; fi
    done
}

cleanup_faults() {
    log_warn "Cleaning up fault-injection resources in namespace '$NAMESPACE'..."
    kubectl delete -f "$FAULT_DIR/cpu-stress.yaml" --ignore-not-found >/dev/null 2>&1 || true
    kubectl delete -f "$FAULT_DIR/memory-leak.yaml" --ignore-not-found >/dev/null 2>&1 || true
    kubectl delete -f "$FAULT_DIR/network-latency.yaml" --ignore-not-found >/dev/null 2>&1 || true
    kubectl delete -f "$FAULT_DIR/pod-crash.yaml" --ignore-not-found >/dev/null 2>&1 || true
}

trap cleanup_faults EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

# Remove any older CronJob-based injector before collection starts. Its pod
# deletions are not tied to an exact recorded pod UID and must not overlap.
kubectl delete -f "$FAULT_DIR/pod-crash.yaml" --ignore-not-found >/dev/null 2>&1 || true

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
echo "  → Target: Worker 2"
echo "  → Duration: 120 seconds"
echo "  → Data label: CRITICAL (cpu-stress)"
kubectl apply -f "$FAULT_DIR/cpu-stress.yaml"
kubectl wait --for=condition=Ready pod/fault-cpu-stress -n "$NAMESPACE" --timeout=90s
CPU_FAULT_NODE="$(fault_pod_node fault-cpu-stress)"
emit_fault_event "cpu_stress" "start" "adservice,cartservice,productcatalogservice,redis-cart" "$CPU_FAULT_NODE"
sleep 150  # 2.5 menit (120s fault + 30s buffer)

# Cleanup CPU stress
kubectl delete -f "$FAULT_DIR/cpu-stress.yaml" --ignore-not-found
emit_fault_event "cpu_stress" "end" "adservice,cartservice,productcatalogservice,redis-cart" "$CPU_FAULT_NODE"
log_normal "CPU stress cleaned. Recovery period (5 minutes)..."
sleep 300  # 5 menit recovery

# ===== FASE 3: MEMORY LEAK =====
log_fault "Deploying Memory Leak fault..."
echo "  → Target: Worker 2"
echo "  → Duration: 300 seconds (gradual increase)"
echo "  → Data label: WARNING -> CRITICAL (memory-leak)"
kubectl apply -f "$FAULT_DIR/memory-leak.yaml"
kubectl wait --for=condition=Ready pod/fault-memory-leak -n "$NAMESPACE" --timeout=90s
MEMORY_FAULT_NODE="$(fault_pod_node fault-memory-leak)"
emit_fault_event "memory_leak" "start" "adservice,cartservice,productcatalogservice,redis-cart" "$MEMORY_FAULT_NODE"
sleep 330  # 5.5 menit

# Cleanup memory leak
kubectl delete -f "$FAULT_DIR/memory-leak.yaml" --ignore-not-found
emit_fault_event "memory_leak" "end" "adservice,cartservice,productcatalogservice,redis-cart" "$MEMORY_FAULT_NODE"
log_normal "Memory leak cleaned. Recovery period (5 minutes)..."
sleep 300  # 5 menit recovery

# ===== FASE 4: POD CRASH =====
log_fault "Starting bounded event-logged pod deletions..."
emit_fault_event "pod_crash" "start" "frontend,cartservice,recommendationservice,paymentservice"
echo "  → Target: Random pods (frontend, cart, recommendation, payment)"
echo "  → Frequency: Every 3 minutes"
echo "  → Duration: 15 minutes"
echo "  → Data label: CRITICAL (pod-crash)"
inject_observed_pod_crashes

# Ensure the old CronJob remains absent during this session.
kubectl delete -f "$FAULT_DIR/pod-crash.yaml" --ignore-not-found
emit_fault_event "pod_crash" "end" "frontend,cartservice,recommendationservice,paymentservice"
log_normal "Pod crash phase complete. Recovery period (5 minutes)..."
sleep 300  # 5 menit recovery

# ===== FASE 5: NETWORK LATENCY =====
log_fault "Deploying Network Latency fault..."
echo "  → Target: Worker 3"
echo "  → Latency: 200ms ± 50ms, 5% packet loss"
echo "  → Duration: 180 seconds"
echo "  → Data label: WARNING (network-latency)"
kubectl apply -f "$FAULT_DIR/network-latency.yaml"
kubectl wait --for=condition=Ready pod/fault-network-latency -n "$NAMESPACE" --timeout=90s
NETWORK_FAULT_NODE="$(fault_pod_node fault-network-latency)"
emit_fault_event "net_latency" "start" "checkoutservice,currencyservice,emailservice,shippingservice" "$NETWORK_FAULT_NODE"
sleep 210  # 3.5 menit

# Cleanup network latency
kubectl delete -f "$FAULT_DIR/network-latency.yaml" --ignore-not-found
emit_fault_event "net_latency" "end" "checkoutservice,currencyservice,emailservice,shippingservice" "$NETWORK_FAULT_NODE"
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
