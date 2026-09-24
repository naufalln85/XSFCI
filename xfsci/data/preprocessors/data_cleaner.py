"""
============================================================
XFSCI Data Cleaner - Fase 3B: Preprocessing
============================================================
Membersihkan dataset berlabel dari data_labeler.py:
  1. Hapus baris duplikat
  2. Handle missing values (NaN/Inf)
  3. Standarisasi interval waktu 5 detik
  4. Filter pod system (prometheus, kube-system)
  5. Tambah kolom pod_service (nama service tanpa suffix)
  6. Reset kolom label fault-pod ke level sistem

Cara pakai:
  python data/preprocessors/data_cleaner.py
============================================================
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from loguru import logger

BASE_DIR = Path(__file__).parent.parent.parent
PROCESSED_DIR = BASE_DIR / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# Pod sistem yang harus difilter keluar
SYSTEM_POD_PREFIXES = [
    "prometheus", "grafana", "loki", "promtail",
    "alertmanager", "kube-proxy", "coredns",
    "local-path", "flannel", "metrics-server",
    "fault-cpu", "fault-memory", "fault-network", "fault-pod",
]


def is_system_pod(pod_name: str) -> bool:
    """Return True jika pod adalah pod sistem / fault injector (bukan workload)."""
    pod_lower = pod_name.lower()
    return any(pod_lower.startswith(p) for p in SYSTEM_POD_PREFIXES)


def get_service_name(pod_name: str) -> str:
    """Ekstrak nama service dari nama pod lengkap."""
    parts = pod_name.split("-")
    if len(parts) >= 3:
        return "-".join(parts[:-2])
    return pod_name


class XFSCIDataCleaner:
    def __init__(self):
        self.stats = {}

    def load(self, csv_path: Path) -> pd.DataFrame:
        logger.info(f"Loading: {csv_path.name}")
        df = pd.read_csv(csv_path)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        self.stats["raw_rows"] = len(df)
        logger.info(f"  Raw rows: {len(df):,}")
        return df

    def filter_system_pods(self, df: pd.DataFrame) -> pd.DataFrame:
        """Hapus pod sistem dan pod fault injector dari dataset."""
        before = len(df)
        mask = ~df["pod_name"].apply(is_system_pod)
        df = df[mask].copy()
        removed = before - len(df)
        logger.info(f"  System pods removed: {removed:,} rows (kept {len(df):,})")
        self.stats["system_pods_removed"] = removed
        return df

    def add_service_column(self, df: pd.DataFrame) -> pd.DataFrame:
        """Tambahkan kolom pod_service untuk identifikasi service."""
        df["pod_service"] = df["pod_name"].apply(get_service_name)
        return df

    def remove_duplicates(self, df: pd.DataFrame) -> pd.DataFrame:
        """Hapus baris duplikat (timestamp + pod_name yang sama)."""
        before = len(df)
        df = df.drop_duplicates(subset=["timestamp", "pod_name"]).copy()
        removed = before - len(df)
        if removed > 0:
            logger.info(f"  Duplicates removed: {removed:,}")
        self.stats["duplicates_removed"] = removed
        return df

    def handle_missing_values(self, df: pd.DataFrame) -> pd.DataFrame:
        """Handle NaN dan infinite values."""
        numeric_cols = [
            "cpu_usage", "memory_usage", "memory_usage_percent",
            "pod_restarts", "net_rx_bytes", "net_tx_bytes",
            "request_rate", "error_rate",
        ]

        # Ganti infinite dengan NaN dulu
        df[numeric_cols] = df[numeric_cols].replace([np.inf, -np.inf], np.nan)

        nan_before = df[numeric_cols].isna().sum().sum()

        # Isi NaN dengan 0 untuk kolom counter
        zero_fill_cols = ["pod_restarts", "request_rate", "error_rate"]
        df[zero_fill_cols] = df[zero_fill_cols].fillna(0)

        # Isi NaN untuk kolom resource dengan forward-fill per pod
        resource_cols = [c for c in numeric_cols if c not in zero_fill_cols]
        df[resource_cols] = (
            df.sort_values("timestamp")
              .groupby("pod_name")[resource_cols]
              .transform(lambda x: x.ffill().bfill().fillna(0))
        )

        nan_after = df[numeric_cols].isna().sum().sum()
        logger.info(f"  NaN values fixed: {nan_before} -> {nan_after}")
        self.stats["nan_fixed"] = int(nan_before)
        return df

    def clip_outliers(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Clip nilai ekstrim di luar batas fisik:
          - cpu_usage: 0 - 16 cores (total cluster 4 VMs * 4 cores)
          - memory_usage: 0 - 4 GB per pod
          - net_rx/tx: 0 - 1 GB/s
        """
        df["cpu_usage"] = df["cpu_usage"].clip(0, 16.0)
        df["memory_usage"] = df["memory_usage"].clip(0, 4 * 1024**3)
        df["memory_usage_percent"] = df["memory_usage_percent"].clip(0, 100)
        df["net_rx_bytes"] = df["net_rx_bytes"].clip(0, 1e9)
        df["net_tx_bytes"] = df["net_tx_bytes"].clip(0, 1e9)
        df["request_rate"] = df["request_rate"].clip(0, 1e6)
        df["error_rate"] = df["error_rate"].clip(0, 100)
        df["pod_restarts"] = df["pod_restarts"].clip(0, 100)
        return df

    def sort_and_reset(self, df: pd.DataFrame) -> pd.DataFrame:
        """Urutkan berdasarkan timestamp dan pod_name, reset index."""
        df = df.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)
        return df

    def validate(self, df: pd.DataFrame):
        """Validasi akhir sebelum simpan."""
        numeric_cols = [
            "cpu_usage", "memory_usage", "memory_usage_percent",
            "pod_restarts", "net_rx_bytes", "net_tx_bytes",
            "request_rate", "error_rate",
        ]
        nan_count = df[numeric_cols].isna().sum().sum()
        inf_count = np.isinf(df[numeric_cols].values).sum()
        assert nan_count == 0, f"Masih ada {nan_count} NaN!"
        assert inf_count == 0, f"Masih ada {inf_count} Inf!"
        logger.success("  Validation passed: No NaN/Inf remaining")

    def print_summary(self, df: pd.DataFrame):
        logger.info("")
        logger.info("=" * 55)
        logger.info("  Data Cleaning Summary:")
        logger.info(f"  Raw rows       : {self.stats.get('raw_rows', 0):,}")
        logger.info(f"  System removed : {self.stats.get('system_pods_removed', 0):,}")
        logger.info(f"  Duplicates rm  : {self.stats.get('duplicates_removed', 0):,}")
        logger.info(f"  NaN fixed      : {self.stats.get('nan_fixed', 0):,}")
        logger.info(f"  Final rows     : {len(df):,}")
        logger.info(f"  Unique pods    : {df['pod_name'].nunique()}")
        logger.info(f"  Unique services: {df['pod_service'].nunique()}")
        logger.info(f"  Time range     : {df['timestamp'].min()} -> {df['timestamp'].max()}")
        logger.info("")
        logger.info("  Label distribution after cleaning:")
        dist = df["label"].value_counts()
        total = len(df)
        for label, count in dist.items():
            pct = count / total * 100
            logger.info(f"    {label:<30} {count:>5} ({pct:5.1f}%)")
        logger.info("=" * 55)

    def save(self, df: pd.DataFrame, output_path: Path = None) -> Path:
        if output_path is None:
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = PROCESSED_DIR / f"cleaned_{ts_str}.csv"
        df.to_csv(output_path, index=False)
        size_kb = output_path.stat().st_size / 1024
        logger.success(f"Saved: {output_path.name} ({size_kb:.1f} KB)")
        return output_path

    def run(self, csv_path: Path, output_path: Path = None):
        df = self.load(csv_path)
        df = self.filter_system_pods(df)
        df = self.add_service_column(df)
        df = self.remove_duplicates(df)
        df = self.handle_missing_values(df)
        df = self.clip_outliers(df)
        df = self.sort_and_reset(df)
        self.validate(df)
        self.print_summary(df)
        out = self.save(df, output_path)
        return df, out


def main():
    parser = argparse.ArgumentParser(description="XFSCI Data Cleaner - Fase 3B")
    parser.add_argument("--input",  type=str, default=None, help="Path CSV labeled input")
    parser.add_argument("--output", type=str, default=None, help="Path CSV output")
    args = parser.parse_args()

    logger.info("=" * 55)
    logger.info("  XFSCI Data Cleaner - Fase 3B")
    logger.info("=" * 55)

    # Pilih file input (labeled_*.csv terbaru)
    if args.input:
        csv_path = Path(args.input)
    else:
        files = sorted(PROCESSED_DIR.glob("labeled_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            logger.error("Tidak ada labeled_*.csv. Jalankan data_labeler.py terlebih dahulu.")
            sys.exit(1)
        csv_path = files[0]
        logger.info(f"Using: {csv_path.name}")

    output_path = Path(args.output) if args.output else None
    cleaner = XFSCIDataCleaner()
    df, out = cleaner.run(csv_path, output_path)

    logger.success("Cleaning selesai!")
    logger.info("Next: python data/preprocessors/synthetic_generator.py")


if __name__ == "__main__":
    main()
