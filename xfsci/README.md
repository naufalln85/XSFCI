# XFSCI: Explainable Federated Self-Healing Cloud Infrastructure

Framework AI otonom, kolaboratif, aman, dan transparan untuk infrastruktur cloud modern berbasis Kubernetes.

## 🏗️ Arsitektur 8 Layer

| Layer | Nama | Teknologi |
|-------|------|-----------|
| 1 | Monitoring Layer | Prometheus, OpenTelemetry, Loki |
| 2 | Cloud Intelligence Layer | Graph Neural Network (GNN) |
| 3 | Failure Prediction Layer | LSTM + Time-Series Transformer |
| 4 | RL Decision Agent | PPO / DQN (Gymnasium) |
| 5 | Self-Healing Engine | Kubernetes API + Safety Guardrails |
| 6 | Explainability Layer | SHAP + LIME |
| 7 | Federated Learning Layer | Flower Framework |
| 8 | Security Layer | FedMedian / Krum (Byzantine Defense) |

## 📋 Prerequisites

### Software yang Harus Diinstall di Server/VM Target

```bash
# 1. Docker (wajib untuk kind)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 2. kubectl (Kubernetes CLI)
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# 3. kind (Kubernetes in Docker)
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64
chmod +x ./kind
sudo mv ./kind /usr/local/bin/kind

# 4. Helm (Kubernetes package manager)
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# 5. Python 3.10+
sudo apt update && sudo apt install -y python3.10 python3.10-venv python3-pip

# 6. Node.js 18+ (untuk dashboard nanti)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs
```

### Hardware Minimum
| Komponen | Minimum |
|----------|---------|
| CPU | 8 cores (12 recommended) |
| RAM | 16 GB (32 GB recommended) |
| Storage | 60 GB free |
| OS | Ubuntu 22.04 LTS / Debian 12 |

## 🚀 Quick Start

```bash
# 1. Clone/transfer project ke server
cd /path/to/xfsci

# 2. Setup Python virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Buat Kubernetes cluster
chmod +x scripts/setup_cluster.sh
./scripts/setup_cluster.sh

# 4. Deploy monitoring stack
chmod +x scripts/deploy_monitoring.sh
./scripts/deploy_monitoring.sh

# 5. Verify semuanya berjalan
kubectl get pods --all-namespaces
```

## 📁 Struktur Direktori

```
xfsci/
├── infrastructure/          # Kubernetes & monitoring configs
├── data/                    # Data collection & preprocessing
│   ├── collectors/          # Prometheus/Loki scrapers
│   ├── preprocessors/       # Cleaning, labeling, augmentation
│   ├── raw/                 # Raw CSV data
│   ├── processed/           # Cleaned datasets
│   └── synthetic/           # TimeGAN augmented data
├── models/                  # AI models (GNN, Transformer, RL, XAI)
│   ├── gnn/                 # Graph Neural Network
│   ├── prediction/          # LSTM & Transformer
│   ├── rl_agent/            # DQN & PPO agents
│   └── explainer/           # SHAP & LIME
├── healing/                 # Self-Healing Engine + Guardrails
├── federated/               # Flower FL + Byzantine defense
├── backend/                 # FastAPI REST API
├── dashboard/               # Next.js frontend
├── scripts/                 # Automation scripts
├── configs/                 # YAML configurations
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

## 📖 Dokumentasi Riset
- [Panduan XFSCI Lengkap](../xfsci_hackmd.md)
- [Materi PPT](../materi_ppt_xfsci_lengkap.md)
- [Eksplorasi Cloud Computing](../cloud_computing_exploration_hackmd.md)
