"""
============================================================
XFSCI Data Labeler - Fase 3A: Preprocessing (Multi-Session)
============================================================
Memberikan label ground truth pada dataset mentah berdasarkan
rentang waktu skenario fault injection.

Mendukung 2 profil sesi:
  --session standard  : Timing dari run_faults.sh (~55 menit)
  --session turbo     : Timing dari run_faults_turbo.sh (~15 menit)

Mode multi-sesi:
  --merge-all         : Label semua CSV di data/raw/ dan gabungkan

Skema Label:
  - NORMAL               : Kondisi stabil (baseline & recovery)
  - FAULT_CPU_STRESS     : Beban CPU tinggi
  - FAULT_MEMORY_LEAK    : Kebocoran memori
  - FAULT_POD_CRASH      : Kematian mendadak pod acak
  - FAULT_NETWORK_LATENCY: Latensi jaringan tinggi

Cara pakai:
  python data/preprocessors/data_labeler.py
  python data/preprocessors/data_labeler.py --session turbo
  python data/preprocessors/data_labeler.py --merge-all
  python data/preprocessors/data_labeler.py --interactive
============================================================
"""

import os
import sys
import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
from loguru import logger

BASE_DIR = Path(__file__).parent.parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

LABEL_NORMAL          = "NORMAL"
LABEL_CPU_STRESS      = "FAULT_CPU_STRESS"
LABEL_MEMORY_LEAK     = "FAULT_MEMORY_LEAK"
LABEL_POD_CRASH       = "FAULT_POD_CRASH"
LABEL_NET_LATENCY     = "FAULT_NETWORK_LATENCY"

# Pods per node (berdasarkan nodeSelector di deployment)
WORKER1_PODS = ["frontend", "loadgenerator", "recommendationservice", "paymentservice"]
WORKER2_PODS = ["adservice", "cartservice", "productcatalogservice", "redis-cart"]
WORKER3_PODS = ["checkoutservice", "currencyservice", "emailservice", "shippingservice"]
POD_CRASH_TARGETS = ["frontend", "cartservice", "recommendationservice", "paymentservice"]


def get_pod_prefix(pod_name: str) -> str:
    """Ekstrak nama service dari nama pod lengkap."""
    parts = pod_name.split("-")
    if len(parts) >= 3:
        return "-".join(parts[:-2])
    return pod_name


