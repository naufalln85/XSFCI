#!/bin/bash
# ============================================================
# XFSCI Auto-Train Loop
# ============================================================
# Script ini menjalankan full pipeline secara otomatis dan
# berulang sampai target metrik tercapai:
#   - Cluster Anomaly Accuracy > 99%
#   - Top-3 RCA (A@3)         > 98%
#   - Fault-F1 (Macro)        > 80%
#
# Setiap iterasi:
#   1. Generate synthetic data V3 (balanced per class)
#   2. Feature engineering (feature contract v5)
#   3. Train GNN dengan hyperparameter yang berbeda
#   4. Cek apakah semua target tercapai
#   5. Jika belum → ulangi dengan variasi hyperparameter
#
# Cara pakai:
#   chmod +x scripts/auto_train.sh
#   nohup ./scripts/auto_train.sh > auto_train.log 2>&1 &
#
# Atau langsung di terminal:
#   ./scripts/auto_train.sh
#
# Hasil terbaik disimpan di:
#   models/gnn/weights/gnn_best.pt
#   models/gnn/weights/training_metrics.json
# ============================================================

set -o pipefail

# Warna output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Aktifkan virtual environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# ============================================================
# TARGET METRIK
# ============================================================
TARGET_CLUSTER_ACC=99.0
TARGET_RCA_A3=98.0
TARGET_FAULT_F1=80.0

# ============================================================
# HYPERPARAMETER GRID
# ============================================================
# Setiap iterasi menggunakan kombinasi hyperparameter berbeda
# untuk eksplorasi ruang pencarian yang luas

SAMPLES_LIST=(5000 7000 10000 8000 6000)
EPOCHS_LIST=(80 120 100 150 80)
LR_LIST=(0.002 0.001 0.003 0.0015 0.0025)
BATCH_LIST=(32 16 64 24 48)
GAMMA_LIST=(1.5 2.0 1.0 2.5 1.5)
SMOOTHING_LIST=(0.05 0.03 0.08 0.02 0.10)
GRAPH_WEIGHT_LIST=(0.3 0.4 0.2 0.5 0.35)
NOISE_LIST=(0.10 0.08 0.15 0.12 0.05)

