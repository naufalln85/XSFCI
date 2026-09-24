"""
============================================================
XFSCI Synthetic Data Generator V2 - Physics-Based Augmentation
============================================================
Generator sintetis CANGGIH yang menghasilkan data anomali
dengan variasi realistis berdasarkan model fisika sistem:

Peningkatan dari V1:
  1. Multi-intensitas per fault (ringan/sedang/berat)
  2. Kurva transisi temporal (onset -> peak -> recovery)
  3. Korelasi cross-metric (CPU naik -> latency naik -> error naik)
  4. Concurrent fault (CPU+Network, Memory+Crash, dll.)
  5. Distribusi ke semua pod (bukan hanya pod tertentu)
  6. Time-series windowing untuk LSTM training

Target output: ~50.000 baris data berkualitas tinggi.

Cara pakai:
  python data/preprocessors/synthetic_generator.py
  python data/preprocessors/synthetic_generator.py --target-per-class 8000
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

# Pod names from the Online Boutique microservices
ALL_PODS = [
    "frontend", "cartservice", "productcatalogservice", "redis-cart",
    "checkoutservice", "currencyservice", "emailservice", "shippingservice",
    "adservice", "recommendationservice", "paymentservice",
]

# Seed for reproducibility
np.random.seed(42)


# ============================================================
# INTENSITY PROFILES
# ============================================================
# Setiap fault memiliki 3 profil intensitas yang berbeda
# agar model belajar mengenali fault di semua level

INTENSITY_PROFILES = {
    "FAULT_CPU_STRESS": {
        "light": {
            "cpu_range": (0.15, 0.45),       # 15-45% CPU (ringan)
            "memory_pct_range": (20, 35),
            "error_boost": 0.01,
            "weight": 0.3,
        },
        "medium": {
            "cpu_range": (0.40, 1.2),         # 40-120% CPU (sedang)
            "memory_pct_range": (25, 45),
            "error_boost": 0.03,
            "weight": 0.4,
        },
        "heavy": {
            "cpu_range": (1.0, 2.5),          # 100-250% CPU (berat)
            "memory_pct_range": (30, 60),
            "error_boost": 0.08,
            "weight": 0.3,
        },
    },
    "FAULT_MEMORY_LEAK": {
        "slow": {
            "base_mb": 128, "peak_mb": 512,   # Leak lambat
            "duration_factor": 1.0,
            "oom_probability": 0.05,
            "weight": 0.3,
        },
        "medium": {
            "base_mb": 256, "peak_mb": 1024,  # Leak sedang
            "duration_factor": 0.6,
            "oom_probability": 0.15,
            "weight": 0.4,
        },
        "fast": {
            "base_mb": 512, "peak_mb": 1536,  # Leak cepat -> OOM
            "duration_factor": 0.3,
            "oom_probability": 0.40,
            "weight": 0.3,
        },
    },
    "FAULT_NETWORK_LATENCY": {
        "light": {
            "latency_ms": 50, "jitter_ms": 15,
            "packet_loss_pct": 1,
            "traffic_multiplier": 1.3,
            "weight": 0.3,
        },
        "medium": {
            "latency_ms": 200, "jitter_ms": 50,
            "packet_loss_pct": 5,
            "traffic_multiplier": 2.0,
            "weight": 0.4,
        },
        "heavy": {
            "latency_ms": 500, "jitter_ms": 150,
            "packet_loss_pct": 15,
            "traffic_multiplier": 3.5,
            "weight": 0.3,
        },
    },
    "FAULT_POD_CRASH": {
        "single": {
            "restart_range": (1, 2),
            "downtime_fraction": 0.15,
            "cascade_pods": 1,
            "weight": 0.3,
        },
        "repeated": {
            "restart_range": (2, 5),
            "downtime_fraction": 0.35,
            "cascade_pods": 2,
            "weight": 0.4,
        },
        "cascade": {
            "restart_range": (4, 10),
            "downtime_fraction": 0.60,
            "cascade_pods": 4,
            "weight": 0.3,
        },
    },
}


class PhysicsBasedGenerator:
    """
    Generator sintetis berbasis model fisika sistem.
    Menghasilkan data dengan:
    - Multi-intensitas (ringan/sedang/berat)
    - Kurva transisi temporal (onset -> peak -> recovery)
    - Korelasi cross-metric yang realistis
    - Concurrent fault support
    """

    def __init__(self, target_per_class: int = 5000, noise_factor: float = 0.10):
        self.target_per_class = target_per_class
        self.noise_factor = noise_factor
        self.stats_per_label = {}

    def compute_label_stats(self, df: pd.DataFrame):
        """Hitung statistik dari data real sebagai baseline."""
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

    # ============================================================
    # TEMPORAL CURVE GENERATORS
    # ============================================================

    def _onset_peak_recovery_curve(self, n: int, onset_frac: float = 0.15,
                                    peak_frac: float = 0.60,
                                    recovery_frac: float = 0.25) -> np.ndarray:
        """
        Membuat kurva transisi realistis: onset -> peak -> recovery.
        Mengembalikan array 0.0 -> 1.0 -> 0.0 sepanjang n titik.

        Ini KRITIS untuk LSTM karena model perlu belajar
        mengenali TRANSISI, bukan hanya titik-titik anomali acak.
        """
        n_onset = max(1, int(n * onset_frac))
        n_peak = max(1, int(n * peak_frac))
        n_recovery = max(1, n - n_onset - n_peak)

        # Onset: sigmoid naik (0 -> 1)
        onset = 1 / (1 + np.exp(-np.linspace(-5, 5, n_onset)))
        # Peak: plateau dengan sedikit fluktuasi
        peak = 1.0 + np.random.normal(0, 0.05, n_peak)
        peak = np.clip(peak, 0.85, 1.15)
        # Recovery: exponential decay (1 -> 0)
        recovery = np.exp(-np.linspace(0, 4, n_recovery))

        curve = np.concatenate([onset, peak, recovery])
        # Pastikan panjang tepat n
        if len(curve) > n:
            curve = curve[:n]
        elif len(curve) < n:
            curve = np.pad(curve, (0, n - len(curve)), mode="edge")

        return curve

    def _generate_base_normal(self, n: int) -> dict:
        """Generate baseline metrik normal."""
        stats = self.stats_per_label.get("NORMAL", {})
        rows = {}
        for col in NUMERIC_COLS:
            if stats:
                mean = stats["mean"][col]
                std = max(stats["std"][col], abs(mean) * 0.05)
            else:
                # Fallback defaults
                defaults = {
                    "cpu_usage": 0.05, "memory_usage": 50*1024**2,
                    "memory_usage_percent": 15, "pod_restarts": 0,
                    "net_rx_bytes": 5000, "net_tx_bytes": 3000,
                    "request_rate": 10, "error_rate": 0.01,
                }
                mean = defaults.get(col, 1.0)
                std = mean * 0.15

            values = np.random.normal(mean, std, n)
            values = np.clip(values, 0, None)
            rows[col] = values

        rows["pod_restarts"] = np.zeros(n)
        return rows

    # ============================================================
    # FAULT-SPECIFIC GENERATORS (Multi-Intensity)
    # ============================================================

    def generate_cpu_stress(self, n: int) -> pd.DataFrame:
        """
        Generate CPU stress data dengan 3 intensitas berbeda.
        Setiap intensitas menghasilkan pola CPU yang berbeda,
        lengkap dengan kurva onset-peak-recovery.
        """
        profiles = INTENSITY_PROFILES["FAULT_CPU_STRESS"]
        all_rows = []

        for intensity_name, profile in profiles.items():
            n_this = int(n * profile["weight"])
            if n_this == 0:
                continue

            base = self._generate_base_normal(n_this)
            curve = self._onset_peak_recovery_curve(n_this)

            cpu_min, cpu_max = profile["cpu_range"]
            cpu_range = cpu_max - cpu_min
            base["cpu_usage"] = cpu_min + curve * cpu_range
            # Tambahkan noise realistis
            base["cpu_usage"] += np.random.normal(0, cpu_range * 0.08, n_this)
            base["cpu_usage"] = np.clip(base["cpu_usage"], 0.01, 4.0)

            # Cross-metric correlation: CPU tinggi -> memory sedikit naik
            mem_min, mem_max = profile["memory_pct_range"]
            base["memory_usage_percent"] = np.random.uniform(mem_min, mem_max, n_this)
            base["memory_usage"] = base["memory_usage_percent"] / 100 * 4 * 1024**3

            # CPU tinggi -> error rate sedikit naik
            base["error_rate"] = np.clip(
                base["error_rate"] + curve * profile["error_boost"],
                0, 1
            )

            # CPU tinggi -> request rate turun (resource contention)
            base["request_rate"] = np.clip(
                base["request_rate"] * (1 - curve * 0.2), 0, None
            )

            df_part = pd.DataFrame(base)
            df_part["label"] = "FAULT_CPU_STRESS"
            df_part["_intensity"] = intensity_name
            all_rows.append(df_part)

        return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

    def generate_memory_leak(self, n: int) -> pd.DataFrame:
        """
        Generate memory leak dengan 3 kecepatan berbeda.
        Mensimulasikan ramp-up gradual yang realistis.
        """
        profiles = INTENSITY_PROFILES["FAULT_MEMORY_LEAK"]
        all_rows = []

        for intensity_name, profile in profiles.items():
            n_this = int(n * profile["weight"])
            if n_this == 0:
                continue

            base = self._generate_base_normal(n_this)

            # Ramp-up memory: base -> peak (linear + noise)
            base_bytes = profile["base_mb"] * 1024**2
            peak_bytes = profile["peak_mb"] * 1024**2

            # Kurva ramp-up non-linear (exponential growth, lebih realistis)
            t = np.linspace(0, 1, n_this)
            growth = base_bytes + (peak_bytes - base_bytes) * (t ** 1.5)
            noise = np.random.normal(0, (peak_bytes - base_bytes) * 0.03, n_this)
            base["memory_usage"] = np.clip(growth + noise, 0, 2 * 1024**3)
            base["memory_usage_percent"] = base["memory_usage"] / (4 * 1024**3) * 100

            # Cross-metric: memory leak -> CPU sedikit naik (GC pressure)
            gc_pressure = np.clip(t * 0.15, 0, 0.3)
            base["cpu_usage"] = base["cpu_usage"] + gc_pressure

            # OOM crash simulation: di akhir kurva, tiba-tiba drop
            if np.random.random() < profile["oom_probability"]:
                crash_point = int(n_this * np.random.uniform(0.7, 0.95))
                base["memory_usage"][crash_point:] = base_bytes * 0.5
                base["memory_usage_percent"][crash_point:] = base_bytes * 0.5 / (4 * 1024**3) * 100
                base["pod_restarts"][crash_point:] = np.arange(1, n_this - crash_point + 1).clip(0, 5)

            df_part = pd.DataFrame(base)
            df_part["label"] = "FAULT_MEMORY_LEAK"
            df_part["_intensity"] = intensity_name
            all_rows.append(df_part)

        return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

    def generate_pod_crash(self, n: int) -> pd.DataFrame:
        """
        Generate pod crash dengan variasi severity.
        Single crash vs repeated crash vs cascading crash.
        """
        profiles = INTENSITY_PROFILES["FAULT_POD_CRASH"]
        all_rows = []

        for intensity_name, profile in profiles.items():
            n_this = int(n * profile["weight"])
            if n_this == 0:
                continue

            base = self._generate_base_normal(n_this)

            # Restart count
            r_min, r_max = profile["restart_range"]
            restarts = np.random.randint(r_min, r_max + 1, n_this)
            # Buat pola step-wise (restart naik bertahap)
            restarts = np.sort(restarts)
            base["pod_restarts"] = restarts.astype(float)

            # Downtime: traffic drop saat pod mati
            downtime_mask = np.random.random(n_this) < profile["downtime_fraction"]
            traffic_factor = np.where(downtime_mask, np.random.uniform(0.02, 0.2, n_this), 1.0)
            base["net_rx_bytes"] = base["net_rx_bytes"] * traffic_factor
            base["net_tx_bytes"] = base["net_tx_bytes"] * traffic_factor
            base["request_rate"] = base["request_rate"] * traffic_factor

            # CPU spike saat restart (init containers loading)
            restart_spike = np.where(downtime_mask, np.random.uniform(0.3, 0.8, n_this), 0)
            base["cpu_usage"] = base["cpu_usage"] + restart_spike

            # Error rate naik saat crash
            base["error_rate"] = np.where(
                downtime_mask,
                np.random.uniform(0.1, 0.5, n_this),
                base["error_rate"]
            )

            df_part = pd.DataFrame(base)
            df_part["label"] = "FAULT_POD_CRASH"
            df_part["_intensity"] = intensity_name
            all_rows.append(df_part)

        return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

    def generate_network_latency(self, n: int) -> pd.DataFrame:
        """
        Generate network latency dengan 3 level severity.
        """
        profiles = INTENSITY_PROFILES["FAULT_NETWORK_LATENCY"]
        all_rows = []

        for intensity_name, profile in profiles.items():
            n_this = int(n * profile["weight"])
            if n_this == 0:
                continue

            base = self._generate_base_normal(n_this)
            curve = self._onset_peak_recovery_curve(n_this)

            # Net traffic naik karena retransmisi TCP
            multiplier = profile["traffic_multiplier"]
            base["net_rx_bytes"] = base["net_rx_bytes"] * (1 + curve * (multiplier - 1))
            base["net_tx_bytes"] = base["net_tx_bytes"] * (1 + curve * (multiplier - 1))

            # Jitter: variabilitas tinggi di traffic
            jitter = np.random.normal(0, profile["jitter_ms"] * 10, n_this)
            base["net_rx_bytes"] = np.abs(base["net_rx_bytes"] + jitter)

            # Packet loss -> error rate naik
            loss_factor = profile["packet_loss_pct"] / 100
            base["error_rate"] = np.clip(
                base["error_rate"] + curve * loss_factor * 0.5,
                0, 1
            )

            # Latency tinggi -> request rate turun (timeout)
            latency_impact = profile["latency_ms"] / 1000
            base["request_rate"] = np.clip(
                base["request_rate"] * (1 - curve * min(latency_impact, 0.6)),
                0, None
            )

            df_part = pd.DataFrame(base)
            df_part["label"] = "FAULT_NETWORK_LATENCY"
            df_part["_intensity"] = intensity_name
            all_rows.append(df_part)

        return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

    def generate_normal(self, n: int) -> pd.DataFrame:
        """Generate data normal dengan variasi waktu realistis."""
        base = self._generate_base_normal(n)
        base["pod_restarts"] = np.zeros(n)
        # Tambahkan pola diurnal (CPU naik sedikit di jam sibuk)
        diurnal = 1 + 0.15 * np.sin(np.linspace(0, 4 * np.pi, n))
        base["cpu_usage"] = base["cpu_usage"] * diurnal
        base["request_rate"] = base["request_rate"] * diurnal

        df = pd.DataFrame(base)
        df["label"] = "NORMAL"
        df["_intensity"] = "baseline"
        return df

    # ============================================================
    # CONCURRENT FAULT GENERATORS
    # ============================================================

    def generate_concurrent_cpu_network(self, n: int) -> pd.DataFrame:
        """CPU stress + Network latency bersamaan."""
        base = self._generate_base_normal(n)
        curve = self._onset_peak_recovery_curve(n)

        # CPU stress sedang
        base["cpu_usage"] = 0.5 + curve * 1.0
        base["cpu_usage"] += np.random.normal(0, 0.1, n)

        # Network latency sedang
        base["net_rx_bytes"] = base["net_rx_bytes"] * (1 + curve * 1.5)
        base["net_tx_bytes"] = base["net_tx_bytes"] * (1 + curve * 1.2)

        # Combined effect: error rate lebih tinggi dari single fault
        base["error_rate"] = np.clip(curve * 0.12, 0, 0.5)

        # Request rate turun signifikan
        base["request_rate"] = base["request_rate"] * (1 - curve * 0.4)

        df = pd.DataFrame(base)
        df["label"] = "FAULT_CPU_STRESS"  # Label dominan
        df["_intensity"] = "concurrent_cpu_net"
        return df

    def generate_concurrent_memory_crash(self, n: int) -> pd.DataFrame:
        """Memory leak yang berujung OOM crash."""
        base = self._generate_base_normal(n)

        # Memory ramp-up
        t = np.linspace(0, 1, n)
        base["memory_usage"] = 256 * 1024**2 + t * 768 * 1024**2
        base["memory_usage_percent"] = base["memory_usage"] / (4 * 1024**3) * 100

        # OOM crash di 70% perjalanan
        crash_point = int(n * 0.7)
        base["pod_restarts"][:crash_point] = 0
        base["pod_restarts"][crash_point:] = np.arange(1, n - crash_point + 1).clip(0, 5).astype(float)

        # Setelah crash: memory reset, CPU spike dari restart
        base["memory_usage"][crash_point:] = 100 * 1024**2
        base["memory_usage_percent"][crash_point:] = 100 * 1024**2 / (4 * 1024**3) * 100
        base["cpu_usage"][crash_point:crash_point+int(n*0.1)] = np.random.uniform(0.3, 0.6, min(int(n*0.1), n - crash_point))

        # Error spike saat crash
        base["error_rate"][crash_point:crash_point+int(n*0.05)] = np.random.uniform(0.2, 0.8, min(int(n*0.05), n - crash_point))

        df = pd.DataFrame(base)
        # Bagian awal = memory leak, bagian akhir = pod crash
        df["label"] = "FAULT_MEMORY_LEAK"
        df.loc[crash_point:, "label"] = "FAULT_POD_CRASH"
        df["_intensity"] = "concurrent_mem_crash"
        return df

    # ============================================================
    # METADATA & ASSEMBLY
    # ============================================================

    def _add_metadata(self, df: pd.DataFrame, real_df: pd.DataFrame) -> pd.DataFrame:
        """Tambahkan timestamp, pod_name, pod_service realistis."""
        n = len(df)

        # Distribusikan ke semua pod (bukan hanya pod dari label tertentu)
        all_pods = real_df["pod_name"].unique()
        if len(all_pods) == 0:
            all_pods = [f"{p}-{np.random.randint(1000,9999)}-{np.random.choice(list('abcdef'))}{np.random.choice(list('abcdef'))}{np.random.choice(list('0123456789'))}{np.random.choice(list('0123456789'))}{np.random.choice(list('abcdef'))}"
                        for p in ALL_PODS]
        df["pod_name"] = np.random.choice(all_pods, n)
        df["pod_service"] = df["pod_name"].apply(
            lambda x: "-".join(x.split("-")[:-2]) if len(x.split("-")) >= 3 else x
        )

        # Timestamps: beberapa batch di waktu berbeda
        n_batches = max(1, n // 500)
        timestamps = []
        for batch_i in range(n_batches):
            batch_size = n // n_batches if batch_i < n_batches - 1 else n - len(timestamps)
            # Spread across different "hours"
            base_time = datetime.utcnow() - timedelta(hours=np.random.randint(1, 24))
            batch_ts = [base_time + timedelta(seconds=j * 5) for j in range(batch_size)]
            timestamps.extend(batch_ts)

        df["timestamp"] = timestamps[:n]

        # Reorder columns
        cols = ["timestamp", "pod_name"] + NUMERIC_COLS + ["label", "pod_service"]
        extra = [c for c in df.columns if c not in cols and c != "_intensity"]
        df = df[cols + extra].copy()

        return df

    def run(self, real_df: pd.DataFrame) -> pd.DataFrame:
        """Generate semua data sintetis dan gabung dengan real."""
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
        logger.info("=" * 60)
        logger.info("  Generating physics-based synthetic data...")
        logger.info("=" * 60)

        for label, gen_fn in generators.items():
            real_count = self.stats_per_label.get(label, {}).get("count", 0)
            needed = max(0, self.target_per_class - real_count)

            if needed <= 0:
                logger.info(f"  {label}: Already {real_count} rows, no synthetic needed")
                continue

            synth = gen_fn(needed)
            if len(synth) == 0:
                continue

            synth = self._add_metadata(synth, real_df)
            synth["is_synthetic"] = True

            # Log intensitas breakdown
            if "_intensity" in synth.columns:
                intensity_dist = synth["_intensity"].value_counts().to_dict()
                detail = ", ".join([f"{k}={v}" for k, v in intensity_dist.items()])
                synth.drop(columns=["_intensity"], inplace=True)
            else:
                detail = "uniform"

            synthetic_parts.append(synth)
            logger.info(f"  {label}: +{len(synth)} rows (real={real_count}) [{detail}]")

        # Concurrent faults (bonus rows)
        logger.info("")
        logger.info("  Generating concurrent fault scenarios...")
        concurrent_n = self.target_per_class // 5  # 20% dari target

        concurrent_cpu_net = self.generate_concurrent_cpu_network(concurrent_n)
        concurrent_cpu_net = self._add_metadata(concurrent_cpu_net, real_df)
        concurrent_cpu_net["is_synthetic"] = True
        if "_intensity" in concurrent_cpu_net.columns:
            concurrent_cpu_net.drop(columns=["_intensity"], inplace=True)
        synthetic_parts.append(concurrent_cpu_net)
        logger.info(f"  CONCURRENT (CPU+Network): +{len(concurrent_cpu_net)} rows")

        concurrent_mem_crash = self.generate_concurrent_memory_crash(concurrent_n)
        concurrent_mem_crash = self._add_metadata(concurrent_mem_crash, real_df)
        concurrent_mem_crash["is_synthetic"] = True
        if "_intensity" in concurrent_mem_crash.columns:
            concurrent_mem_crash.drop(columns=["_intensity"], inplace=True)
        synthetic_parts.append(concurrent_mem_crash)
        logger.info(f"  CONCURRENT (Memory->Crash): +{len(concurrent_mem_crash)} rows")

        if not synthetic_parts:
            logger.success("All classes already have enough data!")
            real_df["is_synthetic"] = False
            return real_df

        # Combine
        synth_df = pd.concat(synthetic_parts, ignore_index=True)
        real_df = real_df.copy()
        real_df["is_synthetic"] = False

        combined = pd.concat([real_df, synth_df], ignore_index=True)
        combined = combined.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)

        # Print distribution
        logger.info("")
        logger.info("=" * 60)
        logger.info("  Final Combined Dataset Distribution:")
        dist = combined["label"].value_counts()
        total = len(combined)
        for label, count in dist.items():
            real_c = len(real_df[real_df["label"] == label]) if label in real_df["label"].values else 0
            synth_c = count - real_c
            pct = count / total * 100
            bar = "#" * int(pct / 2)
            logger.info(f"  {label:<30} {count:>6} ({pct:5.1f}%) [real={real_c}, synth={synth_c}] {bar}")
        logger.info(f"  {'TOTAL':<30} {total:>6}")
        logger.info("=" * 60)

        return combined


def main():
    parser = argparse.ArgumentParser(description="XFSCI Synthetic Generator V2 - Physics-Based")
    parser.add_argument("--input",            type=str, default=None)
    parser.add_argument("--output",           type=str, default=None)
    parser.add_argument("--target-per-class", type=int, default=5000,
                        help="Target minimum baris per kelas (default: 5000)")
    parser.add_argument("--noise",            type=float, default=0.10,
                        help="Faktor noise (default: 0.10)")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  XFSCI Synthetic Generator V2 - Physics-Based")
    logger.info(f"  Target per class : {args.target_per_class}")
    logger.info(f"  Noise factor     : {args.noise}")
    logger.info("=" * 60)

    # Input file
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

    gen = PhysicsBasedGenerator(
        target_per_class=args.target_per_class,
        noise_factor=args.noise,
    )
    combined = gen.run(real_df)

    # Save
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    synth_only = combined[combined["is_synthetic"] == True]

    synth_path = SYNTHETIC_DIR / f"synthetic_v2_{ts_str}.csv"
    synth_only.to_csv(synth_path, index=False)
    logger.info(f"Synthetic-only saved: {synth_path.name} ({len(synth_only):,} rows)")

    if args.output:
        combined_path = Path(args.output)
    else:
        combined_path = PROCESSED_DIR / f"augmented_{ts_str}.csv"
    combined.to_csv(combined_path, index=False)
    logger.success(f"Combined dataset saved: {combined_path.name} ({len(combined):,} rows)")

    logger.info("")
    logger.info("Next: python data/preprocessors/feature_engineer.py")


if __name__ == "__main__":
    main()
