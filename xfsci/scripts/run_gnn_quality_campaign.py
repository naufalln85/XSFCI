#!/usr/bin/env python3
"""Bounded, logged multi-session XFSCI GNN data and evaluation campaign.

Runs only against namespace ``demo``. It collects independent sessions, labels
fault intervals from run_faults.sh event markers, trains only after at least
three sessions, and stops at the first candidate that clears every test gate
or when --max-sessions is reached. It does not start the SRE orchestrator.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
FAULT_RUNNER = ROOT / "infrastructure" / "fault-injection" / "run_faults.sh"
LABELER = ROOT / "data" / "preprocessors" / "data_labeler.py"
CLEANER = ROOT / "data" / "preprocessors" / "data_cleaner.py"
FEATURE_ENGINEER = ROOT / "data" / "preprocessors" / "feature_engineer.py"
TRAINER = ROOT / "models" / "gnn" / "train_gnn.py"
LABELS = [
    "NORMAL",
    "FAULT_CPU_STRESS",
    "FAULT_MEMORY_LEAK",
    "FAULT_POD_CRASH",
    "FAULT_NETWORK_LATENCY",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(message: str) -> None:
    print(f"[{utc_now()}] {message}", flush=True)


def run_capture(command: list[str], log_path: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=ROOT, env=env, text=True, capture_output=True, check=False
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.write(result.stdout)
        log.write(result.stderr)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}. "
            f"See {log_path}"
        )
    return result.stdout.strip()


def run_logged(command: list[str], log_path: Path, env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    say(f"START {log_path.name}: {' '.join(command)}")
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"START_UTC={utc_now()}\nCOMMAND={' '.join(command)}\n")
        log.flush()
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        log.write(f"END_UTC={utc_now()}\nEXIT_CODE={result.returncode}\n")
    say(f"END {log_path.name}: exit={result.returncode}; full log: {log_path}")
    return result.returncode


def process_alive_with_orchestrator() -> list[str]:
    found = []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except OSError:
            continue
        if (
            "agent.orchestrator" in cmdline
            or "agent.orchestration" in cmdline
            or "data/collectors/metrics_scraper.py" in cmdline
        ):
            found.append(f"pid={entry.name} cmd={cmdline.strip()}")
    return found


def prometheus_has_traces(prometheus_url: str) -> bool:
    query = 'count(xfsci_calls_total{k8s_namespace_name="demo",span_kind="SPAN_KIND_SERVER"})'
    url = prometheus_url.rstrip("/") + "/api/v1/query?" + urlencode({"query": query})
    with urlopen(url, timeout=10) as response:
        body = json.load(response)
    return body.get("status") == "success" and bool(body.get("data", {}).get("result"))


def preflight(args: argparse.Namespace, env: dict[str, str], campaign_dir: Path) -> None:
    say("PRE-FLIGHT: checking context, demo namespace, injector permissions, Prometheus, and frontend")
    context = run_capture(["kubectl", "config", "current-context"], campaign_dir / "preflight.log", env)
    if context != args.expected_context:
        raise RuntimeError(
            f"Kubernetes context mismatch: active={context!r}, expected={args.expected_context!r}. "
            "No fault injection was started."
        )
    namespace = run_capture(
        ["kubectl", "get", "namespace", "demo", "-o", "jsonpath={.metadata.name}"],
        campaign_dir / "preflight.log", env,
    )
    if namespace != "demo":
        raise RuntimeError("Required namespace demo was not found.")
    node_json = json.loads(run_capture(
        ["kubectl", "get", "nodes", "-o", "json"], campaign_dir / "preflight.log", env
    ))
    sites = {
        item.get("metadata", {}).get("labels", {}).get("xfsci-site")
        for item in node_json.get("items", [])
        if item.get("metadata", {}).get("labels", {}).get("xfsci-site")
    }
    if not {"bandung", "surabaya"}.issubset(sites):
        raise RuntimeError(
            f"Fault manifests require nodes labeled xfsci-site=bandung and surabaya; found {sorted(sites)}."
        )
    permissions = [
        "create pods", "delete pods", "create cronjobs.batch", "delete cronjobs.batch",
        "create serviceaccounts", "delete serviceaccounts",
        "create roles.rbac.authorization.k8s.io", "delete roles.rbac.authorization.k8s.io",
        "create rolebindings.rbac.authorization.k8s.io", "delete rolebindings.rbac.authorization.k8s.io",
    ]
    for permission in permissions:
        result = run_capture(
            ["kubectl", "auth", "can-i", *permission.split(), "-n", "demo"],
            campaign_dir / "preflight.log", env,
        )
        if result.lower() != "yes":
            raise RuntimeError(f"Kubernetes permission denied: {permission} in demo.")
    if not prometheus_has_traces(args.prometheus_url):
        raise RuntimeError("Prometheus has no xfsci_calls_total server traces for demo.")
    from agent.pandas_processor import PandasMetricProcessor
    from models.gnn.feature_contract import FEATURE_PIPELINE_VERSION, MODEL_FEATURE_COLS
    from models.gnn.graph_dataset import SERVICE_NAMES

    contract_path = PROCESSED_DIR / "feature_contract.json"
    if not contract_path.exists():
        raise RuntimeError(f"Feature contract is missing: {contract_path}")
    saved_version = json.loads(contract_path.read_text(encoding="utf-8")).get("version")
    if saved_version != FEATURE_PIPELINE_VERSION:
        raise RuntimeError(
            f"Code/processed feature contract mismatch: source={FEATURE_PIPELINE_VERSION!r}, "
            f"processed={saved_version!r}. Reconcile the VM's v4 branch before collecting."
        )

    processor = PandasMetricProcessor()
    try:
        snapshot, status = processor.get_gnn_feature_snapshot()
    except Exception as exc:
        raise RuntimeError(
            f"Read-only live feature snapshot failed: {exc}; "
            f"status={processor.last_gnn_snapshot_status}"
        ) from exc
    feature_count = len(next(iter(snapshot.values()))) if snapshot else 0
    if not status.get("available") or len(snapshot) != len(SERVICE_NAMES) or feature_count != len(MODEL_FEATURE_COLS):
        raise RuntimeError(
            "Live feature contract is not ready: "
            f"available={status.get('available')} services={len(snapshot)}/{len(SERVICE_NAMES)} "
            f"features={feature_count}/{len(MODEL_FEATURE_COLS)} status={status}"
        )
    say(
        f"Read-only live telemetry PASS: pipeline={FEATURE_PIPELINE_VERSION} "
        f"services={len(snapshot)} features={feature_count}; no inference was run."
    )
    request = Request(args.frontend_url.rstrip("/") + "/", method="GET")
    with urlopen(request, timeout=10) as response:
        if response.status >= 400:
            raise RuntimeError(f"Frontend health request returned HTTP {response.status}.")
    running = process_alive_with_orchestrator()
    if running:
        details = "\n".join(running)
        raise RuntimeError(
            "An XFSCI orchestrator or metrics scraper process appears active. Stop autonomous "
            "remediation and duplicate collection before "
            f"fault collection:\n{details}"
        )
    say(f"PRE-FLIGHT PASS: context={context}; namespace=demo; node sites={sorted(sites)}")


def transaction_traffic(
    frontend_url: str, count: int, interval: int, log_path: Path, stop_event: threading.Event
) -> None:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    base = frontend_url.rstrip("/") + "/"
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"START_UTC={utc_now()}\nMAX_TRANSACTIONS={count}\nINTERVAL_SECONDS={interval}\n")
        for transaction in range(1, count + 1):
            statuses = []
            try:
                for path, data in [
                    ("", None),
                    ("product/OLJCESPC7Z", None),
                    ("cart", {"product_id": "OLJCESPC7Z", "quantity": "1"}),
                    ("cart/checkout", {
                        "email": "xfsci-test@example.invalid",
                        "street_address": "5 Lab Street",
                        "zip_code": "12345",
                        "city": "Jakarta",
                        "state": "DKI",
                        "country": "Indonesia",
                        "credit_card_number": "4242424242424242",
                        "credit_card_expiration_month": "12",
                        "credit_card_expiration_year": "2030",
                        "credit_card_cvv": "123",
                    }),
                ]:
                    body = urlencode(data).encode() if data else None
                    request = Request(urljoin(base, path), data=body, method="POST" if body else "GET")
                    with opener.open(request, timeout=10) as response:
                        statuses.append(str(response.status))
                log.write(f"{utc_now()} transaction={transaction}/{count} http={','.join(statuses)}\n")
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                log.write(f"{utc_now()} transaction={transaction}/{count} ERROR={exc}\n")
            log.flush()
            if transaction < count and stop_event.wait(interval):
                break
        log.write(f"END_UTC={utc_now()}\n")


def wait_for_process(
    proc: subprocess.Popen, label: str, log_path: Path,
    monitor: subprocess.Popen | None = None, monitor_label: str = "peer process",
) -> int:
    started = time.monotonic()
    next_report = started + 60
    while proc.poll() is None:
        time.sleep(2)
        if monitor is not None and monitor.poll() is not None:
            raise RuntimeError(
                f"{monitor_label} exited early with code {monitor.returncode}; "
                f"stopping {label}. See {log_path}"
            )
        if time.monotonic() >= next_report:
            elapsed = int((time.monotonic() - started) / 60)
            say(f"{label} still running ({elapsed} min); full log: {log_path}")
            next_report = time.monotonic() + 60
    return int(proc.returncode or 0)


def stop_child(proc: subprocess.Popen | None, label: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    say(f"Stopping {label} with SIGINT so it can run its cleanup/save path")
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=45)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def collect_session(
    index: int, session_id: str, session_dir: Path, args: argparse.Namespace,
    env: dict[str, str],
) -> tuple[Path, Path]:
    before = set(RAW_DIR.glob("metrics_*.csv"))
    scraper_log = session_dir / "scraper.log"
    fault_log = session_dir / "faults.log"
    traffic_log = session_dir / "traffic.log"
    stop_event = threading.Event()
    traffic_thread = threading.Thread(
        target=transaction_traffic,
        args=(args.frontend_url, args.transactions, args.traffic_interval, traffic_log, stop_event),
        daemon=True,
    )
    scraper_file = scraper_log.open("w", encoding="utf-8")
    scraper_file.write(f"START_UTC={utc_now()}\n")
    scraper_file.flush()
    scraper = subprocess.Popen(
        [sys.executable, str(ROOT / "data" / "collectors" / "metrics_scraper.py"),
         "--interval", "5", "--duration", str(args.scrape_minutes),
         "--prometheus-url", args.prometheus_url],
        cwd=ROOT, env=env, stdout=scraper_file, stderr=subprocess.STDOUT,
    )
    fault = None
    try:
        time.sleep(8)
        if scraper.poll() is not None:
            raise RuntimeError(f"Metrics scraper exited early; inspect {scraper_log}")
        traffic_thread.start()
        launch_delay = ((index - 1) % 5) * 45
        if launch_delay:
            say(f"Session {index}: varying capture-to-fault baseline by {launch_delay}s")
            time.sleep(launch_delay)
        fault_file = fault_log.open("w", encoding="utf-8")
        fault_file.write(f"START_UTC={utc_now()}\nSESSION_ID={session_id}\n")
        fault_file.flush()
        session_env = env.copy()
        session_env["XFSCI_SESSION_ID"] = session_id
        fault = subprocess.Popen(
            ["bash", str(FAULT_RUNNER)], cwd=ROOT, env=session_env,
            stdout=fault_file, stderr=subprocess.STDOUT,
        )
        fault_file.close()
        fault_rc = wait_for_process(
            fault, f"fault injector {session_id}", fault_log,
            monitor=scraper, monitor_label=f"metrics scraper {session_id}",
        )
        if fault_rc != 0:
            raise RuntimeError(f"Fault injector failed with exit={fault_rc}; inspect {fault_log}")
        if scraper.poll() is None:
            scrape_rc = wait_for_process(scraper, f"metrics scraper {session_id}", scraper_log)
        else:
            scrape_rc = int(scraper.returncode or 0)
        if scrape_rc != 0:
            raise RuntimeError(f"Metrics scraper failed with exit={scrape_rc}; inspect {scraper_log}")
        traffic_thread.join(timeout=10)
        stop_event.set()
        created = sorted(set(RAW_DIR.glob("metrics_*.csv")) - before, key=lambda path: path.stat().st_mtime)
        if len(created) != 1:
            raise RuntimeError(f"Expected one new metrics CSV, found {len(created)}: {created}")
        raw_copy = session_dir / created[0].name
        shutil.copy2(created[0], raw_copy)
        say(f"Session {session_id}: saved raw telemetry to {raw_copy}")
        return raw_copy, fault_log
    except KeyboardInterrupt:
        stop_event.set()
        stop_child(fault, "fault injector")
        stop_child(scraper, "metrics scraper")
        raise
    except Exception:
        stop_event.set()
        stop_child(fault, "fault injector")
        stop_child(scraper, "metrics scraper")
        raise
    finally:
        scraper_file.close()
        if traffic_thread.is_alive():
            stop_event.set()
            traffic_thread.join(timeout=10)


def archive_active_model(campaign_dir: Path) -> None:
    weights_dir = ROOT / "models" / "gnn" / "weights"
    weights_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = campaign_dir / "pre_campaign_model_DO_NOT_USE_FOR_REMEDIATION"
    archive_weights = archive_dir / "weights"
    archive_data = archive_dir / "processed"
    archive_weights.mkdir(parents=True, exist_ok=True)
    archive_data.mkdir(parents=True, exist_ok=True)
    for name in ("gnn_best.pt", "gnn_training_candidate.pt", "training_metrics.json"):
        source = weights_dir / name
        if source.exists():
            target = archive_weights / name
            shutil.move(str(source), str(target))
            say(f"Archived existing {name} at {target}; it is not treated as validated.")
    for name in ("scaler_params.json", "feature_contract.json", "dataset_ready.csv"):
        source = PROCESSED_DIR / name
        if source.exists():
            target = archive_data / name
            shutil.copy2(source, target)
            say(f"Preserved previous {name} at {target} before the new training pipeline overwrites it.")


def move_candidate(weights_dir: Path, destination: Path, prefix: str = "rejected_") -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("gnn_best.pt", "gnn_training_candidate.pt", "training_metrics.json"):
        source = weights_dir / name
        if source.exists():
            target = destination / (prefix + name)
            if target.exists():
                target.unlink()
            shutil.move(str(source), str(target))


def evaluate_candidate(metrics: dict, args: argparse.Namespace) -> list[str]:
    reasons = []
    support = metrics.get("test_class_support", {})
    if not isinstance(support, dict):
        support = {}
    for label in LABELS:
        if label not in support:
            reasons.append(f"{label} test support is missing")
            continue
        count = int(support.get(label, 0))
        if count < args.min_test_support:
            reasons.append(f"{label} test support {count} < {args.min_test_support}")
    checks = [
        ("cluster_anomaly_accuracy", args.min_cluster_accuracy),
        ("top3_rca_accuracy", args.min_top3_accuracy),
        ("fault_macro_f1", args.min_fault_macro_f1),
    ]
    for key, threshold in checks:
        if key not in metrics:
            reasons.append(f"{key} is missing")
            continue
        value = float(metrics[key])
        if not math.isfinite(value):
            reasons.append(f"{key} is not finite")
        elif value < threshold:
            reasons.append(f"{key} {value:.4f} < {threshold:.4f}")
    return reasons


def training_dataset_check(cleaned_files: list[Path], session_ids: list[str], campaign_dir: Path):
    from models.gnn.session_split import split_session_ids

    merged_path = campaign_dir / "cleaned_all_sessions.csv"
    frames = [pd.read_csv(path) for path in cleaned_files]
    merged = pd.concat(frames, ignore_index=True).sort_values(["timestamp", "pod_name"])
    merged.to_csv(merged_path, index=False)
    _, _, test_sessions = split_session_ids(session_ids)
    test_rows = merged[merged["session_id"].astype(str).isin(test_sessions)]
    counts = test_rows["label"].value_counts().to_dict()
    return merged_path, test_sessions, {label: int(counts.get(label, 0)) for label in LABELS}


def save_summary(campaign_dir: Path, summary: dict) -> None:
    path = campaign_dir / "campaign_summary.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    from models.gnn.feature_contract import FEATURE_PIPELINE_VERSION

    parser = argparse.ArgumentParser(description="Bounded XFSCI multi-session GNN quality campaign")
    parser.add_argument("--expected-context", required=True, help="Exact output of kubectl config current-context")
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--frontend-url", required=True)
    parser.add_argument("--confirm-demo", action="store_true", required=True,
                        help="Confirm that namespace demo can be disrupted for controlled fault collection")
    parser.add_argument("--confirm-agent-stopped", action="store_true", required=True,
                        help="Confirm autonomous remediation/orchestrator is stopped")
    parser.add_argument("--allow-node-network-fault", action="store_true", required=True,
                        help="Acknowledge that existing netem injection changes the Surabaya node network")
    parser.add_argument("--max-sessions", type=int, default=7, choices=range(3, 11), metavar="3..10")
    parser.add_argument("--min-test-sessions", type=int, default=2, choices=range(1, 4), metavar="1..3")
    parser.add_argument("--scrape-minutes", type=int, default=65, choices=range(63, 91), metavar="63..90")
    parser.add_argument("--transactions", type=int, default=24, choices=range(1, 61), metavar="1..60")
    parser.add_argument("--traffic-interval", type=int, default=15, choices=range(10, 121), metavar="10..120")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--patience", type=int, default=12)
    parser.add_argument("--min-test-support", type=int, default=20)
    parser.add_argument("--min-cluster-accuracy", type=float, default=0.99)
    parser.add_argument("--min-top3-accuracy", type=float, default=0.98)
    parser.add_argument("--min-fault-macro-f1", type=float, default=0.80)
    parser.add_argument("--resume-campaign", type=str, default=None,
                        help="Resume an existing campaign ID by reusing already collected sessions")
    args = parser.parse_args()

    if args.scrape_minutes < 63:
        parser.error("scrape-minutes must cover the fault sequence, maximum varied delay, and final recovery")
    if args.min_test_sessions >= 2 and args.max_sessions < 7:
        parser.error("at least 7 sessions are needed for a session-level test split with two held-out sessions")
    if not 0.0 <= args.min_cluster_accuracy <= 1.0 or not 0.0 <= args.min_top3_accuracy <= 1.0:
        parser.error("accuracy thresholds must be between 0 and 1")
    if not 0.0 <= args.min_fault_macro_f1 <= 1.0:
        parser.error("min-fault-macro-f1 must be between 0 and 1")

    if args.resume_campaign:
        campaign_id = args.resume_campaign
        campaign_dir = ROOT / "artifacts" / "gnn_quality_campaigns" / campaign_id
        if not campaign_dir.exists():
            raise FileNotFoundError(f"Campaign directory does not exist: {campaign_dir}")
        summary_path = campaign_dir / "campaign_summary.json"
        summary = {}
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                summary = {}
        if not summary:
            summary = {
                "campaign_id": campaign_id,
                "started_utc": utc_now(),
                "namespace": "demo",
                "feature_pipeline_version": FEATURE_PIPELINE_VERSION,
                "max_sessions": args.max_sessions,
                "min_test_sessions": args.min_test_sessions,
                "thresholds": {
                    "min_test_support": args.min_test_support,
                    "min_cluster_accuracy": args.min_cluster_accuracy,
                    "min_top3_rca_accuracy": args.min_top3_accuracy,
                    "min_fault_macro_f1": args.min_fault_macro_f1,
                },
                "sessions": [],
                "status": "running",
            }
        summary["status"] = "running"
        save_summary(campaign_dir, summary)
    else:
        campaign_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        campaign_dir = ROOT / "artifacts" / "gnn_quality_campaigns" / campaign_id
        campaign_dir.mkdir(parents=True, exist_ok=False)
        summary = {
            "campaign_id": campaign_id,
            "started_utc": utc_now(),
            "namespace": "demo",
            "feature_pipeline_version": FEATURE_PIPELINE_VERSION,
            "max_sessions": args.max_sessions,
            "min_test_sessions": args.min_test_sessions,
            "thresholds": {
                "min_test_support": args.min_test_support,
                "min_cluster_accuracy": args.min_cluster_accuracy,
                "min_top3_rca_accuracy": args.min_top3_accuracy,
                "min_fault_macro_f1": args.min_fault_macro_f1,
            },
            "sessions": [],
            "status": "running",
        }
        save_summary(campaign_dir, summary)

    env = os.environ.copy()
    env.update({"TARGET_NAMESPACE": "demo", "TZ": "UTC", "PYTHONUNBUFFERED": "1"})

    say(f"Campaign {campaign_id}; logs/artifacts: {campaign_dir}")
    say("Each session runs the bounded standard injector (~56 minutes), one scraper, and at most the configured checkout count.")
    say("Expected total time can exceed 7.5 hours at the default 7 sessions. Use tmux and watch campaign.log.")

    if not args.confirm_demo or not args.confirm_agent_stopped or not args.allow_node_network_fault:
        raise RuntimeError("Required safety confirmations are missing; no cluster changes were made.")

    main_log = campaign_dir / "campaign.log"
    old_stdout = sys.stdout
    class Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, data):
            for stream in self.streams:
                stream.write(data)
                stream.flush()
        def flush(self):
            for stream in self.streams: stream.flush()
    log_handle = main_log.open("a", encoding="utf-8")
    sys.stdout = Tee(old_stdout, log_handle)
    try:
        preflight(args, env, campaign_dir)
        collected_raw: list[Path] = []
        fault_logs: list[Path] = []
        cleaned_files: list[Path] = []
        weights_dir = ROOT / "models" / "gnn" / "weights"
        archive_active_model(campaign_dir)

        for index in range(1, args.max_sessions + 1):
            session_id = f"{campaign_id}_s{index:02d}"
            session_dir = campaign_dir / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            say(f"========== SESSION {index}/{args.max_sessions}: {session_id} ==========")

            existing_cleaned = session_dir / f"cleaned_{session_id}.csv"
            existing_raw = sorted(session_dir.glob("metrics_*.csv"), key=lambda p: p.stat().st_mtime)
            existing_fault = session_dir / "faults.log"

            if existing_cleaned.exists() and existing_cleaned.stat().st_size > 0:
                raw_csv = existing_raw[-1] if existing_raw else session_dir / "metrics.csv"
                fault_log = existing_fault
                cleaned_csv = existing_cleaned
                say(f"Session {session_id}: reusing existing cleaned dataset {cleaned_csv.name}")
                collected_raw.append(raw_csv)
                fault_logs.append(fault_log)
                cleaned_files.append(cleaned_csv)
            elif existing_raw and existing_fault.exists() and existing_fault.stat().st_size > 0:
                raw_csv = existing_raw[-1]
                fault_log = existing_fault
                say(f"Session {session_id}: reusing existing raw telemetry {raw_csv.name} and fault log")
                collected_raw.append(raw_csv)
                fault_logs.append(fault_log)
                labeled_csv = session_dir / f"labeled_{session_id}.csv"
                rc = run_logged(
                    [sys.executable, str(LABELER), "--input", str(raw_csv), "--output", str(labeled_csv),
                     "--fault-log", str(fault_log), "--session-id", session_id],
                    session_dir / "labeler.log", env,
                )
                if rc:
                    raise RuntimeError(f"Labeling failed for {session_id}; inspect {session_dir / 'labeler.log'}")

                cleaned_csv = session_dir / f"cleaned_{session_id}.csv"
                rc = run_logged(
                    [sys.executable, str(CLEANER), "--input", str(labeled_csv), "--output", str(cleaned_csv)],
                    session_dir / "cleaner.log", env,
                )
                if rc:
                    raise RuntimeError(f"Cleaning failed for {session_id}; inspect {session_dir / 'cleaner.log'}")
                cleaned_files.append(cleaned_csv)
            else:
                raw_csv, fault_log = collect_session(index, session_id, session_dir, args, env)
                collected_raw.append(raw_csv)
                fault_logs.append(fault_log)

                labeled_csv = session_dir / f"labeled_{session_id}.csv"
                rc = run_logged(
                    [sys.executable, str(LABELER), "--input", str(raw_csv), "--output", str(labeled_csv),
                     "--fault-log", str(fault_log), "--session-id", session_id],
                    session_dir / "labeler.log", env,
                )
                if rc:
                    raise RuntimeError(f"Labeling failed for {session_id}; inspect {session_dir / 'labeler.log'}")

                cleaned_csv = session_dir / f"cleaned_{session_id}.csv"
                rc = run_logged(
                    [sys.executable, str(CLEANER), "--input", str(labeled_csv), "--output", str(cleaned_csv)],
                    session_dir / "cleaner.log", env,
                )
                if rc:
                    raise RuntimeError(f"Cleaning failed for {session_id}; inspect {session_dir / 'cleaner.log'}")
                cleaned_files.append(cleaned_csv)

            session_record = {
                "session_id": session_id,
                "raw_csv": str(raw_csv),
                "fault_log": str(fault_log),
                "labeled_csv": str(session_dir / f"labeled_{session_id}.csv"),
                "cleaned_csv": str(cleaned_csv),
                "finished_utc": utc_now(),
            }
            for i, r in enumerate(summary["sessions"]):
                if r.get("session_id") == session_id:
                    summary["sessions"][i] = session_record
                    break
            else:
                summary["sessions"].append(session_record)
            save_summary(campaign_dir, summary)

            if len(cleaned_files) < 3:
                say("At least 3 complete sessions are required for train/validation/test; collecting the next one.")
                continue

            merged_csv, test_sessions, supports = training_dataset_check(
                cleaned_files, [record["session_id"] for record in summary["sessions"]], campaign_dir
            )
            summary["test_sessions"] = test_sessions
            summary["test_raw_support"] = supports
            say(f"Held-out session(s): {test_sessions}; raw label support: {supports}")
            if len(test_sessions) < args.min_test_sessions:
                summary["last_rejection"] = (
                    f"test split has {len(test_sessions)} independent session(s); "
                    f"need {args.min_test_sessions}"
                )
                say(f"HELD-OUT SESSION COUNT GATE FAILED; collecting more sessions (need {args.min_test_sessions}).")
                save_summary(campaign_dir, summary)
                continue
            if any(supports[label] < args.min_test_support for label in LABELS):
                summary["last_rejection"] = "held-out session lacks minimum raw label support"
                say("HELD-OUT DATA GATE FAILED; collecting a new independent session before training.")
                save_summary(campaign_dir, summary)
                continue

            feature_log = campaign_dir / f"feature_engineer_after_{index:02d}_sessions.log"
            rc = run_logged(
                [sys.executable, str(FEATURE_ENGINEER), "--input", str(merged_csv)],
                feature_log, env,
            )
            if rc:
                raise RuntimeError(f"Feature engineering failed; inspect {feature_log}")

            metrics_path = weights_dir / "training_metrics.json"
            if metrics_path.exists():
                metrics_path.unlink()
            attempt_dir = campaign_dir / f"attempt_{index:02d}"
            train_log = attempt_dir / "training.log"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            rc = run_logged(
                [sys.executable, str(TRAINER), "--epochs", str(args.epochs), "--patience", str(args.patience)],
                train_log, env,
            )
            metrics = {}
            if metrics_path.exists():
                try:
                    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    metrics = {}
            reasons = [f"trainer exit code {rc}"] if rc else []
            if not metrics:
                reasons.append("training_metrics.json missing or invalid")
            else:
                reasons.extend(evaluate_candidate(metrics, args))
            session_record.update({"training_metrics": metrics, "rejection_reasons": reasons})
            summary["last_metrics"] = metrics
            if not reasons:
                session_record["accepted"] = True
                accepted_dir = campaign_dir / "accepted_candidate"
                move_candidate(weights_dir, accepted_dir, prefix="accepted_")
                session_record["candidate_dir"] = str(accepted_dir)
                summary.update({"status": "quality_gate_passed", "accepted_session_count": index,
                                "accepted_metrics": metrics, "completed_utc": utc_now()})
                save_summary(campaign_dir, summary)
                say(f"QUALITY GATES PASSED: {metrics}; candidate and metrics stored at {accepted_dir}")
                say("It has not been copied into the runtime model path or enabled for autonomous remediation.")
                return 0
            session_record["accepted"] = False
            move_candidate(weights_dir, attempt_dir)
            summary["last_rejection"] = reasons
            save_summary(campaign_dir, summary)
            say("Candidate rejected: " + "; ".join(reasons))
            say(f"Rejected model/metrics archived under {attempt_dir}; collecting a fresh session.")

        summary.update({"status": "session_limit_reached", "completed_utc": utc_now()})
        save_summary(campaign_dir, summary)
        say(f"STOPPED at max_sessions={args.max_sessions}; no candidate cleared every gate.")
        say(f"Review {campaign_dir / 'campaign_summary.json'} and per-step logs before another campaign.")
        return 2
    except KeyboardInterrupt:
        if (campaign_dir / "pre_campaign_model_DO_NOT_USE_FOR_REMEDIATION").exists():
            move_candidate(ROOT / "models" / "gnn" / "weights", campaign_dir / "interrupted_candidate")
        summary.update({"status": "interrupted", "completed_utc": utc_now()})
        save_summary(campaign_dir, summary)
        say("Campaign interrupted; fault runner cleanup and scraper save were requested. Review all session logs.")
        return 130
    except Exception as exc:
        if (campaign_dir / "pre_campaign_model_DO_NOT_USE_FOR_REMEDIATION").exists():
            move_candidate(ROOT / "models" / "gnn" / "weights", campaign_dir / "unvalidated_candidate")
        summary.update({"status": "failed", "error": str(exc), "traceback": traceback.format_exc(),
                        "completed_utc": utc_now()})
        save_summary(campaign_dir, summary)
        say(f"CAMPAIGN FAILED: {exc}")
        say(f"Full details: {campaign_dir / 'campaign_summary.json'}")
        return 1
    finally:
        sys.stdout = old_stdout
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
