#!/usr/bin/env bash
# ==============================================================================
# connect_prometheus.sh — Terowongan Port-Forward Prometheus untuk VM5
# ==============================================================================
# Jika port NodePort 30090 dari VM1 tidak terjangkau (karena firewall / docker network),
# skrip ini membuka port-forward langsung melalui K8s API server (port 6443).
#
# Penggunaan:
#   bash scripts/connect_prometheus.sh          # Jalankan di foreground
#   bash scripts/connect_prometheus.sh --daemon # Jalankan di background
# ==============================================================================

set -e

NAMESPACE="${NAMESPACE:-monitoring}"
LOCAL_PORT="${LOCAL_PORT:-9090}"

echo "🔍 Memeriksa koneksi Kubernetes (kubectl)..."
if ! command -v kubectl &>/dev/null; then
    echo "❌ kubectl tidak ditemukan di VM5!"
    exit 1
fi

echo "🔍 Mencari service Prometheus di namespace '$NAMESPACE'..."
PROM_SVC=$(kubectl get svc -n "$NAMESPACE" -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [ -z "$PROM_SVC" ]; then
    PROM_SVC=$(kubectl get svc -n "$NAMESPACE" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | grep -E 'prometheus-operated|prometheus-k8s|prometheus' | grep -v -E 'grafana|alertmanager|node-exporter|kube-state' | head -n 1 || true)
fi

if [ -z "$PROM_SVC" ]; then
    PROM_SVC="prometheus-k8s"
fi

echo "✅ Ditemukan service: $PROM_SVC"
echo "🔌 Membuka terowongan port-forward ke svc/$PROM_SVC (port $LOCAL_PORT)..."
echo "📊 Endpoint Prometheus VM5: http://127.0.0.1:$LOCAL_PORT"

if [[ "$1" == "--daemon" || "$1" == "-d" ]]; then
    nohup kubectl port-forward -n "$NAMESPACE" "svc/$PROM_SVC" "${LOCAL_PORT}:9090" --address 0.0.0.0 > /tmp/prom-portforward.log 2>&1 &
    sleep 2
    if curl -s "http://127.0.0.1:${LOCAL_PORT}/-/healthy" &>/dev/null; then
        echo "✅ Terowongan Prometheus aktif di background (PID: $!)"
    else
        echo "⚠️ Port-forward dimulai di background. Periksa log: cat /tmp/prom-portforward.log"
    fi
else
    echo "💡 Tekan Ctrl+C untuk menghentikan terowongan."
    exec kubectl port-forward -n "$NAMESPACE" "svc/$PROM_SVC" "${LOCAL_PORT}:9090" --address 0.0.0.0
fi
