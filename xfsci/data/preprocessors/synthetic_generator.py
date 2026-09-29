"""
============================================================
XFSCI Synthetic Data Generator V3 - Graph-Aware Augmentation
============================================================
Generator sintetis CANGGIH yang menghasilkan data anomali
dengan variasi realistis berdasarkan model fisika sistem.

V3 Improvements (Critical fix for RCA accuracy):
  - Graph-Aware: setiap timestep menghasilkan 11 baris (1 per pod)
    dengan hanya pod target yang diberi label fault
  - Pod yang tidak terkena fault diberi metrik NORMAL yang realistis
  - Ini memastikan model GNN belajar membedakan pod fault vs pod sehat
    dalam konteks graf yang sama (bukan menyebar fault ke semua pod)

Peningkatan dari V2:
  1. Multi-intensitas per fault (ringan/sedang/berat)
  2. Kurva transisi temporal (onset -> peak -> recovery)
  3. Korelasi cross-metric (CPU naik -> latency naik -> error naik)
  4. Concurrent fault (CPU+Network, Memory+Crash, dll.)
  5. HANYA pod target yang diberi fault, pod lain NORMAL
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

# Pod penempatan per worker node (sesuai setup_cluster.sh)
WORKER1_PODS = ["frontend", "loadgenerator", "recommendationservice", "paymentservice"]
WORKER2_PODS = ["adservice", "cartservice", "productcatalogservice", "redis-cart"]
WORKER3_PODS = ["checkoutservice", "currencyservice", "emailservice", "shippingservice"]
POD_CRASH_TARGETS = ["frontend", "cartservice", "recommendationservice", "paymentservice"]

# Pod target per fault type (sesuai fault injection scenario)
FAULT_TARGET_PODS = {
    "FAULT_CPU_STRESS":      WORKER2_PODS,
    "FAULT_MEMORY_LEAK":     WORKER2_PODS,
    "FAULT_POD_CRASH":       POD_CRASH_TARGETS,
    "FAULT_NETWORK_LATENCY": WORKER3_PODS,
}

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


class GraphAwareGenerator:
    """
    V3: Graph-Aware Synthetic Generator.
    
    KUNCI PERBEDAAN dari V2:
    Generator ini menghasilkan FULL GRAPH SNAPSHOTS per timestep,
    dimana setiap timestep berisi 11 baris (1 per pod microservice).
    
    Hanya pod TARGET yang diberi label fault dan metrik anomali.
    Pod lainnya (7-8 pod) diberi label NORMAL dengan metrik baseline.
    
    Ini memastikan model GNN belajar membedakan pod bermasalah
    dari pod sehat dalam konteks graf yang sama — bukan memperlakukan
    semua pod sebagai fault yang merupakan bug utama di V2.
    """

    def __init__(self, target_per_class: int = 5000, noise_factor: float = 0.10):
        self.target_per_class = target_per_class
        self.noise_factor = noise_factor
        self.stats_per_label = {}
        # Cache pod names dari data real
        self._pod_names_map = {}

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
        
        # Cache pod names: service -> full pod name
        if "pod_name" in df.columns:
            for _, row in df.drop_duplicates("pod_name").iterrows():
                pod_name = row["pod_name"]
                parts = pod_name.split("-")
                if len(parts) >= 3:
                    svc = "-".join(parts[:-2])
                else:
                    svc = pod_name
                if svc not in self._pod_names_map:
                    self._pod_names_map[svc] = pod_name

    def _get_pod_full_name(self, service: str) -> str:
        """Dapatkan full pod name untuk service, generate jika belum ada."""
        if service in self._pod_names_map:
            return self._pod_names_map[service]
        # Generate realistic pod name
        suffix = f"{np.random.randint(1000,9999)}-{''.join(np.random.choice(list('abcdefghijklmnop0123456789'), 5))}"
        name = f"{service}-{suffix}"
        self._pod_names_map[service] = name
        return name

    # ============================================================
    # TEMPORAL CURVE GENERATORS
    # ============================================================

    def _onset_peak_recovery_curve(self, n: int, onset_frac: float = 0.15,
                                    peak_frac: float = 0.60,
                                    recovery_frac: float = 0.25) -> np.ndarray:
        """
        Membuat kurva transisi realistis: onset -> peak -> recovery.
        Mengembalikan array 0.0 -> 1.0 -> 0.0 sepanjang n titik.
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
        if len(curve) > n:
            curve = curve[:n]
        elif len(curve) < n:
            curve = np.pad(curve, (0, n - len(curve)), mode="edge")

        return curve

    def _generate_normal_metrics(self, n: int) -> dict:
        """Generate baseline metrik normal untuk satu pod."""
        stats = self.stats_per_label.get("NORMAL", {})
        rows = {}
        for col in NUMERIC_COLS:
            if stats:
                mean = stats["mean"][col]
                std = max(stats["std"][col], abs(mean) * 0.05)
            else:
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

    def _generate_fault_metrics_cpu_stress(self, n: int) -> dict:
        """Generate metrik CPU stress untuk pod target."""
        profiles = INTENSITY_PROFILES["FAULT_CPU_STRESS"]
        # Pick random intensity
        intensities = list(profiles.keys())
        weights = [profiles[k]["weight"] for k in intensities]
        chosen = np.random.choice(intensities, p=weights)
        profile = profiles[chosen]

        base = self._generate_normal_metrics(n)
        curve = self._onset_peak_recovery_curve(n)

        cpu_min, cpu_max = profile["cpu_range"]
        cpu_range = cpu_max - cpu_min
        base["cpu_usage"] = cpu_min + curve * cpu_range
        base["cpu_usage"] += np.random.normal(0, cpu_range * 0.08, n)
        base["cpu_usage"] = np.clip(base["cpu_usage"], 0.01, 4.0)

        mem_min, mem_max = profile["memory_pct_range"]
        base["memory_usage_percent"] = np.random.uniform(mem_min, mem_max, n)
        base["memory_usage"] = base["memory_usage_percent"] / 100 * 4 * 1024**3

        base["error_rate"] = np.clip(
            base["error_rate"] + curve * profile["error_boost"], 0, 1
        )
        base["request_rate"] = np.clip(
            base["request_rate"] * (1 - curve * 0.2), 0, None
        )
        return base

    def _generate_fault_metrics_memory_leak(self, n: int) -> dict:
        """Generate metrik memory leak untuk pod target."""
        profiles = INTENSITY_PROFILES["FAULT_MEMORY_LEAK"]
        intensities = list(profiles.keys())
        weights = [profiles[k]["weight"] for k in intensities]
        chosen = np.random.choice(intensities, p=weights)
        profile = profiles[chosen]

        base = self._generate_normal_metrics(n)
        base_bytes = profile["base_mb"] * 1024**2
        peak_bytes = profile["peak_mb"] * 1024**2

        t = np.linspace(0, 1, n)
        growth = base_bytes + (peak_bytes - base_bytes) * (t ** 1.5)
        noise = np.random.normal(0, (peak_bytes - base_bytes) * 0.03, n)
        base["memory_usage"] = np.clip(growth + noise, 0, 2 * 1024**3)
        base["memory_usage_percent"] = base["memory_usage"] / (4 * 1024**3) * 100

        gc_pressure = np.clip(t * 0.15, 0, 0.3)
        base["cpu_usage"] = base["cpu_usage"] + gc_pressure

        if np.random.random() < profile["oom_probability"]:
            crash_point = int(n * np.random.uniform(0.7, 0.95))
            base["memory_usage"][crash_point:] = base_bytes * 0.5
            base["memory_usage_percent"][crash_point:] = base_bytes * 0.5 / (4 * 1024**3) * 100
            base["pod_restarts"][crash_point:] = np.arange(1, n - crash_point + 1).clip(0, 5)

        return base

    def _generate_fault_metrics_pod_crash(self, n: int) -> dict:
        """Generate metrik pod crash untuk pod target."""
        profiles = INTENSITY_PROFILES["FAULT_POD_CRASH"]
        intensities = list(profiles.keys())
        weights = [profiles[k]["weight"] for k in intensities]
        chosen = np.random.choice(intensities, p=weights)
        profile = profiles[chosen]

        base = self._generate_normal_metrics(n)

        r_min, r_max = profile["restart_range"]
        restarts = np.random.randint(r_min, r_max + 1, n)
        restarts = np.sort(restarts)
        base["pod_restarts"] = restarts.astype(float)

        downtime_mask = np.random.random(n) < profile["downtime_fraction"]
        traffic_factor = np.where(downtime_mask, np.random.uniform(0.02, 0.2, n), 1.0)
        base["net_rx_bytes"] = base["net_rx_bytes"] * traffic_factor
        base["net_tx_bytes"] = base["net_tx_bytes"] * traffic_factor
        base["request_rate"] = base["request_rate"] * traffic_factor

        restart_spike = np.where(downtime_mask, np.random.uniform(0.3, 0.8, n), 0)
        base["cpu_usage"] = base["cpu_usage"] + restart_spike

        base["error_rate"] = np.where(
            downtime_mask,
            np.random.uniform(0.1, 0.5, n),
            base["error_rate"]
        )
        return base

    def _generate_fault_metrics_network_latency(self, n: int) -> dict:
        """Generate metrik network latency untuk pod target."""
        profiles = INTENSITY_PROFILES["FAULT_NETWORK_LATENCY"]
        intensities = list(profiles.keys())
        weights = [profiles[k]["weight"] for k in intensities]
        chosen = np.random.choice(intensities, p=weights)
        profile = profiles[chosen]

        base = self._generate_normal_metrics(n)
        curve = self._onset_peak_recovery_curve(n)

        multiplier = profile["traffic_multiplier"]
        base["net_rx_bytes"] = base["net_rx_bytes"] * (1 + curve * (multiplier - 1))
        base["net_tx_bytes"] = base["net_tx_bytes"] * (1 + curve * (multiplier - 1))

        jitter = np.random.normal(0, profile["jitter_ms"] * 10, n)
        base["net_rx_bytes"] = np.abs(base["net_rx_bytes"] + jitter)

        loss_factor = profile["packet_loss_pct"] / 100
        base["error_rate"] = np.clip(
            base["error_rate"] + curve * loss_factor * 0.5, 0, 1
        )
        base["request_rate"] = np.clip(
            base["request_rate"] * (1 - curve * min(profile["latency_ms"] / 1000, 0.6)),
            0, None
        )
        return base

    # ============================================================
    # GRAPH-AWARE SNAPSHOT GENERATOR (V3 Core Innovation)
    # ============================================================

    def generate_graph_snapshots(self, fault_label: str, n_timesteps: int) -> pd.DataFrame:
        """
        V3 CORE: Generate complete graph snapshots untuk satu jenis fault.
        
        Setiap timestep menghasilkan 11 baris (1 per microservice pod):
        - Pod TARGET (3-4 pods): diberi label fault + metrik anomali
        - Pod LAINNYA (7-8 pods): diberi label NORMAL + metrik baseline
        
        Args:
            fault_label: salah satu dari FAULT_LABELS
            n_timesteps: jumlah timestep yang dihasilkan
            
        Returns:
            DataFrame dengan n_timesteps * 11 baris
        """
        target_pods = FAULT_TARGET_PODS.get(fault_label, [])
        if not target_pods:
            # NORMAL: semua pod diberi metrik normal
            return self._generate_normal_snapshots(n_timesteps)

        # Pilih fault metric generator
        fault_generators = {
            "FAULT_CPU_STRESS":      self._generate_fault_metrics_cpu_stress,
            "FAULT_MEMORY_LEAK":     self._generate_fault_metrics_memory_leak,
            "FAULT_POD_CRASH":       self._generate_fault_metrics_pod_crash,
            "FAULT_NETWORK_LATENCY": self._generate_fault_metrics_network_latency,
        }
        fault_gen = fault_generators[fault_label]

        all_rows = []
        # Generate base timestamp sequence
        base_time = datetime.utcnow() - timedelta(hours=np.random.randint(1, 48))

        for pod_svc in ALL_PODS:
            pod_name = self._get_pod_full_name(pod_svc)
            is_target = pod_svc in target_pods

            if is_target:
                # Pod target: metrik fault + label fault
                metrics = fault_gen(n_timesteps)
                label = fault_label
            else:
                # Pod bukan target: metrik normal + label NORMAL
                metrics = self._generate_normal_metrics(n_timesteps)
                label = "NORMAL"

            timestamps = [base_time + timedelta(seconds=j * 5) for j in range(n_timesteps)]

            pod_df = pd.DataFrame(metrics)
            pod_df["timestamp"] = timestamps
            pod_df["pod_name"] = pod_name
            pod_df["pod_service"] = pod_svc
            pod_df["label"] = label
            pod_df["is_synthetic"] = True
            all_rows.append(pod_df)

        result = pd.concat(all_rows, ignore_index=True)
        result = result.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)

        # Reorder columns
        cols = ["timestamp", "pod_name"] + NUMERIC_COLS + ["label", "pod_service", "is_synthetic"]
        extra = [c for c in result.columns if c not in cols]
        result = result[cols + extra].copy()

        return result

    def _generate_normal_snapshots(self, n_timesteps: int) -> pd.DataFrame:
        """Generate complete normal graph snapshots."""
        all_rows = []
        base_time = datetime.utcnow() - timedelta(hours=np.random.randint(1, 48))

        for pod_svc in ALL_PODS:
            pod_name = self._get_pod_full_name(pod_svc)
            metrics = self._generate_normal_metrics(n_timesteps)

            # Diurnal pattern
            diurnal = 1 + 0.15 * np.sin(np.linspace(0, 4 * np.pi, n_timesteps))
            metrics["cpu_usage"] = metrics["cpu_usage"] * diurnal
            metrics["request_rate"] = metrics["request_rate"] * diurnal

            timestamps = [base_time + timedelta(seconds=j * 5) for j in range(n_timesteps)]
            pod_df = pd.DataFrame(metrics)
            pod_df["timestamp"] = timestamps
            pod_df["pod_name"] = pod_name
            pod_df["pod_service"] = pod_svc
            pod_df["label"] = "NORMAL"
            pod_df["is_synthetic"] = True
            all_rows.append(pod_df)

        result = pd.concat(all_rows, ignore_index=True)
        result = result.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)

        cols = ["timestamp", "pod_name"] + NUMERIC_COLS + ["label", "pod_service", "is_synthetic"]
        extra = [c for c in result.columns if c not in cols]
        result = result[cols + extra].copy()

        return result

    # ============================================================
    # CONCURRENT FAULT GENERATORS (Graph-Aware V3)
    # ============================================================

    def generate_concurrent_cpu_network(self, n_timesteps: int) -> pd.DataFrame:
        """CPU stress (Worker2) + Network latency (Worker3) bersamaan."""
        all_rows = []
        base_time = datetime.utcnow() - timedelta(hours=np.random.randint(1, 48))

        for pod_svc in ALL_PODS:
            pod_name = self._get_pod_full_name(pod_svc)

            if pod_svc in WORKER2_PODS:
                # CPU stress pada worker2
                metrics = self._generate_fault_metrics_cpu_stress(n_timesteps)
                label = "FAULT_CPU_STRESS"
            elif pod_svc in WORKER3_PODS:
                # Network latency pada worker3
                metrics = self._generate_fault_metrics_network_latency(n_timesteps)
                label = "FAULT_NETWORK_LATENCY"
            else:
                # Pod lain normal
                metrics = self._generate_normal_metrics(n_timesteps)
                label = "NORMAL"

            timestamps = [base_time + timedelta(seconds=j * 5) for j in range(n_timesteps)]
            pod_df = pd.DataFrame(metrics)
            pod_df["timestamp"] = timestamps
            pod_df["pod_name"] = pod_name
            pod_df["pod_service"] = pod_svc
            pod_df["label"] = label
            pod_df["is_synthetic"] = True
            all_rows.append(pod_df)

        result = pd.concat(all_rows, ignore_index=True)
        result = result.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)

        cols = ["timestamp", "pod_name"] + NUMERIC_COLS + ["label", "pod_service", "is_synthetic"]
        extra = [c for c in result.columns if c not in cols]
        return result[cols + extra].copy()

    def generate_concurrent_memory_crash(self, n_timesteps: int) -> pd.DataFrame:
        """Memory leak (Worker2) yang berujung OOM crash."""
        all_rows = []
        base_time = datetime.utcnow() - timedelta(hours=np.random.randint(1, 48))

        crash_point = int(n_timesteps * 0.7)

        for pod_svc in ALL_PODS:
            pod_name = self._get_pod_full_name(pod_svc)

            if pod_svc in WORKER2_PODS:
                # Memory leak → crash
                metrics = self._generate_fault_metrics_memory_leak(n_timesteps)

                timestamps = [base_time + timedelta(seconds=j * 5) for j in range(n_timesteps)]
                pod_df = pd.DataFrame(metrics)
                pod_df["timestamp"] = timestamps
                pod_df["pod_name"] = pod_name
                pod_df["pod_service"] = pod_svc
                pod_df["label"] = "FAULT_MEMORY_LEAK"
                pod_df.loc[crash_point:, "label"] = "FAULT_POD_CRASH"
                pod_df["is_synthetic"] = True
            else:
                metrics = self._generate_normal_metrics(n_timesteps)
                timestamps = [base_time + timedelta(seconds=j * 5) for j in range(n_timesteps)]
                pod_df = pd.DataFrame(metrics)
                pod_df["timestamp"] = timestamps
                pod_df["pod_name"] = pod_name
                pod_df["pod_service"] = pod_svc
                pod_df["label"] = "NORMAL"
                pod_df["is_synthetic"] = True

            all_rows.append(pod_df)

        result = pd.concat(all_rows, ignore_index=True)
        result = result.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)

        cols = ["timestamp", "pod_name"] + NUMERIC_COLS + ["label", "pod_service", "is_synthetic"]
        extra = [c for c in result.columns if c not in cols]
        return result[cols + extra].copy()

    # ============================================================
    # MAIN RUN
    # ============================================================

    def run(self, real_df: pd.DataFrame) -> pd.DataFrame:
        """Generate semua data sintetis dan gabung dengan real."""
        self.compute_label_stats(real_df)

        logger.info("")
        logger.info("=" * 60)
        logger.info("  V3 Graph-Aware Synthetic Generator")
        logger.info("  Each timestep generates 11 rows (1 per pod)")
        logger.info("  Only TARGET pods get fault labels")
        logger.info("=" * 60)

        synthetic_parts = []

        # Hitung berapa timestep per kelas yang dibutuhkan
        # target_per_class = target baris FAULT (hanya pod target, bukan 11 pod)
        for fault_label in ["FAULT_CPU_STRESS", "FAULT_MEMORY_LEAK",
                            "FAULT_POD_CRASH", "FAULT_NETWORK_LATENCY"]:
            target_pods = FAULT_TARGET_PODS[fault_label]
            n_target_pods = len([p for p in target_pods if p in ALL_PODS])

            # Hitung berapa baris fault yang sudah ada di data real
            real_count = len(real_df[real_df["label"] == fault_label]) if fault_label in real_df["label"].values else 0
            needed_fault_rows = max(0, self.target_per_class - real_count)

            if needed_fault_rows <= 0:
                logger.info(f"  {fault_label}: Already {real_count} fault rows, no synthetic needed")
                continue

            # Jumlah timestep: needed_fault_rows / n_target_pods
            # (karena setiap timestep hanya menghasilkan n_target_pods baris fault)
            n_timesteps = max(1, needed_fault_rows // max(n_target_pods, 1))

            logger.info(f"  {fault_label}: need {needed_fault_rows} fault rows → {n_timesteps} timesteps × {n_target_pods} target pods")

            snapshot_df = self.generate_graph_snapshots(fault_label, n_timesteps)
            synthetic_parts.append(snapshot_df)

            actual_fault = len(snapshot_df[snapshot_df["label"] == fault_label])
            actual_normal = len(snapshot_df[snapshot_df["label"] == "NORMAL"])
            logger.info(f"    Generated: {actual_fault} fault rows + {actual_normal} normal rows = {len(snapshot_df)} total")

        # Normal snapshots (untuk menyeimbangkan)
        real_normal_count = len(real_df[real_df["label"] == "NORMAL"]) if "NORMAL" in real_df["label"].values else 0
        needed_normal = max(0, self.target_per_class - real_normal_count)
        if needed_normal > 0:
            n_normal_timesteps = max(1, needed_normal // len(ALL_PODS))
            normal_df = self._generate_normal_snapshots(n_normal_timesteps)
            synthetic_parts.append(normal_df)
            logger.info(f"  NORMAL: +{len(normal_df)} rows ({n_normal_timesteps} timesteps)")

        # Concurrent faults (bonus)
        logger.info("")
        logger.info("  Generating concurrent fault scenarios...")
        concurrent_timesteps = max(1, self.target_per_class // (5 * len(ALL_PODS)))

        concurrent_cpu_net = self.generate_concurrent_cpu_network(concurrent_timesteps)
        synthetic_parts.append(concurrent_cpu_net)
        logger.info(f"  CONCURRENT (CPU+Network): +{len(concurrent_cpu_net)} rows ({concurrent_timesteps} timesteps)")

        concurrent_mem_crash = self.generate_concurrent_memory_crash(concurrent_timesteps)
        synthetic_parts.append(concurrent_mem_crash)
        logger.info(f"  CONCURRENT (Memory→Crash): +{len(concurrent_mem_crash)} rows ({concurrent_timesteps} timesteps)")

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
    parser = argparse.ArgumentParser(description="XFSCI Synthetic Generator V3 - Graph-Aware")
    parser.add_argument("--input",            type=str, default=None)
    parser.add_argument("--output",           type=str, default=None)
    parser.add_argument("--target-per-class", type=int, default=5000,
                        help="Target minimum baris FAULT per kelas (default: 5000)")
    parser.add_argument("--noise",            type=float, default=0.10,
                        help="Faktor noise (default: 0.10)")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  XFSCI Synthetic Generator V3 - Graph-Aware")
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

    gen = GraphAwareGenerator(
        target_per_class=args.target_per_class,
        noise_factor=args.noise,
    )
    combined = gen.run(real_df)

    # Save
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    synth_only = combined[combined["is_synthetic"] == True]

    synth_path = SYNTHETIC_DIR / f"synthetic_v3_{ts_str}.csv"
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
