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
import re
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


def _utc_naive(value):
    """Normalize CSV/log timestamps to comparable UTC-naive pandas timestamps."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


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
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
        logger.info(f"  Rows: {len(df):,} | Pods: {df['pod_name'].nunique()}")
        logger.info(f"  Range: {df['timestamp'].min()} -> {df['timestamp'].max()}")
        return df

    def apply_labels(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["label"] = LABEL_NORMAL
        ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
        df["timestamp"] = ts
        pod_prefix = df["pod_name"].apply(get_pod_prefix)

        # Prioritas: NORMAL < NET_LATENCY < POD_CRASH < MEMORY_LEAK < CPU_STRESS
        def window_mask(window):
            start, end = _utc_naive(window["start"]), _utc_naive(window["end"])
            mask = (ts >= start) & (ts <= end)
            node = str(window.get("node_name") or "").strip()
            if node and "node_name" in df.columns:
                mask &= df["node_name"].astype(str).eq(node)
            return mask

        def complete_rows(mask):
            if "telemetry_complete" in df.columns:
                return mask & pd.to_numeric(df["telemetry_complete"], errors="coerce").fillna(0).eq(1)
            return mask

        def baseline_by_pod(window, metric):
            start = _utc_naive(window["start"])
            base_mask = (ts >= start - pd.Timedelta(minutes=5)) & (ts < start)
            node = str(window.get("node_name") or "").strip()
            if node and "node_name" in df.columns:
                base_mask &= df["node_name"].astype(str).eq(node)
            if "telemetry_complete" in df.columns:
                base_mask &= pd.to_numeric(df["telemetry_complete"], errors="coerce").fillna(0).eq(1)
            values = pd.to_numeric(df.loc[base_mask, metric], errors="coerce")
            baseline = values.groupby(df.loc[base_mask, "pod_name"]).median()
            mad = values.groupby(df.loc[base_mask, "pod_name"]).apply(
                lambda series: (series - series.median()).abs().median()
            )
            return baseline, mad

        def apply_observed_node_fault(fault_name, label, metric, predicate):
            window = self.fault_windows.get(fault_name)
            if not window:
                return
            if not window.get("node_name"):
                logger.warning(f"  {fault_name}: no injector node recorded; refusing broad service labeling")
                return
            in_scope = complete_rows(window_mask(window))
            baseline, mad = baseline_by_pod(window, metric)
            per_pod_mask = pd.Series(False, index=df.index)
            for pod_name, indices in df.loc[in_scope].groupby("pod_name").groups.items():
                if pod_name not in baseline.index:
                    continue
                rows = df.loc[indices]
                per_pod_mask.loc[indices] = predicate(rows, float(baseline[pod_name]), float(mad.get(pod_name, 0.0)))
            df.loc[per_pod_mask, "label"] = label
            logger.info(f"  {fault_name:<16}: {int(per_pod_mask.sum()):>5} rows with measured {metric} change")

        if "net_latency" in self.fault_windows:
            window = self.fault_windows["net_latency"]
            if "request_latency_p95_ms" in df.columns:
                apply_observed_node_fault(
                    "net_latency", LABEL_NET_LATENCY, "request_latency_p95_ms",
                    lambda rows, baseline, _mad: (
                        pd.to_numeric(rows["request_latency_p95_ms"], errors="coerce")
                        >= max(100.0, baseline + 50.0, baseline * 1.5)
                    ),
                )
            else:
                logger.warning("  net_latency: request_latency_p95_ms missing; rows remain unlabeled")

        if "pod_crash" in self.fault_windows:
            w = self.fault_windows["pod_crash"]
            events = w.get("events", [])
            crash_mask = pd.Series(False, index=df.index)
            confirmed_events = 0
            for event in events:
                service = str(event.get("service", "")).strip()
                event_time = _utc_naive(event["timestamp"])
                # The deleted pod itself is the intervention target; only
                # retain it if model inputs show readiness loss or restarts.
                event_mask = (
                    pod_prefix.eq(service)
                    & (ts >= event_time)
                    & (ts <= event_time + pd.Timedelta(seconds=20))
                )
                event_rows = df.loc[event_mask]
                observed = pd.Series(False, index=df.index)
                if not event_rows.empty:
                    observed.loc[event_rows.index] = False
                    if "service_ready_ratio" in df:
                        readiness = pd.to_numeric(
                            event_rows["service_ready_ratio"], errors="coerce"
                        )
                        observed.loc[event_rows.index] |= readiness.lt(0.999).fillna(False)
                    if "pod_restarts" in df:
                        restarts = pd.to_numeric(event_rows["pod_restarts"], errors="coerce")
                        observed.loc[event_rows.index] |= restarts.gt(0).fillna(False)
                    confirmed_mask = event_mask & observed
                if confirmed_mask.any():
                    confirmed_events += 1
                    crash_mask |= confirmed_mask
            if events:
                df.loc[crash_mask, "label"] = LABEL_POD_CRASH
                logger.info(
                    f"  POD_CRASH       : {int(crash_mask.sum()):>5} rows; "
                    f"{confirmed_events}/{len(events)} deletes had observable readiness/restart evidence"
                )
            else:
                logger.warning("  POD_CRASH       : no exact pod-delete records; refusing time-window fallback labels")

        if "memory_leak" in self.fault_windows:
            def memory_increased(rows, baseline, _mad):
                values = pd.to_numeric(rows["memory_usage"], errors="coerce")
                threshold = max(5 * 1024**2, baseline * 0.03)
                return values >= baseline + threshold
            apply_observed_node_fault("memory_leak", LABEL_MEMORY_LEAK, "memory_usage", memory_increased)

        if "cpu_stress" in self.fault_windows:
            def cpu_increased(rows, baseline, mad_value):
                threshold = max(0.05, baseline * 1.5, baseline + 4.4478 * mad_value)
                return pd.to_numeric(rows["cpu_usage"], errors="coerce") >= threshold
            apply_observed_node_fault("cpu_stress", LABEL_CPU_STRESS, "cpu_usage", cpu_increased)

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

    def run(self, csv_path: Path, output_path: Path = None, session_id: str = None):
        df = self.load_raw_csv(csv_path)
        logger.info("Applying labels...")
        df = self.apply_labels(df)
        if session_id:
            df["session_id"] = str(session_id)
            logger.info(f"  Session ID: {session_id}")
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


def windows_from_fault_log(log_path: Path, expected_session_id: str = None) -> dict:
    """Read precise UTC start/end markers emitted by run_faults.sh."""
    pattern = re.compile(
        r"XFSCI_FAULT_EVENT session_id=(?P<session>\S+) fault=(?P<fault>\S+) "
        r"phase=(?P<phase>start|end) timestamp=(?P<timestamp>\S+) targets=(?P<targets>\S+)"
        r"(?: node=(?P<node>\S+))?"
    )
    crash_pattern = re.compile(
        r"XFSCI_POD_EVENT session_id=(?P<session>\S+) fault=pod_crash "
        r"timestamp=(?P<timestamp>\S+) service=(?P<service>\S+) "
        r"pod=(?P<pod>\S+) uid=(?P<uid>\S+) node=(?P<node>\S+) reason=(?P<reason>\S+)"
    )
    events = {}
    crash_events = []
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            crash_match = crash_pattern.search(line)
            if crash_match:
                item = crash_match.groupdict()
                if expected_session_id and item["session"] != expected_session_id:
                    if item["session"] == "manual":
                        logger.warning("Crash event session is 'manual'; accepting for the requested session")
                    else:
                        raise ValueError(
                            f"Crash event session {item['session']!r} does not match "
                            f"--session-id {expected_session_id!r}"
                        )
                if item["service"] not in POD_CRASH_TARGETS:
                    raise ValueError(f"Crash event has an unapproved service target: {item['service']!r}")
                if get_pod_prefix(item["pod"]) != item["service"] or not item["uid"]:
                    raise ValueError("Crash event pod/service identity is inconsistent or UID is missing")
                crash_events.append({
                    "timestamp": _utc_naive(item["timestamp"]),
                    "service": item["service"],
                    "pod": item["pod"],
                    "uid": item["uid"],
                    "node_name": item["node"],
                    "reason": item["reason"],
                })
                continue
            match = pattern.search(line)
            if not match:
                continue
            item = match.groupdict()
            if expected_session_id and item["session"] != expected_session_id:
                if item["session"] == "manual":
                    logger.warning(
                        f"Fault log session is 'manual' (default); accepting for {expected_session_id}"
                    )
                else:
                    raise ValueError(
                        f"Fault log session {item['session']!r} does not match "
                        f"--session-id {expected_session_id!r}"
                    )
            fault = item["fault"]
            events.setdefault(fault, {})[item["phase"]] = {
                "time": _utc_naive(item["timestamp"]),
                "target_pods": [name for name in item["targets"].split(",") if name],
                "node_name": "" if item.get("node") in (None, "-") else item.get("node"),
            }

    required = {"cpu_stress", "memory_leak", "pod_crash", "net_latency"}
    missing = required - set(events)
    if missing:
        raise ValueError(f"Fault log is missing scenarios: {sorted(missing)}")

    windows = {}
    for fault in sorted(required):
        phases = events[fault]
        if "start" not in phases or "end" not in phases:
            raise ValueError(f"Fault log has incomplete start/end markers for {fault}")
        if phases["end"]["time"] <= phases["start"]["time"]:
            raise ValueError(f"Fault log end time is not after start time for {fault}")
        if phases["start"]["target_pods"] != phases["end"]["target_pods"]:
            raise ValueError(f"Fault target list changed during {fault}")
        if phases["start"].get("node_name") != phases["end"].get("node_name"):
            raise ValueError(f"Fault injector node changed during {fault}")
        windows[fault] = {
            "start": phases["start"]["time"],
            "end": phases["end"]["time"],
            "target_pods": phases["start"]["target_pods"],
                "node_name": phases["start"].get("node_name"),
            }
    windows["pod_crash"]["events"] = crash_events
    for event in crash_events:
        if not windows["pod_crash"]["start"] <= event["timestamp"] <= windows["pod_crash"]["end"]:
            raise ValueError(f"Crash event for {event['pod']} falls outside the recorded pod_crash window")
    if not crash_events:
        logger.warning("Fault log contains no exact pod-delete events; crash rows will remain unknown")
    return windows


def main():
    parser = argparse.ArgumentParser(description="XFSCI Data Labeler - Fase 3A (Multi-Session)")
    parser.add_argument("--input",       type=str, default=None, help="Path CSV input")
    parser.add_argument("--output",      type=str, default=None, help="Path CSV output")
    parser.add_argument("--interactive", action="store_true",    help="Input waktu fault manual")
    parser.add_argument("--fault-log", type=str, default=None,
                        help="Log run_faults.sh dengan marker waktu presisi")
    parser.add_argument("--session-id", type=str, default=None,
                        help="ID sesi independen untuk session-aware split")
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
    if bool(args.fault_log) != bool(args.session_id):
        parser.error("--fault-log and --session-id must be provided together")

    if args.fault_log:
        fault_windows = windows_from_fault_log(Path(args.fault_log), args.session_id)
        logger.info(f"Loaded precise fault windows from {args.fault_log}")
    elif args.interactive:
        fault_windows = interactive_input()
    else:
        fault_windows = auto_detect_windows(csv_path, session=args.session)

    # Simpan fault_windows.json
    windows_json = PROCESSED_DIR / "fault_windows.json"
    serialized = {
        k: {
            "start": str(v["start"]), "end": str(v["end"]),
            "target_pods": v.get("target_pods"), "node_name": v.get("node_name"),
            "events": [
                {**event, "timestamp": str(event["timestamp"])}
                for event in v.get("events", [])
            ],
        }
        for k, v in fault_windows.items()
    }
    if args.session_id:
        serialized["session_id"] = args.session_id
    windows_json.write_text(json.dumps(serialized, indent=2))
    logger.info(f"Windows saved: {windows_json.name}")

    # Jalankan
    output_path = Path(args.output) if args.output else None
    labeler = XFSCIDataLabeler(fault_windows)
    df, out = labeler.run(csv_path, output_path, session_id=args.session_id)

    logger.success("Labeling selesai!")
    logger.info("Next: python data/preprocessors/data_cleaner.py")


if __name__ == "__main__":
    main()

