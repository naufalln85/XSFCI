#!/bin/bash
# ============================================================
# XFSCI Cluster Emergency Recovery Script
# ============================================================
# Menangani semua kondisi error cluster akibat perubahan IP:
#   1. Diagnosa kondisi cluster saat ini
#   2. Deteksi IP baru secara otomatis
#   3. Update kubeconfig & API server certificate
#   4. Restart control-plane & rejoin workers
#   5. Redeploy semua pod (monitoring + workload)
#
# Cara pakai (di Master Node):
#   chmod +x scripts/emergency_recovery.sh
#   sudo ./scripts/emergency_recovery.sh
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$PROJECT_DIR/configs/config.yaml"

log_step() { echo -e "\n${BLUE}═══ $1 ═══${NC}"; }
log_ok()   { echo -e "  ${GREEN}✅ $1${NC}"; }
log_warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
log_err()  { echo -e "  ${RED}❌ $1${NC}"; }
log_info() { echo -e "  ${CYAN}→  $1${NC}"; }

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   XFSCI Cluster Emergency Recovery                          ║"
echo "║   Fixes: IP change / API server down / All pods dead        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# ============================================================
# STEP 0: Deteksi IP baru
# ============================================================
log_step "STEP 0/7: Detecting Current Node IPs"

# Dapatkan IP baru dari interface yang aktif (bukan loopback)
NEW_MASTER_IP=$(ip -4 addr show | grep -oP '(?<=inet\s)\d+\.\d+\.\d+\.\d+' | grep -v '127.0.0.1' | grep -v '^10\.96\.' | grep -v '^10\.244\.' | head -1)

if [ -z "$NEW_MASTER_IP" ]; then
    # Coba cara alternatif
    NEW_MASTER_IP=$(hostname -I | awk '{print $1}')
fi

if [ -z "$NEW_MASTER_IP" ]; then
    log_err "Tidak bisa mendeteksi IP! Set manual:"
    echo "   export NEW_MASTER_IP=<IP_ANDA>"
    exit 1
fi

log_ok "Detected Master IP: ${GREEN}${NEW_MASTER_IP}${NC}"

# Cek apakah ini benar-benar IP yang berubah
OLD_IP_IN_KUBECONFIG=$(grep "server:" ~/.kube/config 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || echo "UNKNOWN")
log_info "Old IP in kubeconfig: ${OLD_IP_IN_KUBECONFIG}"
log_info "New detected IP: ${NEW_MASTER_IP}"

if [ "$OLD_IP_IN_KUBECONFIG" == "$NEW_MASTER_IP" ]; then
    log_warn "IP sama dengan kubeconfig. Mungkin masalah lain (bukan IP change)."
    log_info "Cek: systemctl status kubelet / containerd"
fi

# ============================================================
# STEP 1: Diagnosa cepat
# ============================================================
log_step "STEP 1/7: Quick Diagnosis"

KUBELET_STATUS=$(systemctl is-active kubelet 2>/dev/null || echo "unknown")
CONTAINERD_STATUS=$(systemctl is-active containerd 2>/dev/null || echo "unknown")

log_info "kubelet status:     ${KUBELET_STATUS}"
log_info "containerd status:  ${CONTAINERD_STATUS}"

# Test koneksi ke API server
if kubectl get nodes --request-timeout=5s > /dev/null 2>&1; then
    log_ok "kubectl masih bisa terhubung ke cluster!"
    CLUSTER_REACHABLE=true
else
    log_warn "kubectl tidak bisa terhubung (IP berubah atau API server down)"
    CLUSTER_REACHABLE=false
fi

# ============================================================
# STEP 2: Fix kubelet & containerd
# ============================================================
log_step "STEP 2/7: Restart Core Services"

if [ "$CONTAINERD_STATUS" != "active" ]; then
    log_info "Starting containerd..."
    sudo systemctl start containerd
    sleep 2
fi
log_ok "containerd: $(systemctl is-active containerd)"

if [ "$KUBELET_STATUS" != "active" ]; then
    log_info "Starting kubelet..."
    sudo systemctl start kubelet
    sleep 3
fi
log_ok "kubelet: $(systemctl is-active kubelet)"

# ============================================================
# STEP 3: Fix kubeconfig & API Server certificate
# ============================================================
log_step "STEP 3/7: Fixing kubeconfig & API Server Certificate"

