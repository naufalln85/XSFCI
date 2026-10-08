#!/usr/bin/env bash
# ============================================================
# XFSCI Chaos Injection Script
# ============================================================
# Menyuntikkan gangguan nyata ke pod Kubernetes untuk menguji
# pipeline self-healing XFSCI secara end-to-end.
#
# Penggunaan:
#   ./inject_chaos.sh <tipe_gangguan> <deployment> [namespace] [durasi_detik]
#
# Tipe gangguan yang tersedia:
#   memory_leak  - Menghabiskan RAM pod secara bertahap
#   cpu_stress   - Membebankan 100% CPU
#   crash_loop   - Membunuh proses utama agar pod restart berulang
#   latency      - Menambahkan delay jaringan (butuh tc)
#
# Contoh:
#   ./inject_chaos.sh memory_leak cartservice demo 120
#   ./inject_chaos.sh cpu_stress frontend demo 60
# ============================================================

set -euo pipefail

# ──────────────────────────────────────────────────────
# Konfigurasi default
# ──────────────────────────────────────────────────────
CHAOS_TYPE="${1:-help}"
DEPLOYMENT="${2:-cartservice}"
NAMESPACE="${3:-demo}"
DURATION="${4:-60}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

banner() {
    echo ""
    echo -e "${RED}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║     🔥 XFSCI CHAOS INJECTION ENGINE 🔥              ║${NC}"
    echo -e "${RED}║     Suntik Gangguan untuk Uji Self-Healing           ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
}

usage() {
    banner
    echo -e "${CYAN}Penggunaan:${NC}"
    echo "  $0 <tipe_gangguan> <deployment> [namespace] [durasi_detik]"
    echo ""
    echo -e "${CYAN}Tipe gangguan:${NC}"
    echo "  memory_leak  - Mengisi RAM pod hingga penuh secara bertahap"
    echo "  cpu_stress   - Membebani CPU pod hingga 100%"
    echo "  crash_loop   - Membunuh proses utama pod agar restart terus"
    echo "  latency      - Menambahkan delay jaringan 500ms (butuh tc)"
    echo ""
    echo -e "${CYAN}Contoh:${NC}"
    echo "  $0 memory_leak cartservice demo 120"
    echo "  $0 cpu_stress frontend demo 60"
    echo ""
    exit 0
}

