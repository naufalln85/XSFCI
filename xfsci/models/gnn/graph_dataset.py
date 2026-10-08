"""
============================================================
XFSCI Graph Dataset Builder - Layer 2: Cloud Intelligence
============================================================
Mengonversi dataset tabular time-series (dataset_ready.csv)
dan topologi microservice (topology_*.json) menjadi sequence
graf teratribusi PyTorch Geometric (torch_geometric.data.Data).

Fitur Graf:
  - Nodes (11 services): Online Boutique microservices
  - Node Features (feature-contract driven): Metrik ternormalisasi [0, 1]
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
import hashlib
import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd
import torch
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.gnn.feature_contract import (
    FEATURE_PIPELINE_VERSION,
    MODEL_FEATURE_COLS,
    SERVICE_NAMES,
    extract_service_name,
)
from models.gnn.session_split import split_session_ids
from models.gnn.service_aggregation import aggregate_service_rows

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

# Feature order always comes from the single training/live contract.
NORMALIZED_FEATURE_COLS: List[str] = sorted(f"{name}_norm" for name in MODEL_FEATURE_COLS)
BASE_NUMERIC_COLS: List[str] = list(MODEL_FEATURE_COLS)
IGNORE_LABEL = -100


# ============================================================
# GRAPH TOPOLOGY BUILDER
# ============================================================

def build_static_edge_index(topology_path: Optional[Path] = None,
                            include_co_location: bool = False,
                            bidirectional: bool = False,
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
      x         : Tensor [11, F] fitur metrik per node
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
    required_quality = {"telemetry_complete", "session_id", "label_id"}
    missing_quality = sorted(required_quality - set(df.columns))
    if missing_quality:
        raise ValueError(
            f"Dataset lacks v5 sample-quality fields {missing_quality}; "
            "recollect and rerun labeling/feature engineering."
        )
    label_values = pd.to_numeric(df["label_id"], errors="coerce")
    df["label_id"] = label_values.where(label_values.between(0, len(LABEL_MAP) - 1), IGNORE_LABEL).fillna(IGNORE_LABEL).astype(int)
    df["service_name"] = df["pod_name"].apply(extract_service_name)

    # Filter hanya pod microservice yang valid
    df = df[df["service_name"].isin(SERVICE_TO_IDX)].copy()

    features_to_use = sorted(NORMALIZED_FEATURE_COLS)
    missing_features = sorted(set(features_to_use) - set(df.columns))
    if missing_features:
        raise ValueError(
            f"Dataset tidak memenuhi kontrak fitur {FEATURE_PIPELINE_VERSION}: "
            f"missing={missing_features}. "
            "Jalankan ulang feature_engineer.py sebelum melatih GNN."
        )
    if scaler_path is None:
        scaler_path = PROJECT_ROOT / "data" / "processed" / "scaler_params.json"
    if not scaler_path.exists():
        raise FileNotFoundError(f"Scaler parameters missing: {scaler_path}; rerun feature_engineer.py")
    with scaler_path.open("r", encoding="utf-8") as handle:
        scaler_params = json.load(handle)
    missing_scaler = sorted(set(BASE_NUMERIC_COLS) - set(scaler_params))
    if missing_scaler:
        raise ValueError(f"Scaler is incomplete for {FEATURE_PIPELINE_VERSION}: {missing_scaler}")
    num_features = len(features_to_use)
    logger.info(f"Menggunakan {num_features} fitur node: {features_to_use[:4]} ...")

    # Siapkan Edge Index statis (RPC dependencies murni searah, mencegah over-smoothing)
    edge_index = build_static_edge_index(
        topology_path=topology_path,
        include_co_location=False,
        bidirectional=False,
        self_loops=True,
    )

    # Deteksi session_id jika belum ada (misal digabung multi-sesi dengan jeda > 5 menit).
    if "session_id" not in df.columns:
        sorted_ts = df["timestamp"].sort_values()
        gaps = sorted_ts.diff() > pd.Timedelta(minutes=5)
        session_map = gaps.cumsum()
        df["session_id"] = "session_" + session_map.reindex(df.index).fillna(0).astype(int).astype(str)

    # Kelompokkan berdasarkan interval waktu (5 detik)
    # Bulatkan timestamp ke kelipatan 5 detik terdekat
    df["time_bin"] = df["timestamp"].dt.floor("5s")
    df = df.sort_values(["timestamp", "pod_name"])
    grouped = df.groupby(["session_id", "time_bin"], sort=False)

    snapshots: List[Data] = []
    last_known_features = {svc: np.zeros(num_features, dtype=np.float32) for svc in SERVICE_NAMES}
    last_session = None

    for (session_val, time_bin), group in grouped:
        session_val = str(session_val)
        if session_val != last_session:
            last_known_features = {svc: np.zeros(num_features, dtype=np.float32) for svc in SERVICE_NAMES}
            last_session = session_val
        x_matrix = np.zeros((NUM_SERVICES, num_features), dtype=np.float32)
        y_vector = np.full(NUM_SERVICES, IGNORE_LABEL, dtype=np.int64)

        # Aggregate only complete replica telemetry. Max-risk features preserve
        # a single affected replica instead of averaging its signal away.
        seen_services = set()
        service_rows = group.groupby("service_name", sort=False)
        for svc, service_group in service_rows:
            svc_idx = SERVICE_TO_IDX[svc]
            seen_services.add(svc)

            complete_mask = pd.to_numeric(
                service_group["telemetry_complete"], errors="coerce"
            ).fillna(0).eq(1)
            complete_rows = service_group.loc[complete_mask]
            service_complete = bool(not service_group.empty and complete_mask.all())
            labels = pd.to_numeric(service_group["label_id"], errors="coerce").dropna().astype(int)
            event_fault = int(labels[labels > 0].max()) if (labels > 0).any() else None

            if service_complete and not complete_rows.empty:
                raw_vals = aggregate_service_rows(complete_rows, BASE_NUMERIC_COLS)
                scaled = {}
                for column, value in zip(BASE_NUMERIC_COLS, raw_vals):
                    params = scaler_params[column]
                    lo, hi = float(params["min"]), float(params["max"])
                    scaled[f"{column}_norm"] = (
                        0.0 if hi <= lo else float(np.clip((value - lo) / (hi - lo), 0.0, 1.0))
                    )
                feat_vals = np.asarray([scaled[column] for column in features_to_use], dtype=np.float32)
                last_known_features[svc] = feat_vals
                x_matrix[svc_idx] = feat_vals
                if event_fault is not None:
                    y_vector[svc_idx] = event_fault
            else:
                # Readiness is an independently measured service signal. Keep
                # it even when pod metrics are missing; all other model inputs
                # use last-known telemetry and the node label remains unknown.
                feat_vals = last_known_features[svc].copy()
                readiness_col = "service_ready_ratio_norm"
                if readiness_col in features_to_use and readiness_col in service_group:
                    readiness_values = pd.to_numeric(
                        service_group[readiness_col], errors="coerce"
                    ).dropna()
                    if not readiness_values.empty:
                        feat_vals[features_to_use.index(readiness_col)] = float(readiness_values.max())
                x_matrix[svc_idx] = feat_vals
                if event_fault is not None:
                    y_vector[svc_idx] = event_fault

            # A NORMAL class is known only when every replica row for this
            # service/time bin has complete telemetry. Missing one replica
            # must not silently turn the service into a normal training target.
            if service_complete and event_fault is None:
                known_labels = pd.to_numeric(
                    service_group["label_id"], errors="coerce"
                )
                if known_labels.notna().all() and known_labels.between(0, len(LABEL_MAP) - 1).all():
                    y_vector[svc_idx] = int(known_labels.max())

        # Forward-fill untuk service yang scrape-nya miss di timestamp ini
        for svc in SERVICE_NAMES:
            if svc not in seen_services:
                svc_idx = SERVICE_TO_IDX[svc]
                x_matrix[svc_idx] = last_known_features[svc]

        # Graph labels are unknown when any node is unknown and no confirmed
        # fault exists. This avoids turning telemetry gaps into healthy graphs.
        has_fault = bool((y_vector > 0).any())
        all_known = bool((y_vector != IGNORE_LABEL).all())
        y_graph = 1.0 if has_fault else (0.0 if all_known else -1.0)

        data_obj = Data(
            x=torch.tensor(x_matrix, dtype=torch.float32),
            edge_index=edge_index.clone(),
            y=torch.tensor(y_vector, dtype=torch.long),
            y_graph=torch.tensor([y_graph], dtype=torch.float32),
            session_id=session_val,
        )
        snapshots.append(data_obj)

    logger.success(f"Berhasil menghasilkan {len(snapshots):,} snapshot graf (N={NUM_SERVICES}, F={num_features})")
    return snapshots


# ============================================================
# DATALOADER BUILDER DENGAN SESSION-AWARE & CHRONOLOGICAL SPLIT
# ============================================================

def build_graph_dataloaders(csv_path: Optional[Path] = None,
                            topology_path: Optional[Path] = None,
                            batch_size: int = 32,
                            train_ratio: float = 0.70,
                            val_ratio: float = 0.15) -> Tuple[DataLoader, DataLoader, DataLoader, dict]:
    """
    Membuat Train, Val, dan Test PyG DataLoaders.
    Mendukung Session-Aware Split (satu sesi utuh masuk ke satu kelompok)
    atau fallback ke Chronological Split jika hanya ada satu sesi.
    """
    base_dir = Path(__file__).resolve().parent.parent.parent
    contract_path = base_dir / "data" / "processed" / "feature_contract.json"
    if not contract_path.exists():
        raise FileNotFoundError(
            f"Feature contract tidak ditemukan: {contract_path}. Jalankan feature_engineer.py terlebih dahulu."
        )
    with open(contract_path, "r", encoding="utf-8") as contract_file:
        feature_contract = json.load(contract_file)
    if feature_contract.get("version") != FEATURE_PIPELINE_VERSION:
        raise ValueError(
            f"Feature contract versi {feature_contract.get('version')!r}; "
            f"trainer memerlukan {FEATURE_PIPELINE_VERSION!r}. Jalankan ulang feature_engineer.py."
        )
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

    # Deteksi apakah tersedia pemisahan berbasis sesi (Session-Aware)
    unique_sessions = list(dict.fromkeys(
        s.session_id for s in snapshots if getattr(s, "session_id", None) is not None
    ))

    if len(unique_sessions) >= 3:
        train_ids, val_ids, test_ids = split_session_ids(unique_sessions, train_ratio, val_ratio)
        train_sess = set(train_ids)
        val_sess = set(val_ids)
        test_sess = set(test_ids)

        train_data = [s for s in snapshots if s.session_id in train_sess]
        val_data = [s for s in snapshots if s.session_id in val_sess]
        test_data = [s for s in snapshots if s.session_id in test_sess]

        logger.info(f"Dataset Split (Session-Aware / Bebas Data Leakage):")
        logger.info(f"  Sessions total: {len(unique_sessions)} -> Train: {list(train_sess)}, Val: {list(val_sess)}, Test: {list(test_sess)}")
        logger.info(f"  Train : {len(train_data):>5,} graf ({len(train_data)/total_snapshots*100:4.1f}%)")
        logger.info(f"  Val   : {len(val_data):>5,} graf ({len(val_data)/total_snapshots*100:4.1f}%)")
        logger.info(f"  Test  : {len(test_data):>5,} graf ({len(test_data)/total_snapshots*100:4.1f}%)")
        test_labels = np.concatenate([s.y.numpy() for s in test_data])
        test_labels = test_labels[test_labels != IGNORE_LABEL]
        logger.info(f"  Test known node classes: {dict(pd.Series(test_labels).value_counts().sort_index())}")
    elif len(unique_sessions) == 2:
        train_data = [s for s in snapshots if s.session_id == unique_sessions[0]]
        test_data = [s for s in snapshots if s.session_id == unique_sessions[1]]
        n_val = max(1, int(len(train_data) * val_ratio))
        val_data = train_data[-n_val:]
        train_data = train_data[:-n_val]
        logger.info(f"Dataset Split (2 Sessions): Train/Val={unique_sessions[0]}, Test={unique_sessions[1]}")
    else:
        # Chronological Split (Single Session fallback)
        train_size = int(total_snapshots * train_ratio)
        val_size = int(total_snapshots * val_ratio)
        test_size = total_snapshots - train_size - val_size

        train_data = snapshots[:train_size]
        val_data = snapshots[train_size:train_size + val_size]
        test_data = snapshots[train_size + val_size:]

        logger.info(f"Dataset Split (Kronologis - Single Session):")
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
        "session_aware": len(unique_sessions) >= 3,
        "session_count": len(unique_sessions),
        "train_sessions": sorted({s.session_id for s in train_data if getattr(s, "session_id", None) is not None}),
        "validation_sessions": sorted({s.session_id for s in val_data if getattr(s, "session_id", None) is not None}),
        "test_sessions": sorted({s.session_id for s in test_data if getattr(s, "session_id", None) is not None}),
        "dataset_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "feature_pipeline_version": FEATURE_PIPELINE_VERSION,
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
