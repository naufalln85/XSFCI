# ============================================================
# XFSCI Pandas Metric Processor
# ============================================================
# Modul ini mengolah data mentah dari Prometheus/Loki menjadi
# metrik terstruktur dengan perhitungan deterministik; kualitas bergantung pada sumber telemetry.
#
# MENGAPA PAKAI PANDAS, BUKAN LLM?
# - Pandas menghitung angka secara EKSAK (tidak bisa halusinasi)
# - Rata-rata CPU, growth rate memory, dll = aritmatika murni
# - Output adalah JSON numerik, bukan teks generatif
#
# Cara kerja:
# 1. Query Prometheus API untuk mendapatkan metrik raw
# 2. Pandas DataFrame mengolah data (aggregasi, rate, dll)
# 3. Output: PandasMetrics (Pydantic schema) → dikirim ke AI Agent
# ============================================================

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np
import requests
import yaml
from pathlib import Path
from loguru import logger

from agent.action_schema import PandasMetrics, AnomalyType


class PandasMetricProcessor:
    """
    Mengolah data metrik dari Prometheus menjadi laporan numerik
    yang dihitung secara deterministik dari sumber telemetry.
    
    Semua output adalah angka hasil perhitungan Python/Pandas —
    TIDAK ADA satu pun output yang dihasilkan oleh LLM/AI.
    """
    
    def __init__(self, config_path: str = None):
        """
        Inisialisasi processor dengan konfigurasi.
        
        Args:
            config_path: Path ke config.yaml (opsional, auto-detect jika None)
        """
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        
        # Ambil URL Prometheus: Prioritas Environment Variable -> Config YAML
        self.prometheus_url = os.environ.get(
            "PROMETHEUS_URL",
            self.config.get("monitoring", {}).get("prometheus", {}).get("url", "http://172.20.0.104:30090")
        )
        self.target_namespace = self.config.get("data", {}).get("target_namespace", "demo")
        self._last_warn_time = 0.0
        self._gnn_scaler_params = None
        self.last_gnn_snapshot_status = {"available": False, "reason": "not_collected"}
        
        # Verifikasi & Auto-healing koneksi Prometheus
        self._setup_connection()
        
        logger.info(f"PandasMetricProcessor initialized | Prometheus: {self.prometheus_url}")

    def _test_url(self, url: str, timeout: float = 2.0) -> bool:
        """Cek apakah URL Prometheus aktif dan merespon."""
        try:
            res = requests.get(f"{url}/-/healthy", timeout=timeout)
            if res.status_code == 200:
                return True
        except Exception:
            pass
        try:
            res = requests.get(f"{url}/api/v1/query", params={"query": "1"}, timeout=timeout)
            return res.status_code == 200
        except Exception:
            return False

    def _setup_connection(self):
        """
        Deteksi dan hubungkan ke Prometheus.
        Jika URL utama (NodePort 30090) connection refused:
        1. Coba localhost:9090 / 127.0.0.1:9090
        2. Jika ada kubectl, buat auto port-forward dari cluster K8s
        """
        if self._test_url(self.prometheus_url):
            logger.info(f"✅ Koneksi Prometheus aktif di {self.prometheus_url}")
            return

        # Cek kandidat endpoint lokal
        candidates = [
            "http://127.0.0.1:9090",
            "http://localhost:9090",
            "http://127.0.0.1:30090",
            "http://localhost:30090"
        ]
        for cand in candidates:
            if cand != self.prometheus_url and self._test_url(cand):
                logger.success(f"🔄 Terhubung ke Prometheus lokal di {cand}")
                self.prometheus_url = cand
                return

        # Coba auto-tunnel via kubectl port-forward
        self._try_auto_port_forward()

    def _try_auto_port_forward(self):
        """Membuka port-forward otomatis ke service Prometheus di namespace monitoring."""
        if not shutil.which("kubectl"):
            return

        try:
            cmd = ["kubectl", "get", "svc", "-n", "monitoring", "-o", "jsonpath={.items[*].metadata.name}"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                svcs = res.stdout.strip().split()
                prom_svc = None
                for s in svcs:
                    if "prometheus-operated" in s:
                        prom_svc = s
                        break
                    elif "prometheus" in s and not any(x in s for x in ["grafana", "alertmanager", "node-exporter", "kube-state"]):
                        prom_svc = s
                
                if not prom_svc and svcs:
                    prom_svc = "prometheus-k8s"

                if prom_svc:
                    logger.info(f"🔌 Membuka terowongan otomatis via kubectl port-forward ke svc/{prom_svc} (port 9090)...")
                    subprocess.Popen(
                        ["kubectl", "port-forward", "-n", "monitoring", f"svc/{prom_svc}", "9090:9090", "--address", "127.0.0.1"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    time.sleep(2)
                    if self._test_url("http://127.0.0.1:9090"):
                        self.prometheus_url = "http://127.0.0.1:9090"
                        logger.success(f"✅ Auto port-forward berhasil! Prometheus aktif di {self.prometheus_url}")
                        return
        except Exception as e:
            logger.debug(f"Auto port-forward error: {e}")

        logger.warning(
            f"⚠️ Prometheus di {self.prometheus_url} tidak dapat dijangkau. "
            f"Jika NodePort terblokir firewall, Anda dapat menjalankan: 'kubectl port-forward -n monitoring svc/prometheus-k8s 9090:9090 &' di VM5."
        )
    
    def _query_prometheus(self, query: str) -> Optional[pd.DataFrame]:
        """
        Eksekusi PromQL query dan kembalikan sebagai DataFrame.
        
        Args:
            query: PromQL query string
        
        Returns:
            DataFrame dengan kolom [metric_labels..., value, timestamp]
        """
        try:
            response = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            if data["status"] != "success" or not data["data"]["result"]:
                return None
            
            rows = []
            for result in data["data"]["result"]:
                row = dict(result["metric"])
                row["value"] = float(result["value"][1])
                row["timestamp"] = float(result["value"][0])
                rows.append(row)
            
            return pd.DataFrame(rows)
        
        except Exception as e:
            now = time.time()
            if now - self._last_warn_time > 30:
                logger.warning(f"Prometheus query failed ({self.prometheus_url}): {e}")
                self._last_warn_time = now
            return None
    
    def _query_prometheus_range(self, query: str, duration_minutes: int = 15,
                                 step: str = "30s") -> Optional[pd.DataFrame]:
        """
        Eksekusi PromQL range query untuk data time-series.
        
        Args:
            query: PromQL query string
            duration_minutes: Berapa menit ke belakang
            step: Resolusi data
        
        Returns:
            DataFrame dengan kolom [timestamp, value, ...labels]
        """
        try:
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(minutes=duration_minutes)
            
            response = requests.get(
                f"{self.prometheus_url}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start_time.timestamp(),
                    "end": end_time.timestamp(),
                    "step": step,
                },
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            
            if data["status"] != "success" or not data["data"]["result"]:
                return None
            
            rows = []
            for result in data["data"]["result"]:
                labels = dict(result["metric"])
                for ts, val in result["values"]:
                    row = dict(labels)
                    row["timestamp"] = float(ts)
                    row["value"] = float(val)
                    rows.append(row)
            
            df = pd.DataFrame(rows)
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
            return df
        
        except Exception as e:
            now = time.time()
            if now - self._last_warn_time > 30:
                logger.warning(f"Prometheus range query failed ({self.prometheus_url}): {e}")
                self._last_warn_time = now
            return None

    def _load_gnn_scaler_params(self) -> dict:
        """Load the exact min-max parameters produced with the current feature pipeline."""
        if self._gnn_scaler_params is not None:
            return self._gnn_scaler_params
        scaler_path = Path(__file__).resolve().parent.parent / "data" / "processed" / "scaler_params.json"
        if not scaler_path.exists():
            raise RuntimeError(f"GNN scaler is missing: {scaler_path}")
        import json
        with open(scaler_path, "r", encoding="utf-8") as scaler_file:
            self._gnn_scaler_params = json.load(scaler_file)
        return self._gnn_scaler_params

    def get_gnn_feature_snapshot(self, lookback_minutes: int = 15) -> tuple[dict, dict]:
        """Build a complete live {service: 21 raw features} snapshot for GNN inference.

        Telemetry is queried by pod from Prometheus, transformed with the same
        FeatureEngineer functions used for training, then replicas are averaged
        into the canonical service nodes. Missing required telemetry fails closed.
        """
        from data.preprocessors.feature_engineer import FeatureEngineer
        from models.gnn.feature_contract import (
            APP_SPAN_SERVICES,
            APP_SPAN_ZERO_SERVICES,
            BASE_METRIC_COLS,
            FEATURE_PIPELINE_VERSION,
            MODEL_FEATURE_COLS,
            application_metric_queries,
        )
        from models.gnn.graph_dataset import SERVICE_NAMES, extract_service_name

        status = {
            "available": False,
            "feature_count": 0,
            "service_count": 0,
            "lookback_minutes": lookback_minutes,
            "degraded_features": [],
            "metric_fallbacks": {},
            "reason": "collecting",
        }
        try:
            scaler = self._load_gnn_scaler_params()
            contract_path = Path(__file__).resolve().parent.parent / "data" / "processed" / "feature_contract.json"
            if not contract_path.exists():
                raise RuntimeError("feature_contract.json is missing; rerun feature_engineer.py")
            import json
            with open(contract_path, "r", encoding="utf-8") as contract_file:
                contract = json.load(contract_file)
            if contract.get("version") != FEATURE_PIPELINE_VERSION:
                raise RuntimeError(
                    f"feature contract version mismatch: {contract.get('version')!r}; "
                    f"expected {FEATURE_PIPELINE_VERSION!r}; rerun feature_engineer.py and retrain GNN"
                )
            missing_scaler = sorted(set(MODEL_FEATURE_COLS) - set(scaler))
            if missing_scaler:
                raise RuntimeError(f"scaler schema incomplete: {missing_scaler}")

            ns = self.target_namespace
            queries = {
                "cpu_usage": f'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",container!="",container!="POD"}}[1m])) by (pod)',
                "memory_usage": f'sum(container_memory_working_set_bytes{{namespace="{ns}",container!="",container!="POD"}}) by (pod)',
                "memory_usage_percent": (
                    f'sum(container_memory_working_set_bytes{{namespace="{ns}",container!="",container!="POD"}}) by (pod) / '
                    f'sum(kube_pod_container_resource_limits{{namespace="{ns}",resource="memory",unit="byte"}}) by (pod) * 100'
                ),
                "memory_limit_cadvisor_bytes": (
                    f'sum(container_spec_memory_limit_bytes{{namespace="{ns}",container!="",container!="POD"}}) by (pod)'
                ),
                "pod_restarts": f'sum(kube_pod_container_status_restarts_total{{namespace="{ns}"}}) by (pod)',
                "net_rx_bytes": f'sum(rate(container_network_receive_bytes_total{{namespace="{ns}"}}[1m])) by (pod)',
                "net_tx_bytes": f'sum(rate(container_network_transmit_bytes_total{{namespace="{ns}"}}[1m])) by (pod)',
                **application_metric_queries(ns),
            }

            def constant_zero(metric_name: str) -> bool:
                params = scaler.get(metric_name, {})
                return float(params.get("min", 0.0)) == 0.0 and float(params.get("max", 0.0)) == 0.0

            # A source may be absent only if its training column was constant
            # zero. Preserve that training value but expose the missing source.
            app_metric_names = {"request_rate", "error_rate"}
            # Application metrics require a real trace source even when their
            # scaler happens to be constant; absence must not masquerade as 0.
            optional_zero_metrics = {
                name for name in BASE_METRIC_COLS
                if name not in app_metric_names and constant_zero(name)
            }
            with ThreadPoolExecutor(max_workers=len(queries)) as pool:
                futures = {
                    name: pool.submit(self._query_prometheus_range, query, lookback_minutes, "5s")
                    for name, query in queries.items()
                }
                results = {name: future.result() for name, future in futures.items()}

            def to_metric_frame(result, metric_name):
                if result is None or result.empty:
                    return None
                pod_key = next(
                    (name for name in ("pod", "pod_name", "k8s_pod_name") if name in result.columns),
                    None,
                )
                if pod_key is None:
                    return None
                frame = result[[pod_key, "timestamp", "value"]].copy()
                frame.rename(columns={pod_key: "pod_name", "value": metric_name}, inplace=True)
                frame["pod_name"] = frame["pod_name"].astype(str)
                frame[metric_name] = pd.to_numeric(frame[metric_name], errors="coerce")
                return frame.groupby(["pod_name", "timestamp"], as_index=False)[metric_name].mean()

            # Prefer kube-state-metrics limits. If that series is unavailable
            # or has gaps, fall back to cAdvisor's per-container limit metric.
            usage_frame = to_metric_frame(results.get("memory_usage"), "memory_usage")
            primary_pct = to_metric_frame(results.get("memory_usage_percent"), "memory_usage_percent")
            cadvisor_limit = to_metric_frame(results.get("memory_limit_cadvisor_bytes"), "memory_limit_bytes")
            fallback_pct = None
            if usage_frame is not None and cadvisor_limit is not None:
                fallback_pct = usage_frame.merge(cadvisor_limit, on=["pod_name", "timestamp"], how="inner")
                fallback_pct = fallback_pct.loc[fallback_pct["memory_limit_bytes"] > 0].copy()
                fallback_pct["memory_usage_percent"] = (
                    fallback_pct["memory_usage"] / fallback_pct["memory_limit_bytes"] * 100.0
                )
                fallback_pct = fallback_pct[["pod_name", "timestamp", "memory_usage_percent"]]

            if fallback_pct is not None and not fallback_pct.empty:
                if primary_pct is None:
                    combined_pct = fallback_pct
                    used_fallback = True
                else:
                    combined_pct = primary_pct.merge(
                        fallback_pct,
                        on=["pod_name", "timestamp"],
                        how="outer",
                        suffixes=("_primary", "_fallback"),
                    )
                    primary_values = combined_pct["memory_usage_percent_primary"]
                    fallback_values = combined_pct["memory_usage_percent_fallback"]
                    valid_primary = np.isfinite(primary_values.to_numpy(dtype=float))
                    used_fallback = bool((~valid_primary & np.isfinite(fallback_values.to_numpy(dtype=float))).any())
                    combined_pct["memory_usage_percent"] = primary_values.where(valid_primary, fallback_values)
                    combined_pct = combined_pct[["pod_name", "timestamp", "memory_usage_percent"]]
                if used_fallback:
                    status["metric_fallbacks"]["memory_usage_percent"] = "container_spec_memory_limit_bytes"
                results["memory_usage_percent"] = combined_pct.rename(
                    columns={"pod_name": "pod", "memory_usage_percent": "value"}
                )
            elif primary_pct is None or not np.isfinite(primary_pct["memory_usage_percent"].to_numpy(dtype=float)).any():
                results["memory_usage_percent"] = None

            frames = []
            for metric_name in BASE_METRIC_COLS:
                result = results.get(metric_name)
                if result is None or result.empty:
                    if metric_name in optional_zero_metrics:
                        status["degraded_features"].append(metric_name)
                        continue
                    raise RuntimeError(f"required Prometheus series unavailable: {metric_name}")
                metric_frame = to_metric_frame(result, metric_name)
                if metric_frame is None:
                    raise RuntimeError(f"Prometheus result for {metric_name} has no pod label")
                frames.append(metric_frame)

            if not frames:
                raise RuntimeError("Prometheus returned no pod metric series")
            raw = frames[0]
            for metric_frame in frames[1:]:
                raw = raw.merge(metric_frame, on=["pod_name", "timestamp"], how="outer")
            raw["service_name"] = raw["pod_name"].map(extract_service_name)

            for metric_name in app_metric_names:
                if metric_name not in raw.columns:
                    raise RuntimeError(f"required trace-derived metric missing: {metric_name}")
                missing_services = [
                    service for service in APP_SPAN_SERVICES
                    if not raw.loc[raw["service_name"] == service, metric_name].notna().any()
                ]
                if missing_services:
                    raise RuntimeError(
                        f"trace-derived {metric_name} unavailable for services: {missing_services}"
                    )

                # Redis has no server-span instrumentation in this deployment;
                # its explicit zero policy is recorded in the feature contract.
                zero_service_rows = raw["service_name"].isin(APP_SPAN_ZERO_SERVICES)
                raw.loc[zero_service_rows, metric_name] = raw.loc[
                    zero_service_rows, metric_name
                ].fillna(0.0)

            for metric_name in optional_zero_metrics:
                if metric_name not in raw.columns:
                    raw[metric_name] = 0.0
                if raw[metric_name].isna().any():
                    status["degraded_features"].append(metric_name)
                raw[metric_name] = raw[metric_name].fillna(0.0)

            for metric_name in app_metric_names.intersection(raw.columns):
                if raw[metric_name].isna().any():
                    status["degraded_features"].append(metric_name)
                    # Idle intervals with verified instrumentation have 0 request volume / errors
                    raw[metric_name] = raw[metric_name].fillna(0.0)

            raw.sort_values(["pod_name", "timestamp"], inplace=True)
            for metric_name in BASE_METRIC_COLS:
                if metric_name in optional_zero_metrics or metric_name in app_metric_names:
                    continue
                raw[metric_name] = raw.groupby("pod_name", sort=False)[metric_name].ffill(limit=1)

            valid_service = raw["pod_name"].map(extract_service_name).isin(SERVICE_NAMES)
            raw = raw.loc[valid_service].copy()
            if raw.empty:
                raise RuntimeError("no Prometheus pods map to the configured 11 services")

            # Discard incomplete samples. Keep each pod's last contiguous segment
            # so rolling and delta calculations never bridge a telemetry gap.
            raw = raw[np.isfinite(raw[BASE_METRIC_COLS].to_numpy(dtype=float)).all(axis=1)].copy()
            contiguous = []
            for _, pod_rows in raw.groupby("pod_name", sort=False):
                pod_rows = pod_rows.sort_values("timestamp").copy()
                gaps = pod_rows["timestamp"].diff() > pd.Timedelta(seconds=7.5)
                if gaps.any():
                    pod_rows = pod_rows.loc[gaps[gaps].index[-1]:]
                contiguous.append(pod_rows)
            if not contiguous:
                raise RuntimeError("no complete pod samples remain after telemetry alignment")
            raw = pd.concat(contiguous, ignore_index=True)
            raw["service_name"] = raw["pod_name"].map(extract_service_name)
            raw["pod_restarts"] = raw["pod_restarts"].clip(lower=0)

            engineer = FeatureEngineer()
            featured = engineer.add_delta_features(raw)
            featured = engineer.add_rolling_features(featured, window=5)
            featured = engineer.add_network_features(featured)
            featured = engineer.add_memory_slope(featured, window=12)
            featured = engineer.add_cpu_zscore_per_pod(featured)
            featured = engineer.add_anomaly_score(featured)

            now = pd.Timestamp(datetime.utcnow())
            latest_by_pod = featured.sort_values("timestamp").groupby("pod_name", sort=False).tail(1)
            latest_by_pod = latest_by_pod.loc[(now - latest_by_pod["timestamp"]) <= pd.Timedelta(seconds=20)]
            if (
                not latest_by_pod.empty
                and latest_by_pod["timestamp"].max() - latest_by_pod["timestamp"].min() > pd.Timedelta(seconds=7.5)
            ):
                raise RuntimeError("latest pod metrics are not from a coherent 5-second snapshot")
            latest_by_pod["service_name"] = latest_by_pod["pod_name"].map(extract_service_name)
            present_services = set(latest_by_pod["service_name"])
            missing_services = sorted(set(SERVICE_NAMES) - present_services)
            if missing_services:
                raise RuntimeError(f"live service coverage incomplete: {missing_services}")

            service_snapshot = latest_by_pod.groupby("service_name")[MODEL_FEATURE_COLS].mean()
            snapshot = {
                service: {feature: float(service_snapshot.loc[service, feature]) for feature in MODEL_FEATURE_COLS}
                for service in SERVICE_NAMES
            }
            if any(not np.isfinite(value) for metrics in snapshot.values() for value in metrics.values()):
                raise RuntimeError("derived feature snapshot contains NaN or infinite values")

            status.update({
                "available": True,
                "feature_count": len(MODEL_FEATURE_COLS),
                "service_count": len(snapshot),
                "reason": "ok" if not status["degraded_features"] else "partial_telemetry_gaps",
                "snapshot_timestamp": latest_by_pod["timestamp"].max().isoformat(),
            })
            self.last_gnn_snapshot_status = status
            return snapshot, status
        except Exception as exc:
            status["reason"] = str(exc)
            self.last_gnn_snapshot_status = status
            logger.warning(f"GNN live feature snapshot unavailable: {exc}")
            raise
    
    def get_cpu_metrics(self, pod_name: str) -> dict:
        """
        Hitung rata-rata CPU usage 5m dan 15m untuk pod tertentu.
        
        Rumus: rate(container_cpu_usage_seconds_total[5m]) * 100
        Ini adalah perhitungan rate deterministik; bukan jaminan kelengkapan/akurasi sumber.
        """
        # CPU rata-rata 5 menit
        query_5m = (
            f'rate(container_cpu_usage_seconds_total{{'
            f'namespace="{self.target_namespace}",'
            f'pod=~"{pod_name}.*",'
            f'container!="POD",container!=""'
            f'}}[5m]) * 100'
        )
        
        # CPU rata-rata 15 menit
        query_15m = query_5m.replace("[5m]", "[15m]")
        
        df_5m = self._query_prometheus(query_5m)
        df_15m = self._query_prometheus(query_15m)
        
        cpu_5m = df_5m["value"].mean() if df_5m is not None and not df_5m.empty else 0.0
        cpu_15m = df_15m["value"].mean() if df_15m is not None and not df_15m.empty else 0.0
        
        return {
            "cpu_usage_avg_5m": round(cpu_5m, 2),
            "cpu_usage_avg_15m": round(cpu_15m, 2),
        }
    
    def get_memory_metrics(self, pod_name: str) -> dict:
        """
        Hitung memory usage dan growth rate.
        
        Growth rate dihitung dengan linear regression sederhana:
        slope = Δmemory / Δtime (MB per menit)
        
        Jika slope > 0 dan konsisten → Memory Leak terdeteksi
        """
        # Memory usage saat ini
        query_current = (
            f'container_memory_usage_bytes{{'
            f'namespace="{self.target_namespace}",'
            f'pod=~"{pod_name}.*",'
            f'container!="POD",container!=""'
            f'}}'
        )
        
        df_current = self._query_prometheus(query_current)
        memory_mb = 0.0
        if df_current is not None and not df_current.empty:
            memory_mb = df_current["value"].sum() / (1024 * 1024)  # Bytes → MB
        
        # Memory limit untuk hitung persentase
        query_limit = query_current.replace(
            "container_memory_usage_bytes",
            "container_spec_memory_limit_bytes"
        )
        df_limit = self._query_prometheus(query_limit)
        memory_limit_mb = 256.0  # Default jika tidak ada limit
        if df_limit is not None and not df_limit.empty:
            limit_val = df_limit["value"].sum() / (1024 * 1024)
            if limit_val > 0:
                memory_limit_mb = limit_val
        
        memory_pct = min((memory_mb / memory_limit_mb) * 100, 100.0)
        
        # Growth rate: Ambil data 15 menit, hitung slope
        df_range = self._query_prometheus_range(
            query_current, duration_minutes=15, step="60s"
        )
        
        growth_rate = 0.0
        if df_range is not None and len(df_range) >= 3:
            df_range["value_mb"] = df_range["value"] / (1024 * 1024)
            df_range["minutes"] = (
                df_range["timestamp"] - df_range["timestamp"].min()
            ).dt.total_seconds() / 60
            
            # Linear regression sederhana untuk growth rate
            if df_range["minutes"].std() > 0:
                slope = np.polyfit(df_range["minutes"], df_range["value_mb"], 1)[0]
                growth_rate = round(slope, 2)
        
        return {
            "memory_usage_mb": round(memory_mb, 2),
            "memory_growth_rate_mb_per_min": growth_rate,
            "memory_usage_percent": round(memory_pct, 2),
        }
    
    def get_pod_metrics(self, pod_name: str, deployment_name: str) -> dict:
        """Hitung metrik pod: restart count, age, replicas."""
        # Restart count 1 jam terakhir
        query_restarts = (
            f'increase(kube_pod_container_status_restarts_total{{'
            f'namespace="{self.target_namespace}",'
            f'pod=~"{pod_name}.*"'
            f'}}[1h])'
        )
        df_restarts = self._query_prometheus(query_restarts)
        restarts = 0
        if df_restarts is not None and not df_restarts.empty:
            restarts = int(df_restarts["value"].max())
        
        # Replicas saat ini
        query_replicas = (
            f'kube_deployment_status_replicas{{'
            f'namespace="{self.target_namespace}",'
            f'deployment="{deployment_name}"'
            f'}}'
        )
        df_replicas = self._query_prometheus(query_replicas)
        replicas = 1
        if df_replicas is not None and not df_replicas.empty:
            replicas = int(df_replicas["value"].iloc[0])
        
        # Pod age (menit)
        query_start = (
            f'time() - kube_pod_start_time{{'
            f'namespace="{self.target_namespace}",'
            f'pod=~"{pod_name}.*"'
            f'}}'
        )
        df_age = self._query_prometheus(query_start)
        age_minutes = 0.0
        if df_age is not None and not df_age.empty:
            age_minutes = df_age["value"].min() / 60  # Seconds → Minutes
        
        return {
            "pod_restarts_1h": restarts,
            "pod_age_minutes": round(age_minutes, 2),
            "current_replicas": replicas,
        }
    
    def get_network_metrics(self, deployment_name: str) -> dict:
        """Summarize trace-derived server request rate, errors, and latency."""
        import re
        from models.gnn.feature_contract import (
            application_metric_queries,
            application_span_selector,
        )

        pod_pattern = f"^{re.escape(deployment_name)}-.*"
        app_queries = application_metric_queries(
            self.target_namespace,
            pod_name_regex=pod_pattern,
        )
        query_rps = app_queries["request_rate"]
        df_rps = self._query_prometheus(query_rps)
        rps = 0.0
        if df_rps is not None and not df_rps.empty:
            rps = df_rps["value"].sum()

        df_error_ratios = self._query_prometheus(app_queries["error_rate"])
        error_rate = 0.0
        if (
            df_rps is not None and not df_rps.empty
            and df_error_ratios is not None and not df_error_ratios.empty
            and rps > 0
            and "k8s_pod_name" in df_rps.columns
            and "k8s_pod_name" in df_error_ratios.columns
        ):
            weighted = df_rps[["k8s_pod_name", "value"]].merge(
                df_error_ratios[["k8s_pod_name", "value"]],
                on="k8s_pod_name",
                suffixes=("_rps", "_error_fraction"),
            )
            error_rate = (
                weighted["value_rps"].mul(weighted["value_error_fraction"]).sum()
                / weighted["value_rps"].sum()
            ) * 100

        span_selector = application_span_selector(
            self.target_namespace,
            pod_name_regex=pod_pattern,
        )
        # Span metrics connector histogram units are configured as seconds.
        query_p50 = (
            f'histogram_quantile(0.50, '
            f'sum by (le) (rate(xfsci_duration_seconds_bucket{{{span_selector}}}[5m]))) * 1000'
        )
        query_p99 = query_p50.replace("0.50", "0.99")
        
        df_p50 = self._query_prometheus(query_p50)
        df_p99 = self._query_prometheus(query_p99)
        
        latency_p50 = df_p50["value"].iloc[0] if df_p50 is not None and not df_p50.empty else 0.0
        latency_p99 = df_p99["value"].iloc[0] if df_p99 is not None and not df_p99.empty else 0.0
        
        return {
            "request_rate_rps": round(rps, 2),
            "error_rate_percent": round(error_rate, 2),
            "latency_p50_ms": round(latency_p50, 2),
            "latency_p99_ms": round(latency_p99, 2),
        }
    
    def detect_anomaly_pattern(self, metrics: dict) -> AnomalyType:
        """
        Deteksi pola anomali berdasarkan metrik — 100% rule-based,
        TIDAK menggunakan AI/LLM.
        
        Returns:
            AnomalyType enum
        """
        # Memory Leak: Pertumbuhan memori konsisten > 5 MB/min
        if metrics.get("memory_growth_rate_mb_per_min", 0) > 5:
            return AnomalyType.MEMORY_LEAK
        
        # CPU Overload: CPU rata-rata > 80%
        if metrics.get("cpu_usage_avg_5m", 0) > 80:
            return AnomalyType.CPU_OVERLOAD
        
        # Pod Crash Loop: > 3 restart dalam 1 jam
        if metrics.get("pod_restarts_1h", 0) >= 3:
            return AnomalyType.POD_CRASH_LOOP
        
        # Network Latency: P99 > 500ms
        if metrics.get("latency_p99_ms", 0) > 500:
            return AnomalyType.NETWORK_LATENCY
        
        # Disk Pressure: > 85%
        if metrics.get("disk_usage_percent", 0) > 85:
            return AnomalyType.DISK_PRESSURE
        
        return AnomalyType.NORMAL
    
    def process_realtime_metrics(self, deployment_name: str,
                                  pod_name: str = None,
                                  node_name: str = "unknown") -> PandasMetrics:
        """
        Pipeline utama: Kumpulkan SEMUA metrik untuk satu deployment
        dan kembalikan sebagai PandasMetrics (Pydantic model).
        
        Args:
            deployment_name: Nama deployment (misal: "cartservice")
            pod_name: Nama pod spesifik (opsional, auto-resolve)
            node_name: Nama node tempat pod berjalan
        
        Returns:
            PandasMetrics — laporan numerik terhitung dari telemetry
        """
        if pod_name is None:
            pod_name = deployment_name  # Akan di-regex match
        
        logger.info(f"Processing metrics for: {deployment_name} (pod: {pod_name})")
        
        # Kumpulkan semua metrik
        cpu = self.get_cpu_metrics(pod_name)
        memory = self.get_memory_metrics(pod_name)
        pod = self.get_pod_metrics(pod_name, deployment_name)
        network = self.get_network_metrics(deployment_name)
        
        # Gabungkan menjadi PandasMetrics
        return PandasMetrics(
            timestamp=datetime.utcnow(),
            target_pod=pod_name,
            target_node=node_name,
            namespace=self.target_namespace,
            **cpu,
            **memory,
            **pod,
            **network,
        )
    
    def generate_situation_summary(self, metrics: PandasMetrics) -> str:
        """
        Generate ringkasan situasi dalam teks — DARI ANGKA PANDAS,
        bukan dari LLM. Ini dipakai sebagai konteks tambahan untuk AI Agent.
        """
        anomaly = self.detect_anomaly_pattern(metrics.model_dump())
        
        lines = [
            f"📊 Situation Report for {metrics.target_pod}",
            f"  Time: {metrics.timestamp.isoformat()}",
            f"  Node: {metrics.target_node}",
            f"  Anomaly: {anomaly.value}",
            f"",
            f"  CPU:    {metrics.cpu_usage_avg_5m:.1f}% (5m avg), {metrics.cpu_usage_avg_15m:.1f}% (15m avg)",
            f"  Memory: {metrics.memory_usage_mb:.1f} MB ({metrics.memory_usage_percent:.1f}%)",
            f"  Mem Growth: {metrics.memory_growth_rate_mb_per_min:+.1f} MB/min",
            f"  Restarts: {metrics.pod_restarts_1h} (last 1h)",
            f"  Replicas: {metrics.current_replicas}",
            f"  RPS:    {metrics.request_rate_rps:.1f}",
            f"  Errors: {metrics.error_rate_percent:.1f}%",
            f"  Latency P50/P99: {metrics.latency_p50_ms:.0f}ms / {metrics.latency_p99_ms:.0f}ms",
        ]
        
        return "\n".join(lines)
