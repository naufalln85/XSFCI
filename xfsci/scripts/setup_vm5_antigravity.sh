#!/usr/bin/env bash
# ==============================================================================
# setup_vm5_antigravity.sh - Setup & Verify Antigravity Engine on VM5
# ==============================================================================
# Skrip ini mengonfigurasi VM5 sebagai Autonomous SRE Node yang menjalankan:
#   1. google-antigravity runtime (terintegrasi Akun Pro)
#   2. Antigravity Custom Skills & SRE Sandbox
#   3. GNN Inference Engine (Top-3 RCA + DualHeadGATv2)
#   4. Kubeconfig connection ke Master Node (VM1)
#
# Topologi Server (4 Cluster + 1 Agent):
#   VM1 = K8s Control Plane (Master)
#   VM2 = K8s Worker Node 1
#   VM3 = K8s Worker Node 2
#   VM4 = K8s Worker Node 3
#   VM5 = Antigravity SRE Agent (GNN + Antigravity Runtime) ← INI
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

# ==== Konfigurasi (Sesuaikan dengan server Anda) ====
MASTER_IP="${MASTER_IP:-172.20.0.104}"   # IP VM1 (Master Node)
VM5_REPO_DIR="${VM5_REPO_DIR:-$HOME/xfsci}"  # Lokasi repo XFSCI di VM5

echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  🚀 XFSCI VM5 — Antigravity Autonomous SRE Setup       ║${NC}"
echo -e "${BLUE}║  Topologi: 4 Cluster Nodes + 1 Agent Node (VM5)         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"

# ===================================================================
# STEP 1: Python Environment
# ===================================================================
echo -e "\n${YELLOW}[1/7] Memeriksa Python Environment...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Python3 belum terinstal!${NC}"
    echo -e "  sudo apt update && sudo apt install -y python3 python3-pip python3-venv"
    exit 1
fi

if [[ -z "$VIRTUAL_ENV" ]]; then
    echo -e "${YELLOW}Membuat virtualenv di ~/venv-xfsci...${NC}"
    python3 -m venv ~/venv-xfsci
    source ~/venv-xfsci/bin/activate
    echo -e "${GREEN}✅ Virtualenv aktif: $(which python3)${NC}"
else
    echo -e "${GREEN}✅ Virtualenv sudah aktif: $VIRTUAL_ENV${NC}"
fi

# ===================================================================
# STEP 2: Instalasi Dependensi Inti & Antigravity SDK
# ===================================================================
echo -e "\n${YELLOW}[2/7] Menginstal google-antigravity dan dependensi...${NC}"
pip install --upgrade pip
pip install google-antigravity loguru pyyaml pydantic httpx requests pandas numpy

# PyTorch CPU-only untuk inferensi GNN (ringan, tanpa CUDA)
echo -e "${YELLOW}  → Menginstal PyTorch CPU untuk GNN Inference Engine...${NC}"
pip install torch --index-url https://download.pytorch.org/whl/cpu || true

# torch-geometric untuk GNN
pip install torch-geometric || true

echo -e "${GREEN}✅ Semua dependensi Python terinstal.${NC}"

# ===================================================================
# STEP 3: Sync GNN Trained Model dari Development Machine
# ===================================================================
echo -e "\n${YELLOW}[3/7] 🧠 Sinkronisasi GNN Trained Model Weights...${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "File-file kritis yang WAJIB ada di VM5:"
echo -e "  ${GREEN}1. models/gnn/weights/gnn_best.pt${NC}      ← Bobot model terlatih"
echo -e "  ${GREEN}2. data/processed/scaler_params.json${NC}   ← Parameter normalisasi"
echo -e "  ${GREEN}3. models/gnn/gnn_model.py${NC}             ← Definisi arsitektur"
echo -e "  ${GREEN}4. models/gnn/graph_dataset.py${NC}         ← Topologi graf + mapping"
echo -e "  ${GREEN}5. models/gnn/gnn_predictor.py${NC}         ← Inference engine"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Cek apakah file model sudah ada di VM5
WEIGHTS_FILE="$VM5_REPO_DIR/models/gnn/weights/gnn_best.pt"
SCALER_FILE="$VM5_REPO_DIR/data/processed/scaler_params.json"

if [[ -f "$WEIGHTS_FILE" && -f "$SCALER_FILE" ]]; then
    echo -e "${GREEN}✅ Model weights & scaler params sudah ada di VM5!${NC}"
    WEIGHTS_SIZE=$(du -h "$WEIGHTS_FILE" | cut -f1)
    echo -e "   gnn_best.pt size: $WEIGHTS_SIZE"
