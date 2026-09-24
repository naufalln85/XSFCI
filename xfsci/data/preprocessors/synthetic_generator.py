"""
============================================================
XFSCI Synthetic Data Generator - Fase 3C: Augmentasi
============================================================
Memperkuat dataset dengan data sintetis yang dihasilkan dari
distribusi statistik data real yang sudah dibersihkan.

Strategi augmentasi per fault type:
  FAULT_CPU_STRESS      : Gaussian noise + skew tinggi di cpu_usage
  FAULT_MEMORY_LEAK     : Ramp-up linear di memory_usage (simulasi leak)
  FAULT_POD_CRASH       : Spike pod_restarts + drop net traffic
  FAULT_NETWORK_LATENCY : Spike net_rx/tx + noise tinggi
  NORMAL                : Gaussian noise ringan

Target output: setiap kelas minimal 1000 baris.

Cara pakai:
  python data/preprocessors/synthetic_generator.py
  python data/preprocessors/synthetic_generator.py --target-per-class 1500
============================================================
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from loguru import logger

BASE_DIR = Path(__file__).parent.parent.parent
PROCESSED_DIR = BASE_DIR / "data" / "processed"
SYNTHETIC_DIR = BASE_DIR / "data" / "synthetic"
SYNTHETIC_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

FAULT_LABELS = [
    "NORMAL",
    "FAULT_CPU_STRESS",
    "FAULT_MEMORY_LEAK",
    "FAULT_POD_CRASH",
    "FAULT_NETWORK_LATENCY",
]

NUMERIC_COLS = [
    "cpu_usage", "memory_usage", "memory_usage_percent",
    "pod_restarts", "net_rx_bytes", "net_tx_bytes",
    "request_rate", "error_rate",
]

# Seed untuk reproducibility
np.random.seed(42)


class SyntheticGenerator:
    def __init__(self, target_per_class: int = 1000, noise_factor: float = 0.15):
        self.target_per_class = target_per_class
        self.noise_factor = noise_factor
        self.stats_per_label = {}

    def compute_label_stats(self, df: pd.DataFrame):
        """Hitung statistik (mean, std, min, max) per label dari data real."""
        for label in df["label"].unique():
            subset = df[df["label"] == label][NUMERIC_COLS]
            self.stats_per_label[label] = {
                "mean": subset.mean(),
                "std": subset.std().fillna(0),
                "min": subset.min(),
                "max": subset.max(),
                "count": len(subset),
            }
            logger.info(f"  {label}: {len(subset):,} real rows")

    def _generate_base(self, label: str, n: int) -> pd.DataFrame:
        """
        Generate n baris berdasarkan statistik real label tersebut.
        Ditambahkan noise Gaussian dengan faktor noise_factor.
        """
        stats = self.stats_per_label.get(label)
        if not stats:
            raise ValueError(f"No stats found for label: {label}")

        rows = {}
        for col in NUMERIC_COLS:
            mean = stats["mean"][col]
            std = max(stats["std"][col], mean * 0.05)  # std minimal 5% dari mean
            noise = np.random.normal(0, std * self.noise_factor, n)
            values = np.random.normal(mean, std, n) + noise
            # Clip ke batas fisik realistis
            values = np.clip(values, stats["min"][col], stats["max"][col] * 1.2)
            rows[col] = values

        return pd.DataFrame(rows)

    def generate_cpu_stress(self, n: int) -> pd.DataFrame:
        """
        Simulasi CPU Stress: cpu_usage tinggi (0.5 - 2.0 cores),
        memory stabil, net stabil.
        """
        df = self._generate_base("FAULT_CPU_STRESS", n)
        # Override cpu_usage dengan distribusi bimodal tinggi
        df["cpu_usage"] = np.random.uniform(0.4, 2.0, n)
        df["memory_usage_percent"] = np.clip(
            np.random.normal(30, 5, n), 20, 60
        )
        df["label"] = "FAULT_CPU_STRESS"
        return df

    def generate_memory_leak(self, n: int) -> pd.DataFrame:
        """
        Simulasi Memory Leak: memory_usage naik linear (ramp-up),
        cpu stabil, pod_restarts = 0 (belum OOM).
        """
        df = self._generate_base("FAULT_MEMORY_LEAK", n)
        # Ramp-up linear 256MB -> 1GB
        base_memory = 256 * 1024**2
        peak_memory = 1 * 1024**3
        ramp = np.linspace(base_memory, peak_memory, n)
        noise = np.random.normal(0, 10 * 1024**2, n)  # +-10MB noise
        df["memory_usage"] = np.clip(ramp + noise, 0, 1.2 * 1024**3)
        df["memory_usage_percent"] = df["memory_usage"] / (4 * 1024**3) * 100
        df["pod_restarts"] = 0
        df["label"] = "FAULT_MEMORY_LEAK"
        return df

    def generate_pod_crash(self, n: int) -> pd.DataFrame:
        """
        Simulasi Pod Crash: pod_restarts meningkat step-wise,
        net traffic drop saat pod mati, CPU rendah saat restart.
        """
        df = self._generate_base("FAULT_POD_CRASH", n)
        # Restarts bertahap: 0, 1, 2, 3...
        restarts = np.floor(np.linspace(0, 5, n)).astype(int)
        df["pod_restarts"] = restarts
        # Net traffic drop saat restarts tinggi
        traffic_factor = np.where(restarts > 0, np.random.uniform(0.1, 0.5, n), 1.0)
        df["net_rx_bytes"] = df["net_rx_bytes"] * traffic_factor
        df["net_tx_bytes"] = df["net_tx_bytes"] * traffic_factor
        df["label"] = "FAULT_POD_CRASH"
        return df

    def generate_network_latency(self, n: int) -> pd.DataFrame:
        """
        Simulasi Network Latency: net_rx/tx bytes melonjak
        karena retransmisi dan buffering TCP, cpu stabil.
        """
        df = self._generate_base("FAULT_NETWORK_LATENCY", n)
        # Net traffic tinggi dengan variabilitas besar (jitter)
        base_rx = df["net_rx_bytes"].mean()
        df["net_rx_bytes"] = np.abs(
            np.random.normal(base_rx * 2, base_rx * 0.8, n)
        )
        df["net_tx_bytes"] = np.abs(
            np.random.normal(base_rx * 1.5, base_rx * 0.6, n)
        )
        df["label"] = "FAULT_NETWORK_LATENCY"
        return df

    def generate_normal(self, n: int) -> pd.DataFrame:
        """
        Simulasi kondisi Normal: semua metrik stabil dalam range healthy.
        """
        df = self._generate_base("NORMAL", n)
        df["pod_restarts"] = 0
        df["label"] = "NORMAL"
        return df

    def _add_metadata(self, df: pd.DataFrame, real_df: pd.DataFrame, label: str) -> pd.DataFrame:
        """
        Tambahkan kolom timestamp, pod_name, pod_service yang realistis.
        """
        # Gunakan pod_name yang ada di dataset real untuk label tersebut
        real_pods = real_df[real_df["label"] == label]["pod_name"].unique()
        if len(real_pods) == 0:
            real_pods = real_df["pod_name"].unique()

        n = len(df)
        df["pod_name"] = np.random.choice(real_pods, n)
        df["pod_service"] = df["pod_name"].apply(
            lambda x: "-".join(x.split("-")[:-2]) if len(x.split("-")) >= 3 else x
        )

        # Generate timestamps: mulai dari 1 jam sebelum sekarang, interval 5s
        base_time = datetime.utcnow() - timedelta(hours=1)
        timestamps = [base_time + timedelta(seconds=i * 5) for i in range(n)]
        df["timestamp"] = timestamps

        # Susun ulang kolom agar sesuai format CSV asli
        cols = ["timestamp", "pod_name"] + NUMERIC_COLS + ["label"]
        extra_cols = [c for c in df.columns if c not in cols]
        df = df[cols + extra_cols].copy()

        return df

    def run(self, real_df: pd.DataFrame) -> pd.DataFrame:
        """Generate data sintetis dan gabungkan dengan data real."""
        self.compute_label_stats(real_df)

        generators = {
            "NORMAL":                self.generate_normal,
            "FAULT_CPU_STRESS":      self.generate_cpu_stress,
            "FAULT_MEMORY_LEAK":     self.generate_memory_leak,
            "FAULT_POD_CRASH":       self.generate_pod_crash,
            "FAULT_NETWORK_LATENCY": self.generate_network_latency,
        }

        synthetic_parts = []
        logger.info("")
        logger.info("Generating synthetic data...")

        for label, gen_fn in generators.items():
            real_count = self.stats_per_label.get(label, {}).get("count", 0)
            needed = max(0, self.target_per_class - real_count)

            if label not in self.stats_per_label:
                # Label tidak ada di data real, generate dari scratch dengan nilai default
                logger.warning(f"  {label}: Not found in real data, skipping")
                continue

            if needed <= 0:
                logger.info(f"  {label}: Already {real_count} rows, no synthetic needed")
                continue

            synth = gen_fn(needed)
            synth = self._add_metadata(synth, real_df, label)
            synth["is_synthetic"] = True
            synthetic_parts.append(synth)
            logger.info(f"  {label}: +{needed} synthetic rows (real={real_count})")

        if not synthetic_parts:
            logger.success("All classes already have enough data!")
            real_df["is_synthetic"] = False
            return real_df

        # Gabungkan semua
        synth_df = pd.concat(synthetic_parts, ignore_index=True)
        real_df["is_synthetic"] = False

        combined = pd.concat([real_df, synth_df], ignore_index=True)
        combined = combined.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)

        logger.info("")
        logger.info("=" * 55)
        logger.info("  Combined Dataset Distribution:")
        dist = combined["label"].value_counts()
        total = len(combined)
        for label, count in dist.items():
            real_c = len(real_df[real_df["label"] == label])
            synth_c = count - real_c
            pct = count / total * 100
            logger.info(f"  {label:<30} {count:>5} ({pct:4.1f}%) [real={real_c}, synth={synth_c}]")
        logger.info(f"  TOTAL: {total:,} rows")
        logger.info("=" * 55)

        return combined


def main():
    parser = argparse.ArgumentParser(description="XFSCI Synthetic Generator � Fase 3C")
    parser.add_argument("--input",            type=str, default=None)
    parser.add_argument("--output",           type=str, default=None)
    parser.add_argument("--target-per-class", type=int, default=1000,
                        help="Target minimum baris per kelas label (default: 1000)")
    parser.add_argument("--noise",            type=float, default=0.15,
                        help="Faktor noise Gaussian (default: 0.15)")
    args = parser.parse_args()

    logger.info("=" * 55)
    logger.info("  XFSCI Synthetic Generator � Fase 3C")
    logger.info(f"  Target per class : {args.target_per_class}")
    logger.info(f"  Noise factor     : {args.noise}")
    logger.info("=" * 55)

    # Pilih file input
    if args.input:
        csv_path = Path(args.input)
    else:
        files = sorted(PROCESSED_DIR.glob("cleaned_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            logger.error("Tidak ada cleaned_*.csv. Jalankan data_cleaner.py terlebih dahulu.")
            sys.exit(1)
        csv_path = files[0]
        logger.info(f"Using: {csv_path.name}")

    real_df = pd.read_csv(csv_path)
    real_df["timestamp"] = pd.to_datetime(real_df["timestamp"])
    logger.info(f"Real data: {len(real_df):,} rows")

    gen = SyntheticGenerator(
        target_per_class=args.target_per_class,
        noise_factor=args.noise,
    )
    combined = gen.run(real_df)

    # Simpan ke synthetic/ dan processed/
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    synth_only = combined[combined["is_synthetic"] == True]

    synth_path = SYNTHETIC_DIR / f"synthetic_{ts_str}.csv"
    synth_only.to_csv(synth_path, index=False)
    logger.info(f"Synthetic-only saved: {synth_path.name} ({len(synth_only):,} rows)")

    if args.output:
        combined_path = Path(args.output)
    else:
        combined_path = PROCESSED_DIR / f"augmented_{ts_str}.csv"
    combined.to_csv(combined_path, index=False)
    logger.success(f"Combined dataset saved: {combined_path.name} ({len(combined):,} rows)")

    logger.info("Next: python data/preprocessors/feature_engineer.py")


if __name__ == "__main__":
    main()
