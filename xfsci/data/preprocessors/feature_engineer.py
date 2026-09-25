"""
============================================================
XFSCI Feature Engineer - Fase 3D: Preprocessing
============================================================
Menghitung fitur-fitur turunan (derived features) dari
dataset yang sudah di-augmentasi untuk meningkatkan
kemampuan model LSTM dan GNN dalam mendeteksi pola anomali.

Fitur Turunan yang Dihasilkan:
  1. cpu_delta          : Delta cpu_usage antara step (rate of change)
  2. memory_delta       : Delta memory_usage antara step
  3. memory_growth_rate : Laju pertumbuhan memori per menit (MB/min)
  4. cpu_rolling_mean_5 : Rolling mean CPU 5 step (~25 detik)
  5. cpu_rolling_std_5  : Rolling std CPU 5 step (volatility indicator)
  6. mem_rolling_mean_5 : Rolling mean Memory 5 step
  7. restart_delta      : Kenaikan restart count
  8. net_total_bytes    : net_rx + net_tx (total throughput)
  9. net_rx_tx_ratio    : Rasio RX/TX (deteksi traffic anomali)
 10. anomaly_score_raw  : Skor anomali mentah (0-1, deterministik)

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

BASE_DIR = Path(__file__).parent.parent.parent
PROCESSED_DIR = BASE_DIR / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# Kolom numerik dasar (dari metrics_scraper.py)
BASE_NUMERIC_COLS = [
    "cpu_usage", "memory_usage", "memory_usage_percent",
    "pod_restarts", "net_rx_bytes", "net_tx_bytes",
    "request_rate", "error_rate",
]

# Kolom fitur turunan yang akan ditambahkan
DERIVED_COLS = [
    "cpu_delta", "memory_delta", "memory_growth_rate",
    "cpu_rolling_mean_5", "cpu_rolling_std_5", "mem_rolling_mean_5",
    "restart_delta", "net_total_bytes", "net_rx_tx_ratio",
    "anomaly_score_raw",
]

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

        logger.info("  Network features added: net_total_bytes, net_rx_tx_ratio")
        return df

    def add_anomaly_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Hitung skor anomali deterministik (0-1) berdasarkan bobot fitur.
        Digunakan sebagai sinyal awal sebelum model ML dilatih.
        Rumus: weighted sum dari normalized metrics.
        """
        # Normalisasi per-fitur ke [0, 1] secara lokal
        def norm(series):
            mn, mx = series.min(), series.max()
            if mx - mn == 0:
                return pd.Series(np.zeros(len(series)), index=series.index)
            return (series - mn) / (mx - mn)

        score = (
            0.35 * norm(df["cpu_usage"].clip(0, 4)) +
            0.25 * norm(df["memory_usage"].clip(0, 4 * 1024**3)) +
            0.20 * norm(df["restart_delta"].clip(0, 5)) +
            0.10 * norm(df["memory_growth_rate"].clip(0, 100)) +
            0.10 * norm(df["net_total_bytes"].clip(0, 1e8))
        )

        df["anomaly_score_raw"] = score.round(4)
        logger.info("  Anomaly score added: anomaly_score_raw (0-1)")
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
        logger.info("  Feature Engineering Summary:")
        logger.info(f"  Total rows       : {len(df):,}")
        logger.info(f"  Total features   : {len(MODEL_FEATURE_COLS)} base + {len(MODEL_FEATURE_COLS)} norm")
        logger.info(f"  Unique pods      : {df['pod_name'].nunique()}")
        logger.info(f"  Columns          : {list(df.columns)}")
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
        df = self.add_anomaly_score(df)
        df = self.add_label_encoding(df)
        self.compute_scaler_params(df)
        df = self.apply_minmax_normalization(df)
        df = df.sort_values(["pod_name", "timestamp"]).reset_index(drop=True)
        self.print_summary(df)
        out = self.save(df)
        return df, out


def main():
    parser = argparse.ArgumentParser(description="XFSCI Feature Engineer - Fase 3D")
    parser.add_argument("--input",  type=str, default=None, help="Path CSV augmented input")
    args = parser.parse_args()

    logger.info("=" * 55)
    logger.info("  XFSCI Feature Engineer - Fase 3D")
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