class XFSCIDataLabeler:
    def __init__(self, fault_windows: dict = None):
        self.fault_windows = fault_windows or {}

    def load_raw_csv(self, csv_path: Path) -> pd.DataFrame:
        logger.info(f"Loading: {csv_path.name}")
        df = pd.read_csv(csv_path)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        logger.info(f"  Rows: {len(df):,} | Pods: {df['pod_name'].nunique()}")
        logger.info(f"  Range: {df['timestamp'].min()} -> {df['timestamp'].max()}")
        return df

    def apply_labels(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["label"] = LABEL_NORMAL
        ts = df["timestamp"]
        pod_prefix = df["pod_name"].apply(get_pod_prefix)

        # Prioritas: NORMAL < NET_LATENCY < POD_CRASH < MEMORY_LEAK < CPU_STRESS
        if "net_latency" in self.fault_windows:
            w = self.fault_windows["net_latency"]
            target_pods = w.get("target_pods") or WORKER3_PODS
            mask = (ts >= w["start"]) & (ts <= w["end"]) & pod_prefix.isin(target_pods)
            df.loc[mask, "label"] = LABEL_NET_LATENCY
            logger.info(f"  NETWORK_LATENCY : {mask.sum():>5} rows")

        if "pod_crash" in self.fault_windows:
            w = self.fault_windows["pod_crash"]
            target_pods = w.get("target_pods") or POD_CRASH_TARGETS
            in_window = (ts >= w["start"]) & (ts <= w["end"])
            # Hanya beri label POD_CRASH pada pod target yang restart-nya > 0 jika ada data restart,
            # atau setidaknya terbatas pada target_pods (bukan 11 pod seluruh cluster!)
            has_restarts = ((df.loc[in_window, "pod_restarts"] > 0) & pod_prefix.isin(target_pods)).any()
            if has_restarts:
                mask = in_window & (df["pod_restarts"] > 0) & pod_prefix.isin(target_pods)
            else:
                mask = in_window & pod_prefix.isin(target_pods)
            df.loc[mask, "label"] = LABEL_POD_CRASH
            logger.info(f"  POD_CRASH       : {mask.sum():>5} rows")

        if "memory_leak" in self.fault_windows:
            w = self.fault_windows["memory_leak"]
            target_pods = w.get("target_pods") or WORKER2_PODS
            mask = (ts >= w["start"]) & (ts <= w["end"]) & pod_prefix.isin(target_pods)
            df.loc[mask, "label"] = LABEL_MEMORY_LEAK
            logger.info(f"  MEMORY_LEAK     : {mask.sum():>5} rows")

        if "cpu_stress" in self.fault_windows:
            w = self.fault_windows["cpu_stress"]
            target_pods = w.get("target_pods") or WORKER2_PODS
            mask = (ts >= w["start"]) & (ts <= w["end"]) & pod_prefix.isin(target_pods)
            df.loc[mask, "label"] = LABEL_CPU_STRESS
            logger.info(f"  CPU_STRESS      : {mask.sum():>5} rows")

        return df

    def print_distribution(self, df: pd.DataFrame):
        dist = df["label"].value_counts()
        total = len(df)
        logger.info("")
        logger.info("=" * 55)
        logger.info("  Label Distribution:")
        for label, count in dist.items():
            pct = count / total * 100
            bar = "=" * int(pct / 2)
            logger.info(f"  {label:<30} {count:>5} ({pct:5.1f}%) {bar}")
        logger.info("=" * 55)

    def save(self, df: pd.DataFrame, output_path: Path = None) -> Path:
        if output_path is None:
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = PROCESSED_DIR / f"labeled_{ts_str}.csv"
        df.to_csv(output_path, index=False)
        logger.success(f"Saved: {output_path}")
        return output_path

    def run(self, csv_path: Path, output_path: Path = None):
        df = self.load_raw_csv(csv_path)
        logger.info("Applying labels...")
        df = self.apply_labels(df)
        self.print_distribution(df)
        out = self.save(df, output_path)
        return df, out


def auto_detect_windows(csv_path: Path, session: str = "standard") -> dict:
    """Deteksi otomatis fault windows berdasarkan profil sesi."""
    df = pd.read_csv(csv_path, nrows=1)
    start = pd.to_datetime(df["timestamp"].iloc[0])
    logger.info(f"Scraper start time: {start}")
    logger.info(f"Session profile: {session}")

    def T(minutes):
        return start + timedelta(minutes=minutes)

    if session == "turbo":
        # Timing dari run_faults_turbo.sh (~15 menit)
        windows = {
            "cpu_stress":  {"start": T(3.0),  "end": T(4.0),  "target_pods": WORKER1_PODS},
            "memory_leak": {"start": T(5.0),  "end": T(6.0),  "target_pods": WORKER3_PODS},
            "pod_crash":   {"start": T(7.0),  "end": T(12.0), "target_pods": POD_CRASH_TARGETS},
            "net_latency": {"start": T(13.0), "end": T(14.0), "target_pods": WORKER1_PODS},
        }
    else:
        # Timing dari run_faults.sh (~55 menit)
        windows = {
            "cpu_stress":  {"start": T(10.0), "end": T(12.5), "target_pods": WORKER2_PODS},
            "memory_leak": {"start": T(17.0), "end": T(22.5), "target_pods": WORKER2_PODS},
            "pod_crash":   {"start": T(27.0), "end": T(42.0), "target_pods": POD_CRASH_TARGETS},
            "net_latency": {"start": T(47.0), "end": T(50.5), "target_pods": WORKER3_PODS},
        }

    logger.info(f"Estimated fault windows (UTC) [{session}]:")
    for name, w in windows.items():
        pods_info = f" -> pods: {w.get('target_pods', 'all')}" if w.get('target_pods') else " -> pods: ALL"
        logger.info(f"  {name:<15}: {w['start'].strftime('%H:%M:%S')} -> {w['end'].strftime('%H:%M:%S')}{pods_info}")

    return windows


def interactive_input() -> dict:
    print("\n" + "=" * 55)
    print("  XFSCI Data Labeler - Mode Interaktif")
    print("  Format waktu: HH:MM:SS  (UTC)")
    print("  Tekan Enter untuk skip fase.")
    print("=" * 55)

    date_str = input("\nTanggal sesi (YYYY-MM-DD): ").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    def ask(prompt):
        val = input(prompt).strip()
        if not val:
            return None
        return datetime.strptime(f"{date_str} {val}", "%Y-%m-%d %H:%M:%S")

    windows = {}
    for key, name, node in [
        ("cpu_stress",  "CPU Stress",         "Worker 2"),
        ("memory_leak", "Memory Leak",        "Worker 2"),
        ("pod_crash",   "Pod Crash CronJob",  "All"),
        ("net_latency", "Network Latency",    "Worker 3"),
    ]:
        print(f"\nFAULT: {name} (Target: {node})")
        s = ask("  Mulai   (HH:MM:SS UTC): ")
        e = ask("  Selesai (HH:MM:SS UTC): ")
        if s and e:
            windows[key] = {"start": s, "end": e}

    return windows


def main():
    parser = argparse.ArgumentParser(description="XFSCI Data Labeler - Fase 3A (Multi-Session)")
    parser.add_argument("--input",       type=str, default=None, help="Path CSV input")
    parser.add_argument("--output",      type=str, default=None, help="Path CSV output")
    parser.add_argument("--interactive", action="store_true",    help="Input waktu fault manual")
    parser.add_argument("--session",     type=str, default="standard",
                        choices=["standard", "turbo"],
                        help="Profil sesi: standard (55min) atau turbo (15min)")
    parser.add_argument("--merge-all",   action="store_true",
                        help="Label SEMUA CSV di data/raw/ dan gabungkan jadi satu file")
    args = parser.parse_args()

    logger.info("=" * 55)
    logger.info("  XFSCI Data Labeler - Fase 3A (Multi-Session)")
    logger.info(f"  Session: {args.session}")
    logger.info("=" * 55)

    if args.merge_all:
        # ===== MODE MERGE-ALL: Label semua CSV dan gabungkan =====
        all_files = sorted(RAW_DIR.glob("metrics_*.csv"), key=lambda p: p.stat().st_mtime)
        if not all_files:
            logger.error(f"Tidak ada metrics_*.csv di {RAW_DIR}")
            sys.exit(1)

        logger.info(f"Found {len(all_files)} CSV file(s) to process:")
        all_labeled = []

        for i, csv_path in enumerate(all_files):
            logger.info(f"\n--- Processing file {i+1}/{len(all_files)}: {csv_path.name} ---")

            # Deteksi sesi: file pertama = standard, berikutnya = turbo
            session_type = "standard" if i == 0 else "turbo"
            logger.info(f"  Auto-detected session: {session_type}")

            fault_windows = auto_detect_windows(csv_path, session=session_type)
            labeler = XFSCIDataLabeler(fault_windows)
            df = labeler.load_raw_csv(csv_path)
            logger.info("  Applying labels...")
            df = labeler.apply_labels(df)
            df["source_file"] = csv_path.name
            df["session"] = session_type
            labeler.print_distribution(df)
            all_labeled.append(df)

        # Gabungkan semua
        merged = pd.concat(all_labeled, ignore_index=True)
        merged = merged.sort_values(["timestamp", "pod_name"]).reset_index(drop=True)

        logger.info(f"\n{'='*55}")
        logger.info(f"  MERGED Dataset: {len(merged):,} total rows from {len(all_files)} sessions")

        # Print merged distribution
        dist = merged["label"].value_counts()
        total = len(merged)
        for label, count in dist.items():
            pct = count / total * 100
            bar = "=" * int(pct / 2)
            logger.info(f"  {label:<30} {count:>5} ({pct:5.1f}%) {bar}")
        logger.info(f"{'='*55}")

        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = PROCESSED_DIR / f"labeled_merged_{ts_str}.csv"
        merged.to_csv(out_path, index=False)
        logger.success(f"Merged labeled file saved: {out_path}")
        logger.info("Next: python data/preprocessors/data_cleaner.py")
        return

    # ===== MODE SINGLE FILE =====
    if args.input:
        csv_path = Path(args.input)
    else:
        files = sorted(RAW_DIR.glob("metrics_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            logger.error(f"Tidak ada metrics_*.csv di {RAW_DIR}")
            sys.exit(1)
        csv_path = files[0]
        logger.info(f"Using latest CSV: {csv_path.name}")

    # Tentukan fault windows
    if args.interactive:
        fault_windows = interactive_input()
    else:
        fault_windows = auto_detect_windows(csv_path, session=args.session)

    # Simpan fault_windows.json
    windows_json = PROCESSED_DIR / "fault_windows.json"
    serialized = {k: {"start": str(v["start"]), "end": str(v["end"])} for k, v in fault_windows.items()}
    windows_json.write_text(json.dumps(serialized, indent=2))
    logger.info(f"Windows saved: {windows_json.name}")

    # Jalankan
    output_path = Path(args.output) if args.output else None
    labeler = XFSCIDataLabeler(fault_windows)
    df, out = labeler.run(csv_path, output_path)

    logger.success("Labeling selesai!")
    logger.info("Next: python data/preprocessors/data_cleaner.py")


if __name__ == "__main__":
    main()