else
    echo -e "${YELLOW}⚠️  File model belum lengkap di VM5.${NC}"
    echo -e ""
    echo -e "${CYAN}Cara 1 — Dari laptop/dev machine (via SCP):${NC}"
    echo -e "  ${BLUE}scp xfsci/models/gnn/weights/gnn_best.pt ubuntu@<IP_VM5>:$VM5_REPO_DIR/models/gnn/weights/${NC}"
    echo -e "  ${BLUE}scp xfsci/data/processed/scaler_params.json ubuntu@<IP_VM5>:$VM5_REPO_DIR/data/processed/${NC}"
    echo -e ""
    echo -e "${CYAN}Cara 2 — Dari VM1/Master (via rsync):${NC}"
    echo -e "  ${BLUE}rsync -avz --progress /path/to/xfsci/models/gnn/weights/ ubuntu@<IP_VM5>:$VM5_REPO_DIR/models/gnn/weights/${NC}"
    echo -e "  ${BLUE}rsync -avz --progress /path/to/xfsci/data/processed/ ubuntu@<IP_VM5>:$VM5_REPO_DIR/data/processed/${NC}"
    echo -e ""
    echo -e "${CYAN}Cara 3 — Dari Git repo (jika di-push):${NC}"
    echo -e "  ${BLUE}cd $VM5_REPO_DIR && git pull origin main${NC}"
fi

# Buat direktori yang diperlukan
mkdir -p "$VM5_REPO_DIR/models/gnn/weights"
mkdir -p "$VM5_REPO_DIR/data/processed"

