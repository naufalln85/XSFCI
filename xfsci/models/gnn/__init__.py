"""
============================================================
XFSCI GNN Package - Layer 2: Cloud Intelligence
============================================================
Menyediakan modul Graph Neural Network untuk deteksi anomali
dan lokalisasi akar masalah (root-cause localization)
berdasarkan graf topologi microservices.
============================================================
"""

from .graph_dataset import (
    MicroserviceGraphDataset,
    SERVICE_NAMES,
    SERVICE_TO_IDX,
    IDX_TO_SERVICE,
    LABEL_MAP,
    IDX_TO_LABEL,
    build_graph_dataloaders,
)
from .gnn_model import DualHeadGATv2
from .gnn_predictor import GNNPredictor

__all__ = [
    "MicroserviceGraphDataset",
    "SERVICE_NAMES",
    "SERVICE_TO_IDX",
    "IDX_TO_SERVICE",
    "LABEL_MAP",
    "IDX_TO_LABEL",
    "build_graph_dataloaders",
    "DualHeadGATv2",
    "GNNPredictor",
]