if [ "$CLUSTER_REACHABLE" == "false" ]; then
    log_info "Updating kubeconfig server URL ke IP baru: ${NEW_MASTER_IP}..."

    # Backup kubeconfig lama
    cp ~/.kube/config ~/.kube/config.backup.$(date +%Y%m%d_%H%M%S) 2>/dev/null || true

    # Update server URL di kubeconfig
    sed -i "s|server: https://[0-9.]*:6443|server: https://${NEW_MASTER_IP}:6443|g" ~/.kube/config
    log_ok "kubeconfig server URL updated"

    # Cek apakah koneksi sudah berhasil dengan IP baru
    if kubectl get nodes --request-timeout=5s > /dev/null 2>&1; then
        log_ok "Koneksi berhasil dengan IP baru!"
        CLUSTER_REACHABLE=true
    else
        log_warn "Masih tidak bisa terhubung. Perlu update API server certificate..."

        # ============================================================
        # FIX UTAMA: Regenerate API server certificate dengan SAN baru
        # ============================================================
        log_info "Regenerating API server certificate dengan SAN ${NEW_MASTER_IP}..."

        # Backup certificate lama
        sudo cp /etc/kubernetes/pki/apiserver.crt /etc/kubernetes/pki/apiserver.crt.bak 2>/dev/null || true
        sudo cp /etc/kubernetes/pki/apiserver.key /etc/kubernetes/pki/apiserver.key.bak 2>/dev/null || true

        # Hapus certificate lama (akan di-regenerate oleh kubeadm)
        sudo rm -f /etc/kubernetes/pki/apiserver.crt /etc/kubernetes/pki/apiserver.key

        # Regenerate menggunakan kubeadm
        sudo kubeadm init phase certs apiserver \
            --apiserver-advertise-address="${NEW_MASTER_IP}" \
            --apiserver-cert-extra-sans="${NEW_MASTER_IP},localhost,127.0.0.1"
        log_ok "API server certificate regenerated dengan SAN: ${NEW_MASTER_IP}"

        # Restart API server (static pod akan auto-restart setelah manifest diubah)
        log_info "Restarting API server static pod..."
        sudo crictl rm -f $(sudo crictl ps | grep kube-apiserver | awk '{print $1}') 2>/dev/null || true
        sleep 10  # Tunggu API server restart

        # Update kubeconfig admin
        sudo kubeadm init phase kubeconfig admin \
            --apiserver-advertise-address="${NEW_MASTER_IP}" 2>/dev/null || true
        sudo cp /etc/kubernetes/admin.conf ~/.kube/config 2>/dev/null || true
        sudo chown $(id -u):$(id -g) ~/.kube/config 2>/dev/null || true

        log_ok "kubeconfig admin di-refresh"
        sleep 5

        # Test lagi
        if kubectl get nodes --request-timeout=10s > /dev/null 2>&1; then
            log_ok "Koneksi berhasil setelah regenerate certificate!"
            CLUSTER_REACHABLE=true
        else
            log_err "Masih tidak bisa connect. Coba STEP 3B: kubeadm full restart"
            CLUSTER_REACHABLE=false
        fi
    fi
fi

# ============================================================
# STEP 3B: Jika masih gagal → restart semua control-plane pods
# ============================================================
if [ "$CLUSTER_REACHABLE" == "false" ]; then
    log_step "STEP 3B/7: Force Restart Control-Plane Pods"

    log_info "Menghentikan semua container control-plane..."
    sudo crictl rm -f $(sudo crictl ps | grep -E "kube-apiserver|kube-controller|kube-scheduler|etcd" | awk '{print $1}') 2>/dev/null || true
    
    log_info "Menunggu static pods restart otomatis oleh kubelet (30s)..."
    sleep 30

    if kubectl get nodes --request-timeout=10s > /dev/null 2>&1; then
        log_ok "Control-plane berhasil restart!"
        CLUSTER_REACHABLE=true
    else
        log_err "Control-plane masih bermasalah."
        log_warn "Coba jalankan manual:"
        echo ""
        echo "    sudo systemctl restart kubelet"
        echo "    sleep 30"
        echo "    kubectl get nodes"
        echo ""
        echo "  Jika masih gagal, mungkin perlu kubeadm reset + re-init:"
        echo "    sudo kubeadm reset --force"
        echo "    sudo kubeadm init --pod-network-cidr=10.244.0.0/16 --apiserver-advertise-address=${NEW_MASTER_IP}"
        exit 1
    fi
