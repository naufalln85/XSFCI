"""
============================================================
XFSCI Graph Dataset Builder - Layer 2: Cloud Intelligence
============================================================
Mengonversi dataset tabular time-series (dataset_ready.csv)
dan topologi microservice (topology_*.json) menjadi sequence
graf teratribusi PyTorch Geometric (torch_geometric.data.Data).

Fitur Graf:
  - Nodes (11 pods): Online Boutique microservices
  - Node Features (18): Metrik ternormalisasi [0, 1]
  - Edges:
      1. Network Service Dependencies (Bidirectional)
      2. Host Co-location Edges (Node Worker 1, 2, 3)
      3. Self-loops
  - Node Labels (5 classes): NORMAL, CPU, MEMORY, CRASH, NET
  - Graph-Level Label: 0.0 (Healthy) vs 1.0 (Anomalous Cluster)

Split Data:
  - Kronologis temporal (70% Train, 15% Val, 15% Test)
    untuk mencegah data leakage time-series.
============================================================
"""

import os
import sys
import glob
import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd
import torch
from loguru import logger

try:
    from torch_geometric.data import Data, Dataset
    from torch_geometric.loader import DataLoader
    PYG_AVAILABLE = True
except ImportError:
    PYG_AVAILABLE = False
    logger.warning("torch_geometric belum terinstall. Menggunakan custom Data wrapper.")
    
    class Data:
        """Lightweight fallback jika torch_geometric belum terinstall."""
        def __init__(self, x=None, edge_index=None, y=None, y_graph=None, **kwargs):
            self.x = x
            self.edge_index = edge_index
            self.y = y
            self.y_graph = y_graph
            for k, v in kwargs.items():
                setattr(self, k, v)
        
        def to(self, device):
            for k, v in self.__dict__.items():
                if isinstance(v, torch.Tensor):
                    setattr(self, k, v.to(device))
            return self

    class Dataset(torch.utils.data.Dataset):
        pass
    
    class BatchGraph:
        """Container untuk batch graf gabungan (block-diagonal)."""
        def __init__(self, x, edge_index, y, y_graph, batch):
            self.x = x
            self.edge_index = edge_index
            self.y = y
            self.y_graph = y_graph
            self.batch = batch
        def to(self, device):
            self.x = self.x.to(device)
            self.edge_index = self.edge_index.to(device)
            self.y = self.y.to(device)
            self.y_graph = self.y_graph.to(device)
            self.batch = self.batch.to(device)
            return self

    def fallback_collate_fn(data_list):
        num_nodes_per_graph = data_list[0].x.size(0)
        x = torch.cat([d.x for d in data_list], dim=0)
        y = torch.cat([d.y for d in data_list], dim=0)
        y_graph = torch.cat([d.y_graph for d in data_list], dim=0)
        edge_index_list = []
        batch_list = []
        for i, d in enumerate(data_list):
            edge_index_list.append(d.edge_index + i * num_nodes_per_graph)
            batch_list.append(torch.full((d.x.size(0),), i, dtype=torch.long))
        edge_index = torch.cat(edge_index_list, dim=1)
        batch = torch.cat(batch_list, dim=0)
        return BatchGraph(x, edge_index, y, y_graph, batch)
    
    DataLoader = torch.utils.data.DataLoader



# ============================================================
# CANONICAL SERVICES & TOPOLOGY DEFINITIONS
# ============================================================

SERVICE_NAMES: List[str] = [
    "frontend",
    "cartservice",
    "productcatalogservice",
    "redis-cart",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "shippingservice",
    "adservice",
    "recommendationservice",
    "paymentservice",
]

NUM_SERVICES = len(SERVICE_NAMES)
SERVICE_TO_IDX: Dict[str, int] = {name: idx for idx, name in enumerate(SERVICE_NAMES)}
IDX_TO_SERVICE: Dict[int, str] = {idx: name for idx, name in enumerate(SERVICE_NAMES)}

LABEL_MAP: Dict[str, int] = {
    "NORMAL": 0,
    "FAULT_CPU_STRESS": 1,
    "FAULT_MEMORY_LEAK": 2,
    "FAULT_POD_CRASH": 3,
    "FAULT_NETWORK_LATENCY": 4,
}
IDX_TO_LABEL: Dict[int, str] = {idx: name for name, idx in LABEL_MAP.items()}

# Pod penempatan per node (sesuai setup_cluster.sh & Kubernetes nodeSelector)
NODE_WORKERS: Dict[str, List[str]] = {
    "worker-1": ["frontend", "recommendationservice", "paymentservice"],
    "worker-2": ["adservice", "cartservice", "productcatalogservice", "redis-cart"],
    "worker-3": ["checkoutservice", "currencyservice", "emailservice", "shippingservice"],
}