MAX_ITERATIONS=${#SAMPLES_LIST[@]}
METRICS_FILE="$PROJECT_DIR/models/gnn/weights/training_metrics.json"
BEST_LOG="$PROJECT_DIR/models/gnn/weights/auto_train_best.log"

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║        XFSCI Auto-Train Loop                                ║"
echo "║        Target: Cluster>99% | RCA>98% | Fault-F1>80%         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo ""
echo -e "  ${BLUE}Max iterations:${NC} $MAX_ITERATIONS"
echo -e "  ${BLUE}Project dir:${NC}    $PROJECT_DIR"
echo -e "  ${BLUE}Python:${NC}         $(python3 --version)"
echo ""

# ============================================================
# FUNGSI: Ekstrak metrik dari training_metrics.json
# ============================================================
extract_metric() {
    local key="$1"
    python3 -c "
import json, sys
try:
    m = json.load(open('$METRICS_FILE'))
    val = m.get('$key', 0)
    print(f'{val * 100:.2f}')
except:
    print('0.00')
"
}

check_targets() {
    local cluster_acc=$(extract_metric "cluster_anomaly_accuracy")
    local rca_a3=$(extract_metric "top3_rca_accuracy")
    local fault_f1=$(extract_metric "fault_macro_f1")

    echo -e "  ${CYAN}Cluster Acc:${NC} ${cluster_acc}% (target: >${TARGET_CLUSTER_ACC}%)"
    echo -e "  ${CYAN}RCA A@3:${NC}     ${rca_a3}% (target: >${TARGET_RCA_A3}%)"
    echo -e "  ${CYAN}Fault-F1:${NC}    ${fault_f1}% (target: >${TARGET_FAULT_F1}%)"

    # Cek apakah SEMUA target tercapai
    python3 -c "
cluster = $cluster_acc
rca = $rca_a3
f1 = $fault_f1
if cluster >= $TARGET_CLUSTER_ACC and rca >= $TARGET_RCA_A3 and f1 >= $TARGET_FAULT_F1:
    exit(0)
else:
    exit(1)
"
}

# ============================================================
# TRACKING BEST OVERALL
# ============================================================
GLOBAL_BEST_SCORE=0.0
GLOBAL_BEST_ITER=0

track_best() {
    local iter="$1"
    local cluster_acc=$(extract_metric "cluster_anomaly_accuracy")
    local rca_a3=$(extract_metric "top3_rca_accuracy")
    local fault_f1=$(extract_metric "fault_macro_f1")

    local composite=$(python3 -c "print(f'{($cluster_acc * 0.20 + $rca_a3 * 0.30 + $fault_f1 * 0.50):.2f}')")

    local is_new_best=$(python3 -c "print('yes' if $composite > $GLOBAL_BEST_SCORE else 'no')")

    if [ "$is_new_best" == "yes" ]; then
        GLOBAL_BEST_SCORE=$composite
        GLOBAL_BEST_ITER=$iter
        # Backup model terbaik
        cp "$PROJECT_DIR/models/gnn/weights/gnn_best.pt" \
           "$PROJECT_DIR/models/gnn/weights/gnn_global_best.pt" 2>/dev/null || true
        cp "$METRICS_FILE" \
           "$PROJECT_DIR/models/gnn/weights/training_metrics_best.json" 2>/dev/null || true

        echo -e "  ${GREEN}⭐ NEW GLOBAL BEST! Score: ${composite}% (Iter $iter)${NC}"
        echo "[$(date)] Iter $iter | Cluster: $cluster_acc% | RCA: $rca_a3% | F1: $fault_f1% | Composite: $composite%" >> "$BEST_LOG"
    fi
}

# ============================================================
# MAIN LOOP
# ============================================================
START_TIME=$(date +%s)

for i in $(seq 0 $((MAX_ITERATIONS - 1))); do
    ITER=$((i + 1))
    SAMPLES=${SAMPLES_LIST[$i]}
    EPOCHS=${EPOCHS_LIST[$i]}
    LR=${LR_LIST[$i]}
    BATCH=${BATCH_LIST[$i]}
    GAMMA=${GAMMA_LIST[$i]}
    SMOOTHING=${SMOOTHING_LIST[$i]}
    GRAPH_W=${GRAPH_WEIGHT_LIST[$i]}
    NOISE=${NOISE_LIST[$i]}

    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║  ITERASI ${ITER}/${MAX_ITERATIONS}                                              ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo -e "  ${YELLOW}Hyperparameters:${NC}"
    echo -e "    Samples/class : $SAMPLES"
    echo -e "    Epochs        : $EPOCHS"
    echo -e "    LR            : $LR"
    echo -e "    Batch size    : $BATCH"
    echo -e "    Focal gamma   : $GAMMA"
    echo -e "    Label smooth  : $SMOOTHING"
    echo -e "    Graph weight  : $GRAPH_W"
    echo -e "    Noise factor  : $NOISE"
    echo ""

    # ── Step 1: Generate Synthetic Data V3 ──
    echo -e "  ${CYAN}[Step 1/3]${NC} Generating synthetic data (${SAMPLES}/class, noise=${NOISE})..."
    python3 data/preprocessors/synthetic_generator.py \
        --target-per-class "$SAMPLES" \
        --noise "$NOISE"

    if [ $? -ne 0 ]; then
        echo -e "  ${RED}❌ Synthetic generation gagal. Skip iterasi ini.${NC}"
        continue
    fi
    echo -e "  ${GREEN}✅ Synthetic data generated${NC}"

    # ── Step 2: Feature Engineering ──
    echo -e "  ${CYAN}[Step 2/3]${NC} Running feature engineering (current feature contract)..."
    python3 data/preprocessors/feature_engineer.py

    if [ $? -ne 0 ]; then
        echo -e "  ${RED}❌ Feature engineering gagal. Skip iterasi ini.${NC}"
        continue
    fi
    echo -e "  ${GREEN}✅ Features engineered${NC}"

    # ── Step 3: Train GNN ──
    echo -e "  ${CYAN}[Step 3/3]${NC} Training GNN (epochs=${EPOCHS}, lr=${LR}, batch=${BATCH})..."
    python3 models/gnn/train_gnn.py \
        --epochs "$EPOCHS" \
        --batch-size "$BATCH" \
        --lr "$LR" \
        --gamma "$GAMMA" \
        --label-smoothing "$SMOOTHING" \
        --graph-loss-weight "$GRAPH_W"

    if [ $? -ne 0 ]; then
        echo -e "  ${RED}❌ Training gagal. Skip iterasi ini.${NC}"
        continue
    fi
    echo -e "  ${GREEN}✅ Training complete${NC}"

    # ── Check Results ──
    echo ""
    echo -e "  ${YELLOW}📊 Hasil Iterasi ${ITER}:${NC}"
    track_best "$ITER"

    if check_targets; then
        ELAPSED=$(( $(date +%s) - START_TIME ))
        MINUTES=$((ELAPSED / 60))

        echo ""
        echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║  🎉 SEMUA TARGET TERCAPAI! (Iterasi ${ITER}, ${MINUTES} menit)          ║${NC}"
        echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${GREEN}║  Cluster Anomaly Acc : $(extract_metric 'cluster_anomaly_accuracy')%                          ║${NC}"
        echo -e "${GREEN}║  Top-3 RCA (A@3)     : $(extract_metric 'top3_rca_accuracy')%                          ║${NC}"
        echo -e "${GREEN}║  Fault-F1            : $(extract_metric 'fault_macro_f1')%                          ║${NC}"
        echo -e "${GREEN}║                                                              ║${NC}"
        echo -e "${GREEN}║  Model: models/gnn/weights/gnn_best.pt                       ║${NC}"
        echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
        exit 0
    fi

    echo -e "  ${YELLOW}→ Target belum tercapai. Lanjut ke iterasi berikutnya...${NC}"
done

# Jika semua iterasi selesai tanpa mencapai target
ELAPSED=$(( $(date +%s) - START_TIME ))
MINUTES=$((ELAPSED / 60))

echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║  Semua ${MAX_ITERATIONS} iterasi selesai (${MINUTES} menit)                       ║${NC}"
echo -e "${YELLOW}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${YELLOW}║  Best iteration: ${GLOBAL_BEST_ITER} (Score: ${GLOBAL_BEST_SCORE}%)                  ║${NC}"
echo -e "${YELLOW}║  Model terbaik: models/gnn/weights/gnn_global_best.pt      ║${NC}"
echo -e "${YELLOW}║  Metrik terbaik: models/gnn/weights/training_metrics_best   ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

echo ""
echo -e "  ${CYAN}Hasil akhir terbaik:${NC}"
if [ -f "$PROJECT_DIR/models/gnn/weights/training_metrics_best.json" ]; then
    python3 -c "
import json
m = json.load(open('$PROJECT_DIR/models/gnn/weights/training_metrics_best.json'))
print(f\"  Cluster Acc : {m.get('cluster_anomaly_accuracy', 0)*100:.2f}%\")
print(f\"  RCA A@3     : {m.get('top3_rca_accuracy', 0)*100:.2f}%\")
print(f\"  Fault-F1    : {m.get('fault_macro_f1', 0)*100:.2f}%\")
print(f\"  Node Acc    : {m.get('node_accuracy', 0)*100:.2f}%\")
"
fi
