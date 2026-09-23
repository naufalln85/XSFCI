#!/bin/bash
# ============================================================
# XFSCI One-Click Cluster Setup
# ============================================================
# Script ini membuat cluster Kubernetes menggunakan kind
# dan memverifikasi semua node berjalan dengan benar.
#
# Cara pakai:
#   chmod +x scripts/setup_cluster.sh
#   ./scripts/setup_cluster.sh
#
# Prerequisites:
#   - Docker sudah terinstall dan berjalan
#   - kind sudah terinstall
#   - kubectl sudah terinstall
# ============================================================

set -e

# Warna output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

CLUSTER_NAME="xfsci-cluster"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
KIND_CONFIG="$PROJECT_DIR/infrastructure/kind-config.yaml"

echo -e "${CYAN}"
echo "╔════════════════════════════════════════════════╗"
echo "║      XFSCI Kubernetes Cluster Setup            ║"
echo "║      Explainable Federated Self-Healing        ║"
echo "║      Cloud Infrastructure                      ║"
echo "╚════════════════════════════════════════════════╝"
echo -e "${NC}"

# ===== Step 1: Pre-checks =====
echo -e "${BLUE}[Step 1/6]${NC} Checking prerequisites..."

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker not found! Install Docker first.${NC}"
    echo "   → https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}❌ Docker daemon not running! Start Docker first.${NC}"
    exit 1
fi
echo -e "  ✅ Docker: $(docker --version)"

# Check kind
if ! command -v kind &> /dev/null; then
    echo -e "${RED}❌ kind not found! Install kind first.${NC}"
    echo "   → https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
    exit 1
fi
echo -e "  ✅ kind: $(kind version)"

# Check kubectl
if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}❌ kubectl not found! Install kubectl first.${NC}"
    echo "   → https://kubernetes.io/docs/tasks/tools/"
    exit 1
fi
echo -e "  ✅ kubectl: $(kubectl version --client --short 2>/dev/null || kubectl version --client)"

# Check Helm
if ! command -v helm &> /dev/null; then
    echo -e "${YELLOW}⚠️  Helm not found. Installing...${NC}"
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
echo -e "  ✅ Helm: $(helm version --short)"

echo ""

# ===== Step 2: Clean up existing cluster =====
echo -e "${BLUE}[Step 2/6]${NC} Checking for existing cluster..."

if kind get clusters 2>/dev/null | grep -q "$CLUSTER_NAME"; then
    echo -e "  ${YELLOW}⚠️  Cluster '$CLUSTER_NAME' already exists.${NC}"
    read -p "  Delete and recreate? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "  Deleting existing cluster..."
        kind delete cluster --name "$CLUSTER_NAME"
        echo -e "  ✅ Old cluster deleted"
    else
        echo -e "  Using existing cluster."
        kubectl cluster-info --context "kind-$CLUSTER_NAME"
        exit 0
    fi
else
    echo -e "  ✅ No existing cluster found"
fi

echo ""

# ===== Step 3: Create cluster =====
echo -e "${BLUE}[Step 3/6]${NC} Creating Kubernetes cluster..."
echo -e "  Config: $KIND_CONFIG"
echo -e "  Nodes: 1 control-plane + 3 workers (Jakarta, Bandung, Surabaya)"
echo ""

# Buat temporary directories untuk mount
mkdir -p /tmp/xfsci-worker-a /tmp/xfsci-worker-b /tmp/xfsci-worker-c

kind create cluster \
    --name "$CLUSTER_NAME" \
    --config "$KIND_CONFIG" \
    --wait 120s

echo ""
echo -e "  ✅ Cluster created successfully!"
echo ""

# ===== Step 4: Verify nodes =====
echo -e "${BLUE}[Step 4/6]${NC} Verifying cluster nodes..."

kubectl get nodes -o wide
echo ""

# Tunggu semua node Ready
echo -e "  Waiting for all nodes to be Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s
echo -e "  ✅ All nodes are Ready"
echo ""

# ===== Step 5: Create namespaces =====
echo -e "${BLUE}[Step 5/6]${NC} Creating namespaces..."

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace demo --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace xfsci-system --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace chaos-testing --dry-run=client -o yaml | kubectl apply -f -

echo -e "  ✅ Namespaces created:"
kubectl get namespaces | grep -E "monitoring|demo|xfsci-system|chaos-testing"
echo ""

# ===== Step 6: Cluster info =====
echo -e "${BLUE}[Step 6/6]${NC} Cluster information..."

echo ""
kubectl cluster-info --context "kind-$CLUSTER_NAME"
echo ""

echo -e "${GREEN}"
echo "╔════════════════════════════════════════════════╗"
echo "║  ✅ CLUSTER SETUP COMPLETE!                    ║"
echo "╠════════════════════════════════════════════════╣"
echo "║                                                ║"
echo "║  Cluster: $CLUSTER_NAME                  ║"
echo "║  Nodes:   4 (1 CP + 3 Workers)                ║"
echo "║                                                ║"
echo "║  Worker Labels:                                ║"
echo "║    - xfsci-site=jakarta  (Worker A)            ║"
echo "║    - xfsci-site=bandung  (Worker B)            ║"
echo "║    - xfsci-site=surabaya (Worker C)            ║"
echo "║                                                ║"
echo "║  Next step:                                    ║"
echo "║    ./scripts/deploy_monitoring.sh              ║"
echo "║                                                ║"
echo "╚════════════════════════════════════════════════╝"
echo -e "${NC}"