# Dependensi pemanggilan RPC antar-service Online Boutique
CANONICAL_CALL_DEPENDENCIES: List[Tuple[str, str]] = [
    ("frontend", "cartservice"),
    ("frontend", "productcatalogservice"),
    ("frontend", "recommendationservice"),
    ("frontend", "shippingservice"),
    ("frontend", "checkoutservice"),
    ("frontend", "adservice"),
    ("checkoutservice", "cartservice"),
    ("checkoutservice", "productcatalogservice"),
    ("checkoutservice", "shippingservice"),
    ("checkoutservice", "paymentservice"),
    ("checkoutservice", "emailservice"),
    ("checkoutservice", "currencyservice"),
    ("recommendationservice", "productcatalogservice"),
    ("cartservice", "redis-cart"),
]

# 18 Fitur ternormalisasi yang dihasilkan oleh feature_engineer.py
NORMALIZED_FEATURE_COLS: List[str] = [
    "cpu_usage_norm",
    "memory_usage_norm",
    "memory_usage_percent_norm",
    "pod_restarts_norm",
    "net_rx_bytes_norm",
    "net_tx_bytes_norm",
    "request_rate_norm",
    "error_rate_norm",
    "cpu_delta_norm",
    "memory_delta_norm",
    "memory_growth_rate_norm",
    "cpu_rolling_mean_5_norm",
    "cpu_rolling_std_5_norm",
    "mem_rolling_mean_5_norm",
    "restart_delta_norm",
    "net_total_bytes_norm",
    "net_rx_tx_ratio_norm",
    "anomaly_score_raw_norm",
]

BASE_NUMERIC_COLS: List[str] = [
    "cpu_usage", "memory_usage", "memory_usage_percent",
    "pod_restarts", "net_rx_bytes", "net_tx_bytes",
    "request_rate", "error_rate",
    "cpu_delta", "memory_delta", "memory_growth_rate",
    "cpu_rolling_mean_5", "cpu_rolling_std_5", "mem_rolling_mean_5",
    "restart_delta", "net_total_bytes", "net_rx_tx_ratio",
    "anomaly_score_raw",
]


def extract_service_name(pod_name: str) -> str:
    """Mengekstrak canonical service name dari pod_name."""
    pod_lower = str(pod_name).lower().strip()
    for svc in SERVICE_NAMES:
        if pod_lower == svc or pod_lower.startswith(f"{svc}-"):
            return svc
    # Parsing fallback
    parts = pod_lower.split("-")
    if len(parts) >= 3:
        candidate = "-".join(parts[:-2])
        if candidate in SERVICE_TO_IDX:
            return candidate
    return pod_lower


# ============================================================
# GRAPH TOPOLOGY BUILDER
# ============================================================

def build_static_edge_index(topology_path: Optional[Path] = None,
                            include_co_location: bool = True,
                            bidirectional: bool = True,
                            self_loops: bool = True) -> torch.Tensor:
    """
    Membangun edge_index (format COO [2, E]) yang menghubungkan 11 microservices.
    
    Menyatukan:
      1. RPC Dependencies (dari topology.json atau canonical fallback)
      2. Host Co-location Edges (sesama worker node)
      3. Self-loops
    """
    edges_set = set()

    # 1. Network RPC dependencies
    rpc_calls = list(CANONICAL_CALL_DEPENDENCIES)
    if topology_path and topology_path.exists():
        try:
            with open(topology_path, "r", encoding="utf-8") as f:
                topo = json.load(f)
                for edge in topo.get("edges", []):
                    src = extract_service_name(edge.get("source", ""))
                    tgt = extract_service_name(edge.get("target", ""))
                    if src in SERVICE_TO_IDX and tgt in SERVICE_TO_IDX and src != tgt:
                        rpc_calls.append((src, tgt))
        except Exception as e:
            logger.warning(f"Gagal membaca {topology_path}: {e}, menggunakan canonical call list.")

    for src_name, tgt_name in rpc_calls:
        if src_name in SERVICE_TO_IDX and tgt_name in SERVICE_TO_IDX:
            src_idx = SERVICE_TO_IDX[src_name]
            tgt_idx = SERVICE_TO_IDX[tgt_name]
            edges_set.add((src_idx, tgt_idx))
            if bidirectional:
                # Latensi dan error merambat balik ke caller
                edges_set.add((tgt_idx, src_idx))

    # 2. Host Co-location edges (noisy neighbor awareness)
    if include_co_location:
        for worker, pods in NODE_WORKERS.items():
            for p1 in pods:
                for p2 in pods:
                    if p1 != p2 and p1 in SERVICE_TO_IDX and p2 in SERVICE_TO_IDX:
                        edges_set.add((SERVICE_TO_IDX[p1], SERVICE_TO_IDX[p2]))

    # 3. Self-loops
    if self_loops:
        for i in range(NUM_SERVICES):
            edges_set.add((i, i))

    edge_list = sorted(list(edges_set))
    src = [e[0] for e in edge_list]
    tgt = [e[1] for e in edge_list]

    edge_index = torch.tensor([src, tgt], dtype=torch.long)
    logger.info(f"Topologi graf siap: {NUM_SERVICES} nodes, {edge_index.shape[1]} edges "
                f"(Co-location={include_co_location}, Bidir={bidirectional})")
    return edge_index