fi

# ============================================================
# STEP 4: Update node status & labels
# ============================================================
log_step "STEP 4/7: Verifying All Nodes"

echo ""
echo "  Current node status:"
kubectl get nodes -o wide
echo ""

# Cek node yang NotReady
NOT_READY=$(kubectl get nodes --no-headers | grep -v " Ready" | awk '{print $1}' || true)
if [ -n "$NOT_READY" ]; then
    log_warn "Node tidak Ready: ${NOT_READY}"
    log_info "Worker node perlu menjalankan perintah rejoin. Lihat output di bawah:"
    echo ""
    echo -e "  ${YELLOW}Di SETIAP worker node yang tidak Ready, jalankan:${NC}"
    echo ""
    echo "    sudo systemctl restart kubelet"
    echo ""
    echo "  Jika masih tidak Ready setelah 60 detik, jalankan token rejoin:"
    
    # Generate token rejoin baru
    JOIN_CMD=$(kubeadm token create --print-join-command 2>/dev/null || echo "kubeadm token create --print-join-command (gagal, cek kubeadm)")
    echo ""
    echo -e "  ${CYAN}--- PERINTAH REJOIN WORKER (copy & paste ke worker node) ---${NC}"
    echo ""
    echo "    sudo kubeadm reset --force"
    echo "    sudo ${JOIN_CMD}"
    echo ""
    echo -e "  ${CYAN}--------------------------------------------------------------${NC}"
    echo ""
    log_info "Menunggu worker nodes (60s)..."
    sleep 60
    echo ""
    echo "  Node status setelah menunggu:"
    kubectl get nodes -o wide
fi

log_ok "Node check selesai"

# ============================================================
# STEP 5: Fix & Redeploy Monitoring Stack
# ============================================================
log_step "STEP 5/7: Redeploying Monitoring Stack"

# Bersihkan pod yang Stuck/Unknown/Error
log_info "Menghapus pod yang stuck/unknown/error..."
kubectl delete pods -n monitoring --field-selector=status.phase=Failed 2>/dev/null || true
kubectl delete pods -n monitoring --field-selector=status.phase=Unknown 2>/dev/null || true
kubectl get pods -n monitoring --no-headers 2>/dev/null | grep -E "Error|OOMKilled|ImagePullBackOff|CrashLoopBackOff|Evicted" | awk '{print $1}' | xargs -r kubectl delete pod -n monitoring 2>/dev/null || true

# Update Helm repos
log_info "Updating Helm repos..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo update 2>/dev/null || true
log_ok "Helm repos updated"

# Cek apakah Helm chart sudah ada
HELM_VALUES_DIR="$PROJECT_DIR/infrastructure/helm-values"

# Deploy Prometheus + Grafana
echo ""
log_info "Deploying Prometheus + Grafana..."
helm uninstall prometheus -n monitoring 2>/dev/null || true
kubectl delete job -n monitoring --all --force --grace-period=0 2>/dev/null || true
sleep 3

if [ -f "$HELM_VALUES_DIR/prometheus-values.yaml" ]; then
    helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
        --namespace monitoring \
        --create-namespace \
        -f "$HELM_VALUES_DIR/prometheus-values.yaml" \
        --timeout 300s \
        --wait || {
        log_warn "Prometheus deploy timeout. Cek status manual: kubectl get pods -n monitoring"
    }
else
    # Deploy dengan nilai default minimal
    log_warn "prometheus-values.yaml tidak ditemukan, menggunakan defaults..."
    helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
        --namespace monitoring \
        --create-namespace \
        --set grafana.adminPassword=xfsci-admin-2026 \
        --set grafana.service.type=NodePort \
        --set grafana.service.nodePort=30030 \
        --set prometheus.prometheusSpec.service.type=NodePort \
        --set prometheus.service.type=NodePort \
        --set prometheus.service.nodePort=30090 \
        --set alertmanager.enabled=false \
        --timeout 300s \
        --wait || log_warn "Prometheus deploy timeout"
fi
log_ok "Prometheus + Grafana deployed"

