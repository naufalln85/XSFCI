#!/usr/bin/env python3
"""Read-only preflight for live Prometheus -> 11 x 21 GNN inference."""

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.pandas_processor import PandasMetricProcessor
from models.gnn.feature_contract import FEATURE_PIPELINE_VERSION, MODEL_FEATURE_COLS
from models.gnn.graph_dataset import SERVICE_NAMES
from models.gnn.gnn_predictor import GNNPredictor


def main() -> int:
    processor = PandasMetricProcessor()
    print(f"Feature pipeline: {FEATURE_PIPELINE_VERSION}")
    try:
        snapshot, status = processor.get_gnn_feature_snapshot()
    except Exception as exc:
        print(f"BLOCKED: could not build live feature snapshot: {exc}")
        print(f"Snapshot status: {processor.last_gnn_snapshot_status}")
        return 2
    print(f"Snapshot status: {status}")

    if not status.get("available"):
        print("BLOCKED: live feature snapshot is incomplete; no GNN inference was run.")
        return 2

    predictor = GNNPredictor()
    if not predictor.is_ready:
        print("BLOCKED: checkpoint/scaler is missing or incompatible; retrain the GNN first.")
        return 3

    tensor = predictor.build_feature_tensor_from_metrics(snapshot).detach().cpu().numpy()
    if tensor.shape != (len(SERVICE_NAMES), len(MODEL_FEATURE_COLS)):
        print(f"BLOCKED: wrong model input shape {tensor.shape}.")
        return 4
    if not np.isfinite(tensor).all():
        print("BLOCKED: model input contains NaN or infinity.")
        return 5

    print(f"Input tensor: shape={tensor.shape}, min={tensor.min():.4f}, max={tensor.max():.4f}")
    print("Per-feature normalized range across the 11 services:")
    for index, feature in enumerate(sorted(f"{name}_norm" for name in MODEL_FEATURE_COLS)):
        values = tensor[:, index]
        print(f"  {feature:32s} min={values.min():.4f} max={values.max():.4f} nonzero={np.count_nonzero(values)}/11")

    prediction = predictor.predict_target("frontend", current_metrics_map=snapshot)
    if prediction is None:
        print("BLOCKED: predictor rejected the snapshot/checkpoint.")
        return 6
    print(
        "Read-only inference: "
        f"anomaly={prediction.anomaly_type.value}, risk={prediction.risk_score:.3f}, "
        f"confidence={prediction.confidence:.3f}, root_cause={prediction.root_cause_service}"
    )
    print("PASS: full live snapshot was accepted. This is a schema/telemetry check, not an accuracy guarantee.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