# ──────────────────────────────────────────────────────
# Temukan pod target
# ──────────────────────────────────────────────────────
find_pod() {
    local dep="$1"
    local ns="$2"

    # Coba label app=<dep>
    local pod
    pod=$(kubectl get pods -n "$ns" -l "app=${dep}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

    # Fallback: coba label app.kubernetes.io/name=<dep>
    if [[ -z "$pod" ]]; then
        pod=$(kubectl get pods -n "$ns" -l "app.kubernetes.io/name=${dep}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    fi

    # Fallback: cari berdasarkan prefix nama
    if [[ -z "$pod" ]]; then
        pod=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep "^${dep}" | head -1 | awk '{print $1}' || true)
    fi

    echo "$pod"
}

# ──────────────────────────────────────────────────────
# Chaos: Memory Leak (mengisi RAM bertahap)
# ──────────────────────────────────────────────────────
inject_memory_leak() {
    local pod="$1"
    local ns="$2"
    local dur="$3"

    echo -e "${RED}💣 Injecting MEMORY LEAK into ${pod}/${ns} for ${dur}s...${NC}"
    echo -e "${YELLOW}   Metode: Alokasi memori bertahap menggunakan shell /dev/urandom${NC}"

    # Alokasi memori 50MB setiap 5 detik
    kubectl exec -n "$ns" "$pod" -- sh -c "
        echo '🔥 XFSCI Chaos: Memory leak started';
        i=0;
        while [ \$i -lt $((dur / 5)) ]; do
            head -c 52428800 /dev/urandom > /tmp/chaos_mem_\$i 2>/dev/null || true;
            echo \"  Allocated block \$i (50MB)\";
            i=\$((i + 1));
            sleep 5;
        done;
        echo '🛑 XFSCI Chaos: Memory leak injection completed';
    " &

    local chaos_pid=$!
    echo -e "${GREEN}   Chaos PID: ${chaos_pid}${NC}"
    echo -e "${CYAN}   Gangguan akan berjalan selama ${dur} detik di latar belakang.${NC}"
    echo -e "${CYAN}   Untuk menghentikan manual: kill ${chaos_pid}${NC}"
}

# ──────────────────────────────────────────────────────
# Chaos: CPU Stress (bebankan 100% CPU)
# ──────────────────────────────────────────────────────
inject_cpu_stress() {
    local pod="$1"
    local ns="$2"
    local dur="$3"

    echo -e "${RED}💣 Injecting CPU STRESS into ${pod}/${ns} for ${dur}s...${NC}"
    echo -e "${YELLOW}   Metode: Infinite loop di semua CPU core${NC}"

    kubectl exec -n "$ns" "$pod" -- sh -c "
        echo '🔥 XFSCI Chaos: CPU stress started';
        # Jalankan busy loop, akan dihentikan setelah timeout
        timeout ${dur} sh -c 'while true; do :; done' &
        timeout ${dur} sh -c 'while true; do :; done' &
        echo '   2 CPU stress workers spawned for ${dur}s';
        wait;
        echo '🛑 XFSCI Chaos: CPU stress completed';
    " &

    echo -e "${GREEN}   CPU stress dimulai (2 workers, ${dur}s)${NC}"
}

# ──────────────────────────────────────────────────────
# Chaos: Crash Loop (kill proses utama)
# ──────────────────────────────────────────────────────
inject_crash_loop() {
    local pod="$1"
    local ns="$2"
    local dur="$3"

    echo -e "${RED}💣 Injecting CRASH LOOP into ${pod}/${ns}...${NC}"
    echo -e "${YELLOW}   Metode: Kill PID 1 (proses utama container)${NC}"

    # Setiap 10 detik, bunuh PID 1 agar pod restart
    local iterations=$((dur / 10))
    for i in $(seq 1 "$iterations"); do
        echo -e "${RED}   💀 Kill attempt ${i}/${iterations}...${NC}"
        kubectl exec -n "$ns" "$pod" -- kill 1 2>/dev/null || true
        sleep 10
        # Refresh pod name karena setelah restart nama bisa berubah
        pod=$(find_pod "$DEPLOYMENT" "$ns")
        if [[ -z "$pod" ]]; then
            echo -e "${YELLOW}   Pod belum kembali, menunggu...${NC}"
            sleep 5
            pod=$(find_pod "$DEPLOYMENT" "$ns")
        fi
    done

    echo -e "${GREEN}   Crash loop injection selesai.${NC}"
}

# ──────────────────────────────────────────────────────
# Chaos: Network Latency (tambah delay 500ms)
# ──────────────────────────────────────────────────────
inject_latency() {
    local pod="$1"
    local ns="$2"
    local dur="$3"

    echo -e "${RED}💣 Injecting NETWORK LATENCY into ${pod}/${ns} for ${dur}s...${NC}"
    echo -e "${YELLOW}   Metode: tc qdisc netem delay 500ms (butuh NET_ADMIN cap)${NC}"

    kubectl exec -n "$ns" "$pod" -- sh -c "
        tc qdisc add dev eth0 root netem delay 500ms 2>/dev/null && \
        echo '🔥 Added 500ms latency to eth0' || \
        echo '⚠️ tc not available (container needs NET_ADMIN capability)';
        sleep ${dur};
        tc qdisc del dev eth0 root 2>/dev/null || true;
        echo '🛑 Latency injection removed';
    " &

    echo -e "${GREEN}   Latency injection dimulai (500ms delay, ${dur}s)${NC}"
}

# ──────────────────────────────────────────────────────
# Cleanup: Bersihkan file chaos di pod
# ──────────────────────────────────────────────────────
cleanup_chaos() {
    local dep="$1"
    local ns="$2"

    echo -e "${CYAN}🧹 Cleaning up chaos artifacts from ${dep}/${ns}...${NC}"
    local pod
    pod=$(find_pod "$dep" "$ns")
    if [[ -n "$pod" ]]; then
        kubectl exec -n "$ns" "$pod" -- sh -c "rm -f /tmp/chaos_mem_* 2>/dev/null; echo 'Cleaned'" 2>/dev/null || true
        echo -e "${GREEN}   Cleanup complete.${NC}"
    else
        echo -e "${YELLOW}   Pod not found, skipping cleanup.${NC}"
    fi
}

# ──────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────
if [[ "$CHAOS_TYPE" == "help" || "$CHAOS_TYPE" == "-h" || "$CHAOS_TYPE" == "--help" ]]; then
    usage
fi

banner

echo -e "${CYAN}📋 Konfigurasi Chaos:${NC}"
echo -e "   Tipe     : ${YELLOW}${CHAOS_TYPE}${NC}"
echo -e "   Target   : ${YELLOW}${DEPLOYMENT}${NC}"
echo -e "   Namespace: ${YELLOW}${NAMESPACE}${NC}"
echo -e "   Durasi   : ${YELLOW}${DURATION}s${NC}"
echo ""

# Temukan pod
POD=$(find_pod "$DEPLOYMENT" "$NAMESPACE")
if [[ -z "$POD" ]]; then
    echo -e "${RED}❌ ERROR: Tidak menemukan pod untuk deployment '${DEPLOYMENT}' di namespace '${NAMESPACE}'${NC}"
    echo -e "${YELLOW}   Coba cek: kubectl get pods -n ${NAMESPACE}${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Pod ditemukan: ${POD}${NC}"
echo ""

# Status sebelum chaos
echo -e "${CYAN}📊 Status SEBELUM chaos:${NC}"
kubectl get pods -n "$NAMESPACE" -l "app=${DEPLOYMENT}" -o wide 2>/dev/null || \
kubectl get pods -n "$NAMESPACE" | grep "$DEPLOYMENT" || true
echo ""

# Dispatch chaos
case "$CHAOS_TYPE" in
    memory_leak|mem)            inject_memory_leak "$POD" "$NAMESPACE" "$DURATION" ;;
    cpu_stress|cpu)             inject_cpu_stress "$POD" "$NAMESPACE" "$DURATION" ;;
    crash_loop|pod_crash|crash) inject_crash_loop "$POD" "$NAMESPACE" "$DURATION" ;;
    latency|network_latency)    inject_latency "$POD" "$NAMESPACE" "$DURATION" ;;
    cleanup)                    cleanup_chaos "$DEPLOYMENT" "$NAMESPACE" ;;
    *)
        echo -e "${RED}❌ Tipe gangguan tidak dikenal: ${CHAOS_TYPE}${NC}"
        usage
        ;;
esac

echo ""
echo -e "${CYAN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Chaos injection '${CHAOS_TYPE}' berhasil dijalankan!${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${YELLOW}📌 Langkah selanjutnya:${NC}"
echo -e "   1. Tunggu beberapa detik agar metrik Prometheus terupdate"
echo -e "   2. Jalankan Orchestrator untuk menguji self-healing:"
echo -e "      ${GREEN}python3 -m agent.orchestrator -d ${DEPLOYMENT}${NC}"
echo -e "   3. Untuk membersihkan sisa chaos:"
echo -e "      ${GREEN}$0 cleanup ${DEPLOYMENT} ${NAMESPACE}${NC}"
echo ""
