#!/usr/bin/env python3
"""Telemetry-only preflight for the live Prometheus -> GNN feature contract."""

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.pandas_processor import PandasMetricProcessor
from models.gnn.feature_contract import FEATURE_PIPELINE_VERSION, MODEL_FEATURE_COLS
from models.gnn.graph_dataset import SERVICE_NAMES


def main() -> int:
    processor = PandasMetricProcessor()
    print(f"Feature pipeline: {FEATURE_PIPELINE_VERSION}")
    try:
        snapshot, status = processor.get_gnn_feature_snapshot(preflight=True)
    except Exception as exc:
        print(f"BLOCKED: could not build live feature snapshot: {exc}")
        print(f"Snapshot status: {processor.last_gnn_snapshot_status}")
        return 2
    print(f"Snapshot status: {status}")

    if not status.get("available"):
        print("BLOCKED: live feature snapshot is incomplete; no GNN inference was run.")
        return 2
    if len(snapshot) != len(SERVICE_NAMES):
        print(f"BLOCKED: service count {len(snapshot)}/{len(SERVICE_NAMES)}.")
        return 4
    if any(set(metrics) != set(MODEL_FEATURE_COLS) for metrics in snapshot.values()):
        print("BLOCKED: one or more services do not match the current raw feature schema.")
        return 5
    invalid = [
        f"{service}.{feature}"
        for service, metrics in snapshot.items()
        for feature, value in metrics.items()
        if not np.isfinite(float(value))
    ]
    if invalid:
        print(f"BLOCKED: non-finite telemetry: {invalid}")
        return 6
    for service in SERVICE_NAMES:
        print(
            f"{service:28s} features={len(snapshot[service])} "
            f"ready={snapshot[service]['service_ready_ratio']:.3f} "
            f"latency_p95_ms={snapshot[service]['request_latency_p95_ms']:.2f}"
        )
    print(
        f"PASS: telemetry only; {len(snapshot)} services × {len(MODEL_FEATURE_COLS)} raw features. "
        "No checkpoint was loaded and no inference was run."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