# Deploy Loki
echo ""
log_info "Deploying Loki + Promtail..."
if [ -f "$HELM_VALUES_DIR/loki-values.yaml" ]; then
    helm upgrade --install loki grafana/loki-stack \
        --namespace monitoring \
        -f "$HELM_VALUES_DIR/loki-values.yaml" \
        --timeout 180s || log_warn "Loki deploy timeout"
else
    helm upgrade --install loki grafana/loki-stack \
        --namespace monitoring \
        --set loki.persistence.enabled=false \
        --timeout 180s || log_warn "Loki deploy timeout"
fi
log_ok "Loki + Promtail deployed"

# ============================================================
# STEP 6: Redeploy Online Boutique workload
# ============================================================
log_step "STEP 6/7: Redeploying Online Boutique Workload"

WORKLOAD_FILE="$PROJECT_DIR/infrastructure/workloads/online-boutique.yaml"
if [ -f "$WORKLOAD_FILE" ]; then
    log_info "Deleting old boutique pods..."
    kubectl delete -f "$WORKLOAD_FILE" --ignore-not-found=true 2>/dev/null || true
    sleep 3
    log_info "Deploying Online Boutique..."
    kubectl apply -f "$WORKLOAD_FILE"
    log_ok "Online Boutique applied"
else
    log_warn "online-boutique.yaml tidak ditemukan di $WORKLOAD_FILE"
fi

# ============================================================
# STEP 7: Update configs & verify Ollama
# ============================================================
log_step "STEP 7/7: Update Config & Final Verification"

# Update config.yaml
if [ -f "$CONFIG_FILE" ]; then
    sed -i -E "s|url: \"http://[0-9.]+:30090\"|url: \"http://${NEW_MASTER_IP}:30090\"|g" "$CONFIG_FILE" 2>/dev/null || true
    sed -i -E "s|base_url: \"http://[0-9.]+:11434\"|base_url: \"http://${NEW_MASTER_IP}:11434\"|g" "$CONFIG_FILE" 2>/dev/null || true
    log_ok "configs/config.yaml updated → Master IP: ${NEW_MASTER_IP}"
fi

# Cek Ollama
if curl -s --max-time 3 "http://${NEW_MASTER_IP}:11434/api/version" > /dev/null; then
    OLLAMA_VER=$(curl -s "http://${NEW_MASTER_IP}:11434/api/version" | grep -o '"version":"[^"]*"' | cut -d'"' -f4 || echo "OK")
    log_ok "Ollama API aktif! Version: ${OLLAMA_VER}"
else
    log_warn "Ollama API tidak merespons. Coba: sudo systemctl start ollama"
fi

# Tampilkan status akhir
echo ""
echo "  📦 Monitoring pods:"
kubectl get pods -n monitoring --no-headers 2>/dev/null | awk '{printf "    %-55s %s\n", $1, $3}' || true
echo ""
echo "  🛒 Demo/workload pods:"
kubectl get pods -n demo --no-headers 2>/dev/null | awk '{printf "    %-55s %s\n", $1, $3}' || true
echo ""

MON_RUNNING=$(kubectl get pods -n monitoring --no-headers 2>/dev/null | grep -c "Running" || echo "0")
DEMO_RUNNING=$(kubectl get pods -n demo --no-headers 2>/dev/null | grep -c "Running" || echo "0")

echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   ✅ XFSCI CLUSTER EMERGENCY RECOVERY COMPLETE!             ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  Master IP (BARU):  ${NEW_MASTER_IP}                               ║"
echo "║  Monitoring Pods:   ${MON_RUNNING} running                              ║"
echo "║  Workload Pods:     ${DEMO_RUNNING} running                              ║"
echo "║                                                              ║"
echo "║  Access Points:                                              ║"
echo "║  ┌────────────────────────────────────────────────────────┐  ║"
echo "║  │ Prometheus  → http://${NEW_MASTER_IP}:30090                │  ║"
echo "║  │ Grafana     → http://${NEW_MASTER_IP}:30030                │  ║"
echo "║  │ Loki        → http://${NEW_MASTER_IP}:30100                │  ║"
echo "║  │ Frontend    → http://${NEW_MASTER_IP}:30080                │  ║"
echo "║  │ Ollama API  → http://${NEW_MASTER_IP}:11434                │  ║"
echo "║  └────────────────────────────────────────────────────────┘  ║"
echo "║  (Grafana Login: admin / xfsci-admin-2026)                   ║"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