# ============================================================
# PYTORCH GEOMETRIC DATASET
# ============================================================

class MicroserviceGraphDataset(Dataset):
    """
    Dataset sequence snapshot graf untuk GNN.
    
    Setiap sampel Data berisi:
      x         : Tensor [11, 18] fitur metrik per node
      edge_index: Tensor [2, E] relasi topologi graf
      y         : Tensor [11] label anomali per node (0 s/d 4)
      y_graph   : Tensor [1] skor urgensi global (0.0 jika semua normal, 1.0 jika ada fault)
    """

    def __init__(self, data_list: List[Data]):
        super().__init__()
        self.data_list = data_list

    def len(self) -> int:
        return len(self.data_list)

    def get(self, idx: int) -> Data:
        return self.data_list[idx]

    def __len__(self) -> int:
        return len(self.data_list)

    def __getitem__(self, idx: int) -> Data:
        return self.data_list[idx]


def create_graph_snapshots_from_csv(csv_path: Path,
                                    topology_path: Optional[Path] = None,
                                    scaler_path: Optional[Path] = None) -> List[Data]:
    """
    Membaca dataset_ready.csv, mengelompokkan per timestep,
    dan membuat daftar Data objek PyTorch Geometric.
    """
    logger.info(f"Membaca dataset: {csv_path.name}...")
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["service_name"] = df["pod_name"].apply(extract_service_name)

    # Filter hanya pod microservice yang valid
    df = df[df["service_name"].isin(SERVICE_TO_IDX)].copy()

    # Pastikan label_id tersedia
    if "label_id" not in df.columns:
        if "label" in df.columns:
            df["label_id"] = df["label"].map(LABEL_MAP).fillna(0).astype(int)
        else:
            df["label_id"] = 0

    # Pastikan fitur ternormalisasi tersedia
    features_to_use = []
    for col in NORMALIZED_FEATURE_COLS:
        if col in df.columns:
            features_to_use.append(col)
    
    if len(features_to_use) < len(NORMALIZED_FEATURE_COLS):
        logger.warning(f"Hanya {len(features_to_use)}/{len(NORMALIZED_FEATURE_COLS)} kolom *_norm ditemukan. "
                       f"Melakukan min-max fallback.")
        for base_col in BASE_NUMERIC_COLS:
            norm_col = f"{base_col}_norm"
            if norm_col not in df.columns and base_col in df.columns:
                mn, mx = df[base_col].min(), df[base_col].max()
                df[norm_col] = 0.0 if mx == mn else ((df[base_col] - mn) / (mx - mn)).clip(0, 1)
                features_to_use.append(norm_col)

    features_to_use = sorted(list(set(features_to_use)))
    num_features = len(features_to_use)
    logger.info(f"Menggunakan {num_features} fitur node: {features_to_use[:4]} ...")

    # Siapkan Edge Index statis
    edge_index = build_static_edge_index(topology_path=topology_path)

    # Kelompokkan berdasarkan interval waktu (5 detik)
    # Bulatkan timestamp ke kelipatan 5 detik terdekat
    df["time_bin"] = df["timestamp"].dt.floor("5s")
    grouped = df.groupby("time_bin")

    snapshots: List[Data] = []
    last_known_features = {svc: np.zeros(num_features, dtype=np.float32) for svc in SERVICE_NAMES}

    for time_bin, group in grouped:
        x_matrix = np.zeros((NUM_SERVICES, num_features), dtype=np.float32)
        y_vector = np.zeros(NUM_SERVICES, dtype=np.int64)

        # Isi status per service yang ada di group
        seen_services = set()
        for _, row in group.iterrows():
            svc = row["service_name"]
            svc_idx = SERVICE_TO_IDX[svc]
            feat_vals = row[features_to_use].values.astype(np.float32)
            
            # Update cache
            last_known_features[svc] = feat_vals
            seen_services.add(svc)

            x_matrix[svc_idx] = feat_vals
            y_vector[svc_idx] = max(int(y_vector[svc_idx]), int(row["label_id"]))

        # Forward-fill untuk service yang scrape-nya miss di timestamp ini
        for svc in SERVICE_NAMES:
            if svc not in seen_services:
                svc_idx = SERVICE_TO_IDX[svc]
                x_matrix[svc_idx] = last_known_features[svc]
                y_vector[svc_idx] = 0  # Default normal jika miss

        # Graph-level urgency: 0.0 jika semua normal, 1.0 jika ada minimal 1 anomali
        has_anomaly = (y_vector > 0).any()
        y_graph = 1.0 if has_anomaly else 0.0

        data_obj = Data(
            x=torch.tensor(x_matrix, dtype=torch.float32),
            edge_index=edge_index.clone(),
            y=torch.tensor(y_vector, dtype=torch.long),
            y_graph=torch.tensor([y_graph], dtype=torch.float32),
        )
        snapshots.append(data_obj)

    logger.success(f"Berhasil menghasilkan {len(snapshots):,} snapshot graf (N={NUM_SERVICES}, F={num_features})")
    return snapshots


