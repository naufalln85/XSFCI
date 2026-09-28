#!/bin/bash
# ============================================================
# XFSCI Deploy Monitoring Stack
# ============================================================
# Script ini men-deploy seluruh observability stack:
#   - Prometheus + Grafana (kube-prometheus-stack)
#   - Loki + Promtail (log collection)
#   - Sample workload (Online Boutique)
#
# Cara pakai:
#   chmod +x scripts/deploy_monitoring.sh
#   ./scripts/deploy_monitoring.sh
#
# Prerequisites:
#   - Cluster sudah berjalan (./scripts/setup_cluster.sh)
#   - Helm sudah terinstall
# ============================================================

set -e

# Warna output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HELM_VALUES_DIR="$PROJECT_DIR/infrastructure/helm-values"
WORKLOAD_DIR="$PROJECT_DIR/infrastructure/workloads"

echo -e "${CYAN}"
echo "╔════════════════════════════════════════════════╗"
echo "║      XFSCI Monitoring Stack Deployment         ║"
echo "║      Prometheus + Loki + Online Boutique       ║"
echo "╚════════════════════════════════════════════════╝"
echo -e "${NC}"

# ===== Pre-check =====
echo -e "${BLUE}[Pre-check]${NC} Verifying cluster..."
if ! kubectl get nodes > /dev/null 2>&1; then
    echo -e "${RED}❌ Cluster not reachable! Run setup_cluster.sh first.${NC}"
    exit 1
fi
echo -e "  ✅ Cluster OK"

# Detect Master Node IP
MASTER_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || echo "172.20.0.108")
echo -e "  📍 Detected Cluster IP: ${GREEN}${MASTER_IP}${NC}"
echo ""

# Mode detection
MODE="${1:-all}"
if [ "$MODE" == "--help" ] || [ "$MODE" == "-h" ]; then
    echo "Usage: ./scripts/deploy_monitoring.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  (none)            Run full deployment pipeline with smart auto-detection"
    echo "  --loki-boutique   Deploy only Loki (Log collector) and Online Boutique workload"
    echo "  --verify          Verify current cluster workloads and access points"
    echo "  --force           Force reinstall all Helm charts even if already running"
    exit 0
fi

# ===== Step 1: Add Helm repos =====
if [ "$MODE" != "--verify" ]; then
    echo -e "${BLUE}[Step 1/5]${NC} Adding & updating Helm repositories..."
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
    helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
    helm repo update
    echo -e "  ✅ Helm repos updated"
    echo ""
fi

# ===== Step 2: Deploy Prometheus + Grafana =====
if [ "$MODE" != "--loki-boutique" ] && [ "$MODE" != "--verify" ]; then
    echo -e "${BLUE}[Step 2/5]${NC} Deploying Prometheus + Grafana..."
    
    GRAFANA_READY=$(kubectl get pods -n monitoring -l app.kubernetes.io/name=grafana --no-headers 2>/dev/null | grep -c "3/3" || true)
    PROM_READY=$(kubectl get pods -n monitoring -l app.kubernetes.io/name=prometheus --no-headers 2>/dev/null | grep -c "Running" || true)

    if [ "$GRAFANA_READY" -gt 0 ] && [ "$PROM_READY" -gt 0 ] && [ "$MODE" != "--force" ]; then
        echo -e "  ✅ Prometheus & Grafana are already running and healthy! (Skipping to save time. Use --force to reinstall)"
    else
        echo -e "  Installing/upgrading kube-prometheus-stack..."
        helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
            --namespace monitoring \
            --create-namespace \
            -f "$HELM_VALUES_DIR/prometheus-values.yaml" \
            --timeout 600s
        echo -e "  ✅ Prometheus + Grafana deployed"
    fi
    echo -e "  📊 Prometheus UI: ${GREEN}http://${MASTER_IP}:30090${NC} (or http://localhost:30090)"
    echo -e "  📈 Grafana UI:    ${GREEN}http://${MASTER_IP}:30030${NC} (or http://localhost:30030)"
    echo -e "     Username: admin"
    echo -e "     Password: xfsci-admin-2026"
    echo ""
fi

