"""
============================================================
XFSCI GNN Predictor - Layer 2: Cloud Intelligence Inference
============================================================
Modul inferensi real-time untuk GNN (DualHeadGATv2):
  1. Memuat bobot model terlatih (gnn_best.pt)
  2. Mengonstruksi tensor fitur [11, F] dari metrik seluruh service cluster
  3. Menjalankan forward pass GNN (< 3ms di CPU)
  4. Menghasilkan objek MLPrediction (Action Schema) yang siap
      dikonsumsi langsung oleh Orchestrator & Decision Agent (Antigravity).

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
    FEATURE_PIPELINE_VERSION,
    build_static_edge_index,
    extract_service_name,
)
from models.gnn.gnn_model import DualHeadGATv2
from models.gnn.decision_policy import apply_physical_guardrails

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
        self.feature_contract_version: Optional[str] = None
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
        contract_path = self.scaler_path.parent / "feature_contract.json"
        if contract_path.exists():
            try:
                with open(contract_path, "r", encoding="utf-8") as f:
                    self.feature_contract_version = json.load(f).get("version")
            except Exception as e:
                logger.warning(f"Gagal membaca feature contract: {e}")

    def _load_model(self):
        """Memuat bobot checkpoint model."""
        if not self.weights_path.exists():
            logger.warning(f"GNN weights belum ditemukan di: {self.weights_path}. Model belum dilatih.")
            self.is_ready = False
            return

        try:
            checkpoint = torch.load(self.weights_path, map_location=self.device)
            checkpoint_version = checkpoint.get("feature_pipeline_version")
            checkpoint_features = checkpoint.get("feature_columns")
            if checkpoint_version != FEATURE_PIPELINE_VERSION:
                logger.warning(
                    "GNN checkpoint tidak cocok dengan preprocessing live: "
                    f"checkpoint={checkpoint_version!r}, runtime={FEATURE_PIPELINE_VERSION!r}. "
                    "Latih ulang GNN sebelum mengaktifkan inference."
                )
                self.is_ready = False
                return
            if self.feature_contract_version != FEATURE_PIPELINE_VERSION:
                logger.warning(
                    "GNN inference dinonaktifkan: scaler/feature contract belum dihasilkan oleh pipeline terbaru. "
                    "Jalankan feature_engineer.py dan latih ulang GNN."
                )
                self.is_ready = False
                return
            if checkpoint_features != sorted(NORMALIZED_FEATURE_COLS):
                logger.warning("GNN checkpoint memakai urutan/schema fitur yang berbeda; inference dinonaktifkan.")
                self.is_ready = False
                return
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
            test_acc = metrics.get(
                "test_accuracy",
                metrics.get("cluster_anomaly_accuracy", metrics.get("node_accuracy")),
            )
            if test_acc is None:
                logger.success("GNN Model loaded successfully! (test accuracy not recorded in checkpoint)")
            else:
                logger.success(f"GNN Model loaded successfully! (Cluster Test Acc: {test_acc*100:.2f}%)")
        except Exception as e:
            logger.error(f"Gagal memuat model GNN: {e}")
            self.is_ready = False

    def build_feature_tensor_from_metrics(self, current_metrics_map: Dict[str, Dict[str, float]]) -> torch.Tensor:
        """
        Mengonstruksi tensor input [11, F] dari snapshot metrik real-time.
        
        Args:
          current_metrics_map: Dict {service_name: {metric_col: float_val}}
          
        Returns:
          Tensor [11, F] ternormalisasi
        """
        num_features = len(NORMALIZED_FEATURE_COLS)
        x_matrix = np.zeros((len(SERVICE_NAMES), num_features), dtype=np.float32)

        # graph_dataset trains on sorted(features_to_use); inference must use
        # exactly the same column ordering or every feature lands in the wrong slot.
        ordered_features = sorted(NORMALIZED_FEATURE_COLS)
        for svc_idx, svc_name in enumerate(SERVICE_NAMES):
            svc_metrics = current_metrics_map.get(svc_name, {})

            for f_idx, feat_col in enumerate(ordered_features):
                base_col = feat_col.replace("_norm", "")
                val = float(svc_metrics.get(feat_col, svc_metrics.get(base_col, 0.0)))

                # Terapkan min-max scaling jika nilai masih raw
                if feat_col not in svc_metrics and base_col in self.scaler_params:
                    p = self.scaler_params[base_col]
                    mn, mx = p.get("min", 0.0), p.get("max", 1.0)
                    if mx > mn:
                        val = (val - mn) / (mx - mn)
                        val = np.clip(val, 0.0, 1.0)
                    else:
                        # Fitur konstan pada data latih (contoh request_rate max=min=0)
                        # harus bernilai 0, bukan nilai mentah yang tak ternormalisasi.
                        val = 0.0

                x_matrix[svc_idx, f_idx] = val

        return torch.tensor(x_matrix, dtype=torch.float32, device=self.device)

    def predict_target(self,
                       target_deployment: str,
                       current_metrics_map: Optional[Dict[str, Dict[str, float]]] = None) -> Optional[MLPrediction]:
        """
        Fungsi utama yang dipanggil oleh Orchestrator Step [2/7].
        
        Args:
          target_deployment  : Nama deployment target (misal: "cartservice")
          current_metrics_map: Peta metrik semua service (opsional)
          
        Returns:
          MLPrediction (Pydantic object)
        """
        target_svc = extract_service_name(target_deployment)
        if target_svc not in SERVICE_TO_IDX:
            logger.warning(f"GNN inference skipped: unknown service '{target_deployment}'")
            return None
        target_idx = SERVICE_TO_IDX[target_svc]

        # Fallback jika model belum siap / belum dilatih
        if not self.is_ready or self.model is None:
            logger.warning("GNN prediction skipped: model/checkpoint is not ready")
            return None

        start_time = time.time()

        # Model dilatih dengan snapshot lengkap 11 service × F fitur. Jangan
        # menjalankan GNN dengan node/kolom kosong yang akan tampak seperti nilai 0.
        if not current_metrics_map:
            logger.warning("GNN inference skipped: no live metrics snapshot was provided")
            return None

        expected_scaler_features = {feature.removesuffix("_norm") for feature in NORMALIZED_FEATURE_COLS}
        missing_scaler_features = sorted(expected_scaler_features - set(self.scaler_params))
        if missing_scaler_features:
            logger.warning(
                "GNN inference skipped: scaler schema incomplete; "
                f"missing={missing_scaler_features}"
            )
            return None

        # A min-max constant feature carries no information in the trained model.
        # Do not silently map a newly non-zero live value (e.g. request_rate) to 0.
        for service in SERVICE_NAMES:
            values = current_metrics_map[service]
            for feature in expected_scaler_features:
                if feature in values or f"{feature}_norm" in values:
                    continue
                params = self.scaler_params[feature]
                mn = float(params.get("min", 0.0))
                mx = float(params.get("max", 1.0))
                if mx <= mn and abs(float(values[feature]) - mn) > 1e-8:
                    logger.warning(
                        "GNN inference skipped: live feature falls outside a constant training column; "
                        f"{service}.{feature}={values[feature]} while training constant={mn}. "
                        "Collect representative telemetry and retrain the model."
                    )
                    return None

        ordered_features = sorted(NORMALIZED_FEATURE_COLS)
        missing_services = [svc for svc in SERVICE_NAMES if svc not in current_metrics_map]
        missing_features = {
            svc: [
                feature for feature in ordered_features
                if feature not in current_metrics_map.get(svc, {})
                and feature.removesuffix("_norm") not in current_metrics_map.get(svc, {})
            ]
            for svc in SERVICE_NAMES if svc in current_metrics_map
        }
        incomplete_services = [svc for svc, features in missing_features.items() if features]
        malformed_features = []
        for svc in SERVICE_NAMES:
            values = current_metrics_map.get(svc, {})
            for feature in ordered_features:
                key = feature if feature in values else feature.removesuffix("_norm")
                if key not in values:
                    continue
                try:
                    if not np.isfinite(float(values[key])):
                        malformed_features.append(f"{svc}.{key}")
                except (TypeError, ValueError):
                    malformed_features.append(f"{svc}.{key}")
        if missing_services or incomplete_services or malformed_features:
            logger.warning(
                "GNN inference skipped: incomplete snapshot. "
                f"Missing services={missing_services}; "
                f"services with missing features={incomplete_services}; "
                f"invalid values={malformed_features}. "
                f"Expected all 11 services with {len(ordered_features)} model features; using the orchestrator's non-GNN fallback."
            )
            return None

        # Konstruksi input tensor dengan schema lengkap yang sesuai data latih.
        x_tensor = self.build_feature_tensor_from_metrics(current_metrics_map)

        with torch.no_grad():
            node_logits, graph_urgency, attn = self.model(
                x_tensor, self.edge_index, batch=None, return_attention=True
            )
            node_probs = F.softmax(node_logits, dim=-1)

        elapsed_ms = (time.time() - start_time) * 1000

        # Global cluster risk
        cluster_risk = float(graph_urgency.squeeze().item())

        raw_features = x_tensor.cpu().numpy()
        # Use NORMALIZED_FEATURE_COLS directly in contract order.
        _sorted_feats = sorted(NORMALIZED_FEATURE_COLS)
        idx_anomaly = _sorted_feats.index("anomaly_score_raw_norm")
        idx_restart_delta = _sorted_feats.index("restart_delta_norm")
        idx_mem_slope = _sorted_feats.index("memory_slope_12_norm")
        idx_cpu_zscore = _sorted_feats.index("cpu_zscore_pod_norm")
        idx_latency = _sorted_feats.index("request_latency_p95_ms_norm")
        idx_ready = _sorted_feats.index("service_ready_ratio_norm")
        feature_index = {name: idx for idx, name in enumerate(_sorted_feats)}

        # Diagnosis Target Pod (Hierarchical Gated + Physical Guardrails V2)
        target_probs = node_probs[target_idx].cpu().numpy().copy()
        if cluster_risk < 0.50:
            pred_label_id = 0
            confidence = float(1.0 - cluster_risk)
            anomaly_type = AnomalyType.NORMAL
            target_probs[1:] = 0.0
        else:
            target_probs = apply_physical_guardrails(
                target_probs, raw_features[target_idx], feature_index
            )

            pred_label_id = int(np.argmax(target_probs))
            confidence = float(target_probs[pred_label_id])
            anomaly_type = GNN_LABEL_TO_ANOMALY_TYPE.get(pred_label_id, AnomalyType.NORMAL)

        # Fault probabilities distribution
        fault_probs_dict = {
            a_type.value: round(float(target_probs[lbl_id]), 3)
            for lbl_id, a_type in GNN_LABEL_TO_ANOMALY_TYPE.items()
        }

        # Root Cause Analysis V2: Hybrid scoring (GNN + Enriched Local Features)
        gnn_anomaly = 1.0 - node_probs[:, 0].cpu().numpy()
        local_scores = (
            0.20 * raw_features[:, idx_anomaly] +
            0.15 * raw_features[:, idx_cpu_zscore] +
            0.15 * raw_features[:, idx_restart_delta] +
            0.15 * raw_features[:, idx_mem_slope] +
            0.20 * raw_features[:, idx_latency] +
            0.15 * (1.0 - raw_features[:, idx_ready])
        )
        # Hybrid: 25% GNN + 75% lokal (mengatasi graph contamination)
        hybrid_scores = 0.25 * gnn_anomaly + 0.75 * local_scores
        
        # Hitung Top-3 RCA Candidates secara eksplisit untuk dikirim ke Antigravity
        sorted_indices = np.argsort(hybrid_scores)[::-1]
        top3_rca = []
        for rank, s_idx in enumerate(sorted_indices[:3]):
            svc_name = IDX_TO_SERVICE[int(s_idx)]
            score_val = float(hybrid_scores[s_idx])
            top3_rca.append({
                "rank": rank + 1,
                "service": svc_name,
                "score": round(score_val, 3),
                "percentage": f"{score_val:.1%}"
            })

        root_cause_idx = int(sorted_indices[0])
        root_cause_svc = IDX_TO_SERVICE[root_cause_idx]
        root_cause_score = float(hybrid_scores[root_cause_idx])

        # Cascade Risk: Cari pod tetangga yang hybrid score-nya di atas ambang batas (0.35)
        cascade_pods = []
        for idx, svc in enumerate(SERVICE_NAMES):
            if idx != target_idx and hybrid_scores[idx] > 0.35:
                cascade_pods.append(f"{svc} ({hybrid_scores[idx]:.0%})")

        logger.info(f"🧠 GNN Inference [{elapsed_ms:.2f}ms] | Target: {target_svc} -> {anomaly_type.value} "
                    f"({confidence:.0%}) | Cluster Risk: {cluster_risk:.2f} | Root Cause: {root_cause_svc} ({root_cause_score:.0%}) "
                    f"| Top-3 RCA: {[r['service'] + ' (' + r['percentage'] + ')' for r in top3_rca]}")

        return MLPrediction(
            risk_score=round(cluster_risk, 3),
            anomaly_type=anomaly_type,
            confidence=round(confidence, 3),
            root_cause_service=root_cause_svc,
            top3_root_causes=top3_rca,
            fault_probabilities=fault_probs_dict,
            time_to_failure_minutes=5.0 if anomaly_type != AnomalyType.NORMAL else None,
            cascade_risk=cascade_pods
        )


if __name__ == "__main__":
    logger.info("Testing GNNPredictor...")
    predictor = GNNPredictor()
    pred = predictor.predict_target("cartservice")
    if pred is None:
        logger.warning(
            "No prediction was produced: this entry point needs a complete live "
            f"11-service × {len(NORMALIZED_FEATURE_COLS)}-feature snapshot. The no-input dummy call is not an accuracy test."
        )
        raise SystemExit(0)
    print(f"\nMLPrediction Result:")
    print(f"  Risk Score   : {pred.risk_score}")
    print(f"  Anomaly Type : {pred.anomaly_type}")
    print(f"  Confidence   : {pred.confidence}")
    print(f"  Cascade Risk : {pred.cascade_risk}")