# ============================================================
# DATALOADER BUILDER DENGAN CHRONOLOGICAL SPLIT
# ============================================================

def build_graph_dataloaders(csv_path: Optional[Path] = None,
                            topology_path: Optional[Path] = None,
                            batch_size: int = 32,
                            train_ratio: float = 0.70,
                            val_ratio: float = 0.15) -> Tuple[DataLoader, DataLoader, DataLoader, dict]:
    """
    Membuat Train, Val, dan Test PyG DataLoaders dengan pembagian
    secara kronologis (urutan waktu) untuk mencegah data leakage.
    """
    base_dir = Path(__file__).resolve().parent.parent.parent
    if csv_path is None:
        csv_path = base_dir / "data" / "processed" / "dataset_ready.csv"
    
    if topology_path is None:
        # Cari file topology terbaru
        topo_files = sorted(glob.glob(str(base_dir / "data" / "raw" / "topology_*.json")))
        if topo_files:
            topology_path = Path(topo_files[-1])

    if not csv_path.exists():
        raise FileNotFoundError(f"File dataset tidak ditemukan: {csv_path}")

    # Generate graph snapshots
    snapshots = create_graph_snapshots_from_csv(csv_path, topology_path=topology_path)
    total_snapshots = len(snapshots)

    # Chronological Split
    train_size = int(total_snapshots * train_ratio)
    val_size = int(total_snapshots * val_ratio)
    test_size = total_snapshots - train_size - val_size

    train_data = snapshots[:train_size]
    val_data = snapshots[train_size:train_size + val_size]
    test_data = snapshots[train_size + val_size:]

    logger.info(f"Dataset Split (Kronologis):")
    logger.info(f"  Train : {len(train_data):>5,} graf ({len(train_data)/total_snapshots*100:4.1f}%)")
    logger.info(f"  Val   : {len(val_data):>5,} graf ({len(val_data)/total_snapshots*100:4.1f}%)")
    logger.info(f"  Test  : {len(test_data):>5,} graf ({len(test_data)/total_snapshots*100:4.1f}%)")

    train_dataset = MicroserviceGraphDataset(train_data)
    val_dataset = MicroserviceGraphDataset(val_data)
    test_dataset = MicroserviceGraphDataset(test_data)

    # PyG DataLoader menangani batching graf secara block-diagonal
    collate_fn = None if PYG_AVAILABLE else fallback_collate_fn
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, collate_fn=collate_fn) if not PYG_AVAILABLE else DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, collate_fn=collate_fn) if not PYG_AVAILABLE else DataLoader(val_dataset, batch_size=batch_size, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False, collate_fn=collate_fn) if not PYG_AVAILABLE else DataLoader(test_dataset, batch_size=batch_size, shuffle=False)


    info = {
        "num_services": NUM_SERVICES,
        "num_features": train_data[0].x.shape[1],
        "num_classes": len(LABEL_MAP),
        "total_snapshots": total_snapshots,
        "num_edges": train_data[0].edge_index.shape[1],
    }

    return train_loader, val_loader, test_loader, info


if __name__ == "__main__":
    logger.info("Testing MicroserviceGraphDataset builder...")
    try:
        t_loader, v_loader, test_loader, info = build_graph_dataloaders()
        for batch in t_loader:
            print("\nSample PyG Batch:")
            print(f"  Batch x shape          : {batch.x.shape}")
            print(f"  Batch edge_index shape : {batch.edge_index.shape}")
            print(f"  Batch y (nodes) shape  : {batch.y.shape}")
            print(f"  Batch y_graph shape    : {batch.y_graph.shape}")
            break
        logger.success("Dataset builder test passed!")
    except Exception as e:
        logger.error(f"Test failed: {e}")
