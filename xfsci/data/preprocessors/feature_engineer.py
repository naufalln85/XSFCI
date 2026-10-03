"""
============================================================
XFSCI Feature Engineer - Fase 3D: Preprocessing (V2 - SOTA)
============================================================
Menghitung fitur-fitur turunan (derived features) dari
dataset yang sudah di-augmentasi untuk meningkatkan
kemampuan model LSTM dan GNN dalam mendeteksi pola anomali.

Peningkatan V2 (SOTA Edition):
  - Per-pod Z-score kausal berbasis 15 menit sebelumnya (tanpa data masa depan)
  - Memory Slope (rolling regression 12 step = 60 detik)
  - Network Asymmetry (|RX-TX| / total)
  - CPU Z-Score per Pod (deviasi dari baseline tiap pod)
  - Anomaly Score berbatas tetap agar training/live memakai rumus yang sama

Fitur Turunan yang Dihasilkan:
   1. cpu_delta           : Delta cpu_usage antara step (rate of change)
   2. memory_delta        : Delta memory_usage antara step
   3. memory_growth_rate  : Laju pertumbuhan memori per menit (MB/min)
   4. cpu_rolling_mean_5  : Rolling mean CPU 5 step (~25 detik)
   5. cpu_rolling_std_5   : Rolling std CPU 5 step (volatility indicator)
   6. mem_rolling_mean_5  : Rolling mean Memory 5 step
   7. restart_delta       : Kenaikan restart count
   8. net_total_bytes     : net_rx + net_tx (total throughput)
   9. net_rx_tx_ratio     : Rasio RX/TX (deteksi traffic anomali)
  10. anomaly_score_raw   : Skor anomali mentah (0-1, deterministik)
  11. memory_slope_12     : Rolling OLS slope memory 12 step (60 detik)
  12. cpu_zscore_pod      : Z-score CPU per pod (deviasi dari baseline)
  13. net_asymmetry       : |RX-TX| / (RX+TX+eps) untuk deteksi traffic anomali

Output:
  data/processed/dataset_ready.csv  <-- File final untuk pelatihan model
  data/processed/scaler_params.json <-- Parameter normalisasi (min-max)

Cara pakai:
  python data/preprocessors/feature_engineer.py
============================================================
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.gnn.feature_contract import (
    APP_SPAN_ZERO_SERVICES,
    BASE_METRIC_COLS,
    DERIVED_FEATURE_COLS,
    FEATURE_PIPELINE_VERSION,
    MODEL_FEATURE_COLS,
)

BASE_DIR = PROJECT_ROOT
PROCESSED_DIR = BASE_DIR / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# Kolom numerik dasar (dari metrics_scraper.py)
BASE_NUMERIC_COLS = BASE_METRIC_COLS

# Kolom fitur turunan yang akan ditambahkan
DERIVED_COLS = DERIVED_FEATURE_COLS

# Kolom final untuk model (input features)
MODEL_FEATURE_COLS = BASE_NUMERIC_COLS + DERIVED_COLS

# Label mapping untuk model klasifikasi (integer)
LABEL_MAP = {
    "NORMAL":                0,
    "FAULT_CPU_STRESS":      1,
    "FAULT_MEMORY_LEAK":     2,
    "FAULT_POD_CRASH":       3,
    "FAULT_NETWORK_LATENCY": 4,
}

SCRAPE_INTERVAL_SECONDS = 5


class FeatureEngineer:
    def __init__(self):
        self.scaler_params = {}

    def load(self, csv_path: Path) -> pd.DataFrame:
        logger.info(f"Loading: {csv_path.name}")
        df = pd.read_csv(csv_path)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        if "error_rate" in df.columns:
            # Compatibilitas per baris dengan CSV lama (persen) yang mungkin
            # sudah digabung dengan data sintetis baru (fraksi 0-1).
            error_rate = pd.to_numeric(df["error_rate"], errors="coerce")
            legacy_percent_rows = error_rate > 1.0
            if legacy_percent_rows.any():
                df.loc[legacy_percent_rows, "error_rate"] = error_rate.loc[legacy_percent_rows] / 100.0
                logger.warning(
                    f"Converted {int(legacy_percent_rows.sum())} legacy error_rate rows from percent to fraction."
                )
        logger.info(f"  Rows: {len(df):,} | Pods: {df['pod_name'].nunique()}")
        return df

    def add_delta_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Hitung perubahan (delta) metrik antara dua timestep per pod."""
        df = df.sort_values(["pod_name", "timestamp"]).copy()

        # Delta CPU: selisih cpu_usage antara step sekarang dan sebelumnya
        df["cpu_delta"] = df.groupby("pod_name")["cpu_usage"].diff().fillna(0)

        # Delta Memory (bytes)
        df["memory_delta"] = df.groupby("pod_name")["memory_usage"].diff().fillna(0)

        # Memory Growth Rate (MB per menit)
        df["memory_growth_rate"] = (
            df["memory_delta"] / (1024**2) / (SCRAPE_INTERVAL_SECONDS / 60)
        ).round(4)

        # Restart delta (kenaikan restart count)
        df["restart_delta"] = df.groupby("pod_name")["pod_restarts"].diff().clip(0).fillna(0).astype(int)

        logger.info("  Delta features added: cpu_delta, memory_delta, memory_growth_rate, restart_delta")
        return df

    def add_rolling_features(self, df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
        """
        Hitung rolling statistics per pod.
        window=5 berarti 5 timestep = 25 detik (interval 5s).
        """
        df = df.sort_values(["pod_name", "timestamp"]).copy()

        # Rolling mean CPU (trend jangka pendek)
        df["cpu_rolling_mean_5"] = (
            df.groupby("pod_name")["cpu_usage"]
              .transform(lambda x: x.rolling(window, min_periods=1).mean())
              .round(6)
        )

        # Rolling std CPU (volatility / instabilitas)
        df["cpu_rolling_std_5"] = (
            df.groupby("pod_name")["cpu_usage"]
              .transform(lambda x: x.rolling(window, min_periods=1).std().fillna(0))
              .round(6)
        )

        # Rolling mean Memory
        df["mem_rolling_mean_5"] = (
            df.groupby("pod_name")["memory_usage"]
              .transform(lambda x: x.rolling(window, min_periods=1).mean())
              .round(2)
        )

        logger.info(f"  Rolling features added (window={window}): cpu_rolling_mean_5, cpu_rolling_std_5, mem_rolling_mean_5")
        return df

    def add_network_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Tambahkan fitur analisis lalu lintas jaringan."""
        # Total throughput
        df["net_total_bytes"] = (df["net_rx_bytes"] + df["net_tx_bytes"]).round(2)

        # Rasio RX/TX (normal ~1.0; anomali bisa sangat tinggi atau rendah)
        df["net_rx_tx_ratio"] = (
            df["net_rx_bytes"] / (df["net_tx_bytes"] + 1e-10)  # Hindari div by zero
        ).round(4)
        # Clip rasio ke nilai wajar [0, 100]
        df["net_rx_tx_ratio"] = df["net_rx_tx_ratio"].clip(0, 100)

        # V2: Network Asymmetry — |RX - TX| / (RX + TX + eps)
        # Nilai mendekati 0 = traffic simetris (normal)
        # Nilai mendekati 1 = traffic sangat tidak simetris (anomali jaringan)
        rx = df["net_rx_bytes"]
        tx = df["net_tx_bytes"]
        df["net_asymmetry"] = (
            (rx - tx).abs() / (rx + tx + 1e-10)
        ).clip(0, 1).round(6)

        logger.info("  Network features added: net_total_bytes, net_rx_tx_ratio, net_asymmetry")
        return df

    def add_memory_slope(self, df: pd.DataFrame, window: int = 12) -> pd.DataFrame:
        """
        Hitung rolling OLS slope memory per pod selama `window` timesteps.
        window=12 berarti 12 * 5s = 60 detik.

        Memory slope POSITIF → memory sedang naik (indikator memory leak)
        Memory slope ~0 → memory stabil (normal)
        Memory slope NEGATIF → memory turun (recovery)

        Ini KRITIS karena memory_delta hanya melihat selisih 1-step,
        sementara memory leak memiliki TREND naik yang gradual.
        """
        df = df.sort_values(["pod_name", "timestamp"]).copy()

        def rolling_slope(series, w):
            """Hitung slope linear (least-squares) pada rolling window."""
            result = np.zeros(len(series))
            x = np.arange(w, dtype=np.float64)
            x_mean = x.mean()
            x_var = ((x - x_mean) ** 2).sum()

            vals = series.values.astype(np.float64)
            for i in range(len(vals)):
                if i < w - 1:
                    # Window belum penuh, gunakan window parsial
                    actual_w = i + 1
                    if actual_w < 2:
                        result[i] = 0.0
                        continue
                    x_partial = np.arange(actual_w, dtype=np.float64)
                    y_partial = vals[i - actual_w + 1:i + 1]
                    x_mean_p = x_partial.mean()
                    y_mean_p = y_partial.mean()
                    x_var_p = ((x_partial - x_mean_p) ** 2).sum()
                    if x_var_p == 0:
                        result[i] = 0.0
                    else:
                        result[i] = ((x_partial - x_mean_p) * (y_partial - y_mean_p)).sum() / x_var_p
                else:
                    y_window = vals[i - w + 1:i + 1]
                    y_mean = y_window.mean()
                    if x_var == 0:
                        result[i] = 0.0
                    else:
                        result[i] = ((x - x_mean) * (y_window - y_mean)).sum() / x_var

            return pd.Series(result, index=series.index)

        # Normalize memory to MB sebelum hitung slope (agar unit lebih mudah diinterpretasi)
        df["_mem_mb"] = df["memory_usage"] / (1024**2)
        df["memory_slope_12"] = (
            df.groupby("pod_name")["_mem_mb"]
              .transform(lambda x: rolling_slope(x, window))
              .round(4)
        )
        df.drop(columns=["_mem_mb"], inplace=True)

        logger.info(f"  Memory slope added (window={window}): memory_slope_12 (MB/step)")
        return df

    def add_cpu_zscore_per_pod(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Hitung z-score CPU per pod: (cpu_i - mean_pod) / std_pod.

        Ini mengatasi masalah global normalization dimana:
        - Frontend secara normal CPU 0.15-0.25 → terlihat "tinggi" secara global
        - redis-cart secara normal CPU 0.01 → terlihat "rendah" secara global
        
        Dengan z-score per-pod, anomali diukur berdasarkan
        deviasi dari BASELINE masing-masing pod.
        """
        df = df.sort_values(["pod_name", "timestamp"]).copy()

        # Baseline kausal: gunakan hanya 180 sampel sebelumnya (15 menit),
        # supaya rumus training dapat diulang saat inferensi live tanpa melihat masa depan.
        def causal_zscore(series):
            history = series.shift(1)
            rolling = history.rolling(window=180, min_periods=2)
            mean = rolling.mean()
            std = rolling.std().replace(0, np.nan)
            return ((series - mean) / std).replace([np.inf, -np.inf], np.nan).fillna(0).clip(-5, 5)

        df["cpu_zscore_pod"] = df.groupby("pod_name")["cpu_usage"].transform(causal_zscore).round(4)

        logger.info("  CPU z-score per pod added: cpu_zscore_pod")
        return df

    def add_anomaly_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Hitung skor anomali deterministik (0-1) berdasarkan bobot fitur.
        V2: Menggunakan per-pod z-score dan fitur baru (slope, asymmetry).

        Komponen scoring:
          - cpu_zscore_pod: pod yang CPU-nya jauh dari baseline sendiri
          - memory_slope_12: pod yang memory-nya sedang naik signifikan
          - restart_delta: pod yang baru saja restart
          - net_asymmetry: traffic yang tidak simetris
          - error_rate: fraction of server spans with OpenTelemetry Error status
        """
        # Rumus berbatas tetap agar hasilnya identik di data training dan live.
        # Normalisasi min-max final tetap memakai scaler yang disimpan.
        score = (
            0.25 * (df["cpu_zscore_pod"].abs().clip(0, 5) / 5) +
            0.20 * (df["memory_slope_12"].clip(0, 100) / 100) +
            0.20 * (df["restart_delta"].clip(0, 5) / 5) +
            0.15 * df["net_asymmetry"].clip(0, 1) +
            0.10 * df["error_rate"].clip(0, 1) +
            0.10 * (df["memory_growth_rate"].clip(0, 100) / 100)
        )

        df["anomaly_score_raw"] = score.clip(0, 1).round(4)
        logger.info("  Anomaly score V2 added: anomaly_score_raw (0-1, z-score based)")
        return df

    def add_label_encoding(self, df: pd.DataFrame) -> pd.DataFrame:
        """Tambahkan kolom label_id (integer) untuk pelatihan model."""
        df["label_id"] = df["label"].map(LABEL_MAP).fillna(-1).astype(int)
        unknown = (df["label_id"] == -1).sum()
        if unknown > 0:
            logger.warning(f"  {unknown} rows with unknown label (label_id=-1)")
        logger.info(f"  Label encoding: {LABEL_MAP}")
        return df

    def compute_scaler_params(self, df: pd.DataFrame) -> dict:
        """
        Hitung parameter min-max normalisasi untuk setiap kolom fitur.
        Disimpan ke JSON agar bisa digunakan oleh model saat inference.
        """
        params = {}
        for col in MODEL_FEATURE_COLS:
            if col in df.columns:
                params[col] = {
                    "min": float(df[col].min()),
                    "max": float(df[col].max()),
                    "mean": float(df[col].mean()),
                    "std": float(df[col].std()),
                }
        self.scaler_params = params
        return params

    def apply_minmax_normalization(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Terapkan min-max normalisasi ke semua kolom fitur model.
        Nilai hasil: [0, 1].
        Kolom asli tetap disimpan dengan suffix _raw.
        """
        for col in MODEL_FEATURE_COLS:
            if col not in df.columns:
                continue
            p = self.scaler_params[col]
            mn, mx = p["min"], p["max"]
            if mx - mn == 0:
                df[f"{col}_norm"] = 0.0
            else:
                df[f"{col}_norm"] = ((df[col] - mn) / (mx - mn)).clip(0, 1).round(6)

        logger.info(f"  Min-max normalization applied to {len(MODEL_FEATURE_COLS)} features")
        return df

    def print_summary(self, df: pd.DataFrame):
        logger.info("")
        logger.info("=" * 55)
        logger.info("  Feature Engineering Summary (V2 SOTA):")
        logger.info(f"  Total rows       : {len(df):,}")
        logger.info(f"  Total features   : {len(MODEL_FEATURE_COLS)} base + {len(MODEL_FEATURE_COLS)} norm")
        logger.info(f"  Unique pods      : {df['pod_name'].nunique()}")
        logger.info(f"  Columns          : {list(df.columns)}")
        logger.info("")
        logger.info("  V2 New Features:")
        for col in ["memory_slope_12", "cpu_zscore_pod", "net_asymmetry"]:
            if col in df.columns:
                logger.info(f"    {col:25s} : min={df[col].min():.4f} | max={df[col].max():.4f} | mean={df[col].mean():.4f}")
        logger.info("")
        logger.info("  Label distribution (final):")
        dist = df["label"].value_counts()
        total = len(df)
        for label, count in dist.items():
            pct = count / total * 100
            bar = "=" * int(pct / 3)
            logger.info(f"    {label:<30} {count:>5} ({pct:5.1f}%) {bar}")
        logger.info("=" * 55)

    def save(self, df: pd.DataFrame) -> Path:
        # File utama dataset siap latih
        output_path = PROCESSED_DIR / "dataset_ready.csv"
        df.to_csv(output_path, index=False)
        size_kb = output_path.stat().st_size / 1024
        logger.success(f"Dataset saved: dataset_ready.csv ({len(df):,} rows, {size_kb:.1f} KB)")

        # Simpan scaler params
        scaler_path = PROCESSED_DIR / "scaler_params.json"
        scaler_path.write_text(json.dumps(self.scaler_params, indent=2))
        logger.success(f"Scaler params saved: scaler_params.json")

        contract_path = PROCESSED_DIR / "feature_contract.json"
        contract_path.write_text(json.dumps({
            "version": FEATURE_PIPELINE_VERSION,
            "features": MODEL_FEATURE_COLS,
            "normalization": "minmax_clip_0_1",
            "request_rate_source": "otel_span_metrics_server_calls_per_second",
            "error_rate_source": "otel_span_metrics_status_error_fraction",
            "app_metrics_zero_for_services": list(APP_SPAN_ZERO_SERVICES),
            "error_rate_unit": "fraction_0_1",
            "cpu_usage_unit": "cores",
            "memory_usage_unit": "bytes",
        }, indent=2))
        logger.success("Feature contract saved: feature_contract.json")

        # Simpan label map
        label_map_path = PROCESSED_DIR / "label_map.json"
        label_map_path.write_text(json.dumps(LABEL_MAP, indent=2))
        logger.success(f"Label map saved: label_map.json")

        return output_path

    def run(self, csv_path: Path):
        df = self.load(csv_path)
        df = self.add_delta_features(df)
        df = self.add_rolling_features(df, window=5)
        df = self.add_network_features(df)
        df = self.add_memory_slope(df, window=12)
        df = self.add_cpu_zscore_per_pod(df)
        df = self.add_anomaly_score(df)
        df = self.add_label_encoding(df)
        self.compute_scaler_params(df)
        df = self.apply_minmax_normalization(df)
        df = df.sort_values(["pod_name", "timestamp"]).reset_index(drop=True)
        self.print_summary(df)
        out = self.save(df)
        return df, out


def main():
    parser = argparse.ArgumentParser(description="XFSCI Feature Engineer - Fase 3D (V2 SOTA)")
    parser.add_argument("--input",  type=str, default=None, help="Path CSV augmented input")
    args = parser.parse_args()

    logger.info("=" * 55)
    logger.info("  XFSCI Feature Engineer - Fase 3D (V2 SOTA)")
    logger.info("=" * 55)

    # Pilih file input: augmented > cleaned > labeled (prioritas)
    if args.input:
        csv_path = Path(args.input)
    else:
        for pattern in ["augmented_*.csv", "cleaned_*.csv", "labeled_*.csv"]:
            files = sorted(
                PROCESSED_DIR.glob(pattern),
                key=lambda p: p.stat().st_mtime, reverse=True
            )
            if files:
                csv_path = files[0]
                logger.info(f"Using: {csv_path.name}")
                break
        else:
            logger.error("Tidak ada file processed. Jalankan pipeline dari data_labeler.py.")
            sys.exit(1)

    engineer = FeatureEngineer()
    df, out = engineer.run(csv_path)

    logger.success("Feature engineering selesai!")
    logger.info("")
    logger.info("Dataset siap untuk pelatihan model AI:")
    logger.info(f"  Input model : {out}")
    logger.info(f"  Scaler      : {PROCESSED_DIR}/scaler_params.json")
    logger.info(f"  Label map   : {PROCESSED_DIR}/label_map.json")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. python knowledge_base/rag_indexer.py")
    logger.info("  2. python agent/orchestrator.py --test")


if __name__ == "__main__":
    main()
