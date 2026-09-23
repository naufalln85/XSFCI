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
echo ""

# ===== Step 1: Add Helm repos =====
echo -e "${BLUE}[Step 1/5]${NC} Adding Helm repositories..."

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo update

echo -e "  ✅ Helm repos updated"
echo ""

# ===== Step 2: Deploy Prometheus + Grafana =====
echo -e "${BLUE}[Step 2/5]${NC} Deploying Prometheus + Grafana..."
echo -e "  This may take 2-3 minutes..."

helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
    --namespace monitoring \
    --create-namespace \
    -f "$HELM_VALUES_DIR/prometheus-values.yaml" \
    --wait \
    --timeout 300s

echo ""
echo -e "  ✅ Prometheus + Grafana deployed"
echo -e "  📊 Prometheus UI: ${GREEN}http://localhost:9090${NC}"
echo -e "  📈 Grafana UI:    ${GREEN}http://localhost:3000${NC}"
echo -e "     Username: admin"
echo -e "     Password: xfsci-admin-2026"
echo ""

# ===== Step 3: Deploy Loki + Promtail =====
echo -e "${BLUE}[Step 3/5]${NC} Deploying Loki + Promtail..."

helm upgrade --install loki grafana/loki-stack \
    --namespace monitoring \
    -f "$HELM_VALUES_DIR/loki-values.yaml" \
    --wait \
    --timeout 180s

echo ""
echo -e "  ✅ Loki + Promtail deployed"
echo -e "  📝 Loki API: ${GREEN}http://localhost:3100${NC}"
echo ""

# ===== Step 4: Deploy Sample Workload =====
echo -e "${BLUE}[Step 4/5]${NC} Deploying Online Boutique (sample microservices)..."

kubectl apply -f "$WORKLOAD_DIR/online-boutique.yaml"

echo -e "  Waiting for all pods to be ready..."
echo -e "  (This may take 3-5 minutes for image pulls)"

# Tunggu deployment ready
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
    kubectl rollout status deployment/$dep -n demo --timeout=180s 2>/dev/null && echo "✅" || echo "⚠️ (may still be pulling image)"
done

echo ""
echo -e "  ✅ Online Boutique deployed (11 microservices)"
echo -e "  🌐 Frontend: ${GREEN}http://localhost:30080${NC}"
echo ""

# ===== Step 5: Verification =====
echo -e "${BLUE}[Step 5/5]${NC} Final verification..."

echo ""
echo "  📦 Monitoring namespace pods:"
kubectl get pods -n monitoring --no-headers | awk '{printf "    %-50s %s\n", $1, $3}'
echo ""

echo "  🛒 Demo namespace pods:"
kubectl get pods -n demo --no-headers | awk '{printf "    %-50s %s\n", $1, $3}'
echo ""

# Count pods
MON_READY=$(kubectl get pods -n monitoring --no-headers 2>/dev/null | grep -c "Running" || echo "0")
DEMO_READY=$(kubectl get pods -n demo --no-headers 2>/dev/null | grep -c "Running" || echo "0")

echo -e "${GREEN}"
echo "╔════════════════════════════════════════════════╗"
echo "║  ✅ MONITORING STACK DEPLOYMENT COMPLETE!       ║"
echo "╠════════════════════════════════════════════════╣"
echo "║                                                ║"
echo "║  Monitoring pods running: $MON_READY              ║"
echo "║  Workload pods running:   $DEMO_READY              ║"
echo "║                                                ║"
echo "║  Access Points:                                ║"
echo "║  ┌─────────────────────────────────────────┐   ║"
echo "║  │ Prometheus  → http://localhost:9090     │   ║"
echo "║  │ Grafana     → http://localhost:3000     │   ║"
echo "║  │ Loki        → http://localhost:3100     │   ║"
echo "║  │ Frontend    → http://localhost:30080    │   ║"
echo "║  └─────────────────────────────────────────┘   ║"
echo "║                                                ║"
echo "║  Next steps:                                   ║"
echo "║  1. Activate Python venv:                      ║"
echo "║     source venv/bin/activate                   ║"
echo "║  2. Start data collection:                     ║"
echo "║     python data/collectors/metrics_scraper.py  ║"
echo "║  3. Run fault injection:                       ║"
echo "║     ./infrastructure/fault-injection/          ║"
echo "║     run_faults.sh                              ║"
echo "║                                                ║"
echo "╚════════════════════════════════════════════════╝"
echo -e "${NC}"