# ===== Step 3: Deploy Loki + Promtail =====
if [ "$MODE" != "--verify" ]; then
    echo -e "${BLUE}[Step 3/5]${NC} Deploying Loki + Promtail (Log Collector)..."
    
    LOKI_READY=$(kubectl get pods -n monitoring -l app=loki --no-headers 2>/dev/null | grep -c "Running" || true)
    if [ "$LOKI_READY" -gt 0 ] && [ "$MODE" != "--force" ]; then
        echo -e "  ✅ Loki is already running! (Skipping to save time. Use --force to reinstall)"
    else
        helm upgrade --install loki grafana/loki-stack \
            --namespace monitoring \
            -f "$HELM_VALUES_DIR/loki-values.yaml" \
            --timeout 600s
        echo -e "  ✅ Loki + Promtail deployed"
    fi
    echo -e "  📝 Loki API: ${GREEN}http://${MASTER_IP}:30100${NC} (or http://localhost:30100)"
    echo ""
fi

# ===== Step 4: Deploy Sample Workload =====
if [ "$MODE" != "--verify" ]; then
    echo -e "${BLUE}[Step 4/5]${NC} Deploying Online Boutique (sample microservices)..."
    kubectl apply -f "$WORKLOAD_DIR/online-boutique.yaml"

    echo -e "  Waiting for microservices to be ready (pulling images)..."
    DEPLOYMENTS=(
        "frontend"
        "cartservice"
        "redis-cart"
        "productcatalogservice"
        "currencyservice"
        "paymentservice"
        "shippingservice"
        "emailservice"
        "checkoutservice"
        "recommendationservice"
        "adservice"
    )

    for dep in "${DEPLOYMENTS[@]}"; do
        echo -ne "  Waiting for $dep... "
        kubectl rollout status deployment/$dep -n demo --timeout=120s 2>/dev/null && echo "✅" || echo "⚠️ (pulling or starting)"
    done

    echo ""
    echo -e "  ✅ Online Boutique deployed (11 microservices)"
    echo -e "  🌐 Frontend: ${GREEN}http://${MASTER_IP}:30080${NC} (or http://localhost:30080)"
    echo ""
fi

# ===== Step 5: Verification =====
echo -e "${BLUE}[Step 5/5]${NC} Final verification..."

echo ""
echo "  📦 Monitoring namespace pods:"
kubectl get pods -n monitoring --no-headers 2>/dev/null | awk '{printf "    %-52s %s\n", $1, $3}' || true
echo ""

echo "  🛒 Demo namespace pods:"
kubectl get pods -n demo --no-headers 2>/dev/null | awk '{printf "    %-52s %s\n", $1, $3}' || true
echo ""

# Count pods
MON_READY=$(kubectl get pods -n monitoring --no-headers 2>/dev/null | grep -c "Running" || echo "0")
DEMO_READY=$(kubectl get pods -n demo --no-headers 2>/dev/null | grep -c "Running" || echo "0")

echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          ✅ XFSCI MONITORING & WORKLOAD READY!               ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  Monitoring pods running: $MON_READY                            ║"
echo "║  Workload pods running:   $DEMO_READY                            ║"
echo "║                                                              ║"
echo "║  Access Points (Browser):                                    ║"
echo "║  ┌────────────────────────────────────────────────────────┐  ║"
echo "║  │ Prometheus  → http://${MASTER_IP}:30090                │  ║"
echo "║  │ Grafana     → http://${MASTER_IP}:30030                │  ║"
echo "║  │ Loki        → http://${MASTER_IP}:30100                │  ║"
echo "║  │ Frontend    → http://${MASTER_IP}:30080                │  ║"
echo "║  └────────────────────────────────────────────────────────┘  ║"
echo "║  (Credentials: admin / xfsci-admin-2026)                     ║"
echo "║                                                              ║"
echo "║  Next steps:                                                 ║"
echo "║  1. Test Python scrapers:                                    ║"
echo "║     python3 data/collectors/metrics_scraper.py               ║"
echo "║  2. Run fault injection:                                     ║"
echo "║     ./infrastructure/fault-injection/run_faults.sh           ║"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
