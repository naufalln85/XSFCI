"""
============================================================
XFSCI GNN Predictor - Layer 2: Cloud Intelligence Inference
============================================================
Modul inferensi real-time untuk GNN (DualHeadGATv2):
  1. Memuat bobot model terlatih (gnn_best.pt)
  2. Mengonstruksi tensor fitur [11, 18] dari metrik pod cluster
  3. Menjalankan forward pass GNN (< 3ms di CPU)
  4. Menghasilkan objek MLPrediction (Action Schema) yang siap
     dikonsumsi langsung oleh Orchestrator & Decision Agent (Groq).

Fitur Diagnosis:
  - Root Cause Localization (Pod mana yang menjadi sumber anomali)
  - Cascade Risk Analysis (Pod hilir mana yang terancam terkena efek domino)
  - Explainable Attention Weights
============================================================
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import torch
import torch.nn.functional as F
import numpy as np
from loguru import logger

# Pastikan root proyek ada di sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from agent.action_schema import MLPrediction, AnomalyType
from models.gnn.graph_dataset import (
    SERVICE_NAMES,
    SERVICE_TO_IDX,
    IDX_TO_SERVICE,
    LABEL_MAP,
    IDX_TO_LABEL,
    NORMALIZED_FEATURE_COLS,
    build_static_edge_index,
    extract_service_name,
)
from models.gnn.gnn_model import DualHeadGATv2

# Mapping dari GNN label ke AnomalyType Action Schema
GNN_LABEL_TO_ANOMALY_TYPE = {
    0: AnomalyType.NORMAL,
    1: AnomalyType.CPU_OVERLOAD,
    2: AnomalyType.MEMORY_LEAK,
    3: AnomalyType.POD_CRASH_LOOP,
    4: AnomalyType.NETWORK_LATENCY,
}


class GNNPredictor:
    """
    Inference Engine untuk GNN Layer 2.
    
    Menghubungkan model graf ke XFSCI Orchestrator pipeline.
    """

    def __init__(self,
                 weights_path: Optional[Path] = None,
                 scaler_path: Optional[Path] = None,
                 device: str = "cpu"):
        self.device = torch.device(device)
        base_dir = Path(__file__).resolve().parent.parent.parent

        if weights_path is None:
            weights_path = Path(__file__).resolve().parent / "weights" / "gnn_best.pt"
        self.weights_path = Path(weights_path)

        if scaler_path is None:
            scaler_path = base_dir / "data" / "processed" / "scaler_params.json"
        self.scaler_path = Path(scaler_path)

        self.model: Optional[DualHeadGATv2] = None
        self.scaler_params: Dict[str, Any] = {}
        self.edge_index = build_static_edge_index().to(self.device)
        self.is_ready = False

        self._load_scaler()
        self._load_model()

    def _load_scaler(self):
        """Memuat parameter normalisasi min-max."""
        if self.scaler_path.exists():
            try:
                with open(self.scaler_path, "r", encoding="utf-8") as f:
                    self.scaler_params = json.load(f)
                logger.info(f"Scaler params loaded from: {self.scaler_path.name}")
            except Exception as e:
                logger.warning(f"Gagal membaca scaler params: {e}")
        else:
            logger.warning(f"Scaler params tidak ditemukan di {self.scaler_path}, menggunakan fallback default.")

    def _load_model(self):
        """Memuat bobot checkpoint model."""
        if not self.weights_path.exists():
            logger.warning(f"GNN weights belum ditemukan di: {self.weights_path}. Model belum dilatih.")
            self.is_ready = False
            return

        try:
            checkpoint = torch.load(self.weights_path, map_location=self.device)
            cfg = checkpoint.get("model_config", {
                "in_channels": len(NORMALIZED_FEATURE_COLS),
                "hidden_dim": 32,
                "num_heads": 4,
                "num_classes": len(LABEL_MAP),
                "dropout": 0.2
            })

            self.model = DualHeadGATv2(
                in_channels=cfg["in_channels"],
                hidden_dim=cfg.get("hidden_dim", 32),
                num_heads=cfg.get("num_heads", 4),
                num_classes=cfg.get("num_classes", 5),
                dropout=cfg.get("dropout", 0.2)
            ).to(self.device)

            self.model.load_state_dict(checkpoint["model_state_dict"])
            self.model.eval()
            self.is_ready = True

            metrics = checkpoint.get("training_metrics", {})
            test_acc = metrics.get("test_accuracy", 0.0)
            logger.success(f"GNN Model loaded successfully! (Test Acc: {test_acc*100:.2f}%)")
        except Exception as e:
            logger.error(f"Gagal memuat model GNN: {e}")
            self.is_ready = False

    def build_feature_tensor_from_metrics(self, current_metrics_map: Dict[str, Dict[str, float]]) -> torch.Tensor:
        """
        Mengonstruksi tensor input [11, 18] dari snapshot metrik real-time.
        
        Args:
          current_metrics_map: Dict {service_name: {metric_col: float_val}}
          
        Returns:
          Tensor [11, 18] ternormalisasi
        """
        num_features = len(NORMALIZED_FEATURE_COLS)
        x_matrix = np.zeros((len(SERVICE_NAMES), num_features), dtype=np.float32)

        for svc_idx, svc_name in enumerate(SERVICE_NAMES):
            svc_metrics = current_metrics_map.get(svc_name, {})

            for f_idx, feat_col in enumerate(NORMALIZED_FEATURE_COLS):
                base_col = feat_col.replace("_norm", "")
                val = float(svc_metrics.get(feat_col, svc_metrics.get(base_col, 0.0)))

                # Terapkan min-max scaling jika nilai masih raw
                if feat_col not in svc_metrics and base_col in self.scaler_params:
                    p = self.scaler_params[base_col]
                    mn, mx = p.get("min", 0.0), p.get("max", 1.0)
                    if mx > mn:
                        val = (val - mn) / (mx - mn)
                        val = np.clip(val, 0.0, 1.0)

                x_matrix[svc_idx, f_idx] = val

        return torch.tensor(x_matrix, dtype=torch.float32, device=self.device)

    def predict_target(self,
                       target_deployment: str,
                       current_metrics_map: Optional[Dict[str, Dict[str, float]]] = None) -> MLPrediction:
        """
        Fungsi utama yang dipanggil oleh Orchestrator Step [2/7].
        
        Args:
          target_deployment  : Nama deployment target (misal: "cartservice")
          current_metrics_map: Peta metrik semua service (opsional)
          
        Returns:
          MLPrediction (Pydantic object)
        """
        target_svc = extract_service_name(target_deployment)
        target_idx = SERVICE_TO_IDX.get(target_svc, 0)

        # Fallback jika model belum siap / belum dilatih
        if not self.is_ready or self.model is None:
            logger.warning("GNN Model belum aktif. Fallback ke default safe prediction.")
            return MLPrediction(
                risk_score=0.1,
                anomaly_type=AnomalyType.NORMAL,
                confidence=0.80,
                time_to_failure_minutes=None,
                cascade_risk=[]
            )

        start_time = time.time()

        # Konstruksi input tensor
        if current_metrics_map is None:
            current_metrics_map = {}
        x_tensor = self.build_feature_tensor_from_metrics(current_metrics_map)

        with torch.no_grad():
            node_logits, graph_urgency, attn = self.model(
                x_tensor, self.edge_index, batch=None, return_attention=True
            )
            node_probs = F.softmax(node_logits, dim=-1)

        elapsed_ms = (time.time() - start_time) * 1000

        # Global cluster risk
        cluster_risk = float(graph_urgency.squeeze().item())

        # Diagnosis Target Pod (Hierarchical Gated)
        if cluster_risk < 0.50:
            pred_label_id = 0
            confidence = float(1.0 - cluster_risk)
            anomaly_type = AnomalyType.NORMAL
        else:
            target_probs = node_probs[target_idx].cpu().numpy()
            pred_label_id = int(np.argmax(target_probs))
            confidence = float(target_probs[pred_label_id])
            anomaly_type = GNN_LABEL_TO_ANOMALY_TYPE.get(pred_label_id, AnomalyType.NORMAL)

        # Root Cause Analysis: Cari pod dengan probabilitas anomali non-normal tertinggi
        non_normal_probs = 1.0 - node_probs[:, 0].cpu().numpy()
        root_cause_idx = int(np.argmax(non_normal_probs))
        root_cause_svc = IDX_TO_SERVICE[root_cause_idx]
        root_cause_score = float(non_normal_probs[root_cause_idx])

        # Cascade Risk: Cari pod tetangga yang probabilitas anomali-nya di atas ambang batas (0.35)
        cascade_pods = []
        for idx, svc in enumerate(SERVICE_NAMES):
            if idx != target_idx and non_normal_probs[idx] > 0.35:
                cascade_pods.append(f"{svc} ({non_normal_probs[idx]:.0%})")

        logger.info(f"🧠 GNN Inference [{elapsed_ms:.2f}ms] | Target: {target_svc} -> {anomaly_type.value} "
                    f"({confidence:.0%}) | Cluster Risk: {cluster_risk:.2f} | Root Cause: {root_cause_svc} ({root_cause_score:.0%})")

        return MLPrediction(
            risk_score=round(cluster_risk, 3),
            anomaly_type=anomaly_type,
            confidence=round(confidence, 3),
            time_to_failure_minutes=5.0 if anomaly_type != AnomalyType.NORMAL else None,
            cascade_risk=cascade_pods
        )


if __name__ == "__main__":
    logger.info("Testing GNNPredictor...")
    predictor = GNNPredictor()
    pred = predictor.predict_target("cartservice")
    print(f"\nMLPrediction Result:")
    print(f"  Risk Score   : {pred.risk_score}")
    print(f"  Anomaly Type : {pred.anomaly_type}")
    print(f"  Confidence   : {pred.confidence}")
    print(f"  Cascade Risk : {pred.cascade_risk}")