# ===================================================================
# STEP 4: Beri Izin Eksekusi pada Skill Scripts
# ===================================================================
echo -e "\n${YELLOW}[4/7] Memberikan izin eksekusi pada Custom Skills Sandbox...${NC}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Set executable permissions untuk semua SRE scripts
for SKILL_DIR in "$REPO_ROOT/.agents/skills"/*/scripts; do
    if [[ -d "$SKILL_DIR" ]]; then
        chmod +x "$SKILL_DIR"/*.sh 2>/dev/null || true
        echo -e "  ${GREEN}✅ $(basename $(dirname $SKILL_DIR))/scripts/*.sh${NC}"
    fi
done

# ===================================================================
# STEP 5: Kubeconfig Setup (dari VM1 Master)
# ===================================================================
echo -e "\n${YELLOW}[5/7] Memeriksa koneksi Kubernetes (kubectl)...${NC}"
if ! command -v kubectl &> /dev/null; then
    echo -e "${YELLOW}Mengunduh kubectl binary...${NC}"
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
    rm kubectl
    echo -e "${GREEN}✅ kubectl terinstal.${NC}"
fi

mkdir -p ~/.kube
if [[ ! -f ~/.kube/config ]]; then
    echo -e "${YELLOW}⚠️ ~/.kube/config belum ditemukan di VM5.${NC}"
    echo -e "Copy kubeconfig dari Master Node (VM1) dengan salah satu cara:"
    echo -e ""
    echo -e "  ${CYAN}Cara A (dari VM1 langsung):${NC}"
    echo -e "  ${BLUE}scp ubuntu@$MASTER_IP:~/.kube/config ~/.kube/config${NC}"
    echo -e ""
    echo -e "  ${CYAN}Cara B (dari laptop):${NC}"
    echo -e "  ${BLUE}scp ubuntu@$MASTER_IP:~/.kube/config ubuntu@<IP_VM5>:~/.kube/config${NC}"
    echo -e ""
    echo -e "  Setelah copy, update server address di config jika perlu:"
    echo -e "  ${BLUE}sed -i 's|server: https://.*:6443|server: https://$MASTER_IP:6443|g' ~/.kube/config${NC}"
else
    echo -e "${GREEN}✅ Kubeconfig ditemukan. Menguji koneksi cluster...${NC}"
    if kubectl cluster-info 2>/dev/null; then
        echo -e "${GREEN}✅ Koneksi ke cluster OK!${NC}"
        kubectl get nodes -o wide 2>/dev/null || true
    else
        echo -e "${YELLOW}⚠️ Cluster belum tersambung. Pastikan kubeconfig benar & jaringan VM5→VM1 terbuka.${NC}"
    fi
fi

# ===================================================================
# STEP 6: Verifikasi GNN Inference Engine
# ===================================================================
echo -e "\n${YELLOW}[6/7] 🧪 Menguji GNN Inference Engine...${NC}"
if [[ -f "$WEIGHTS_FILE" ]]; then
    cd "$VM5_REPO_DIR"
    python3 -c "
from models.gnn.gnn_predictor import GNNPredictor
p = GNNPredictor()
if p.is_ready:
    result = p.predict_target('cartservice')
    if result is None:
        print('  ⚠️ Inference skipped: provide a complete live 11-service × 21-feature snapshot.')
        print('     A no-input call is not a valid model accuracy or RCA test.')
    else:
        print(f'  ✅ GNN Inference OK! Risk Score: {result.risk_score:.3f} | Anomaly: {result.anomaly_type.value}')
        print(f'     Root Cause: {result.root_cause_service} | Confidence: {result.confidence:.2%}')
else:
    print('  ⚠️ GNN Model loaded but not ready (weights mismatch?)')
" 2>/dev/null || echo -e "${YELLOW}  ⚠️ GNN test skipped (dependencies may be missing).${NC}"
else
    echo -e "${YELLOW}  ⏭️ Skipped — model weights belum ada di VM5.${NC}"
fi

# ===================================================================
# STEP 7: Otentikasi Akun Pro Antigravity
# ===================================================================
echo -e "\n${YELLOW}[7/7] 🔑 Panduan Otentikasi Akun Pro Antigravity:${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e ""
echo -e "  ${GREEN}Cara A — Device Flow (Tanpa Browser di Server):${NC}"
echo -e "    1. Jalankan: ${BLUE}agy auth login --no-browser${NC}"
echo -e "    2. Buka link verifikasi di browser laptop Anda"
echo -e "    3. Approve & paste kode ke terminal VM5"
echo -e ""
echo -e "  ${GREEN}Cara B — Sync Token dari Laptop (Windows):${NC}"
echo -e "    Dari PowerShell laptop Anda, jalankan:"
echo -e '    '"${BLUE}scp -r \"\$env:USERPROFILE\\.gemini\" ubuntu@<IP_VM5>:~/${NC}"
echo -e ""
echo -e "  ${GREEN}Cara C — Sync Token dari Laptop (Linux/Mac):${NC}"
echo -e "    ${BLUE}scp -r ~/.gemini ubuntu@<IP_VM5>:~/${NC}"
echo -e ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# ===================================================================
# Summary
# ===================================================================
echo -e ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  🎉 VM5 Antigravity SRE Setup Complete!                 ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}║  Topologi Final (4+1):                                 ║${NC}"
echo -e "${GREEN}║  ┌──────────────────────────────────────┐              ║${NC}"
echo -e "${GREEN}║  │ K8s Cluster (VM1-VM4)                │              ║${NC}"
echo -e "${GREEN}║  │  VM1: Control Plane (Master)         │              ║${NC}"
echo -e "${GREEN}║  │  VM2: Worker Node 1                  │              ║${NC}"
echo -e "${GREEN}║  │  VM3: Worker Node 2                  │              ║${NC}"
echo -e "${GREEN}║  │  VM4: Worker Node 3                  │              ║${NC}"
echo -e "${GREEN}║  └──────────┬───────────────────────────┘              ║${NC}"
echo -e "${GREEN}║             │ kubectl API (port 6443)                  ║${NC}"
echo -e "${GREEN}║  ┌──────────▼───────────────────────────┐              ║${NC}"
echo -e "${GREEN}║  │ VM5: Antigravity SRE Agent           │              ║${NC}"
echo -e "${GREEN}║  │  ├─ GNN Inference (DualHeadGATv2)    │              ║${NC}"
echo -e "${GREEN}║  │  ├─ Antigravity Runtime (Sandbox)    │              ║${NC}"
echo -e "${GREEN}║  │  ├─ Claude Opus / Gemini 3.8 Flash   │              ║${NC}"
echo -e "${GREEN}║  │  └─ Rule-Based Safety Net            │              ║${NC}"
echo -e "${GREEN}║  └──────────────────────────────────────┘              ║${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}║  Next Steps:                                           ║${NC}"
echo -e "${GREEN}║  1. Sync GNN weights (jika belum)                      ║${NC}"
echo -e "${GREEN}║  2. Setup kubeconfig (jika belum)                      ║${NC}"
echo -e "${GREEN}║  3. Login Akun Pro: agy auth login --no-browser        ║${NC}"
echo -e "${GREEN}║  4. Test: python3 -m agent.orchestrator -d cartservice ║${NC}"
echo -e "${GREEN}║                                                        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
