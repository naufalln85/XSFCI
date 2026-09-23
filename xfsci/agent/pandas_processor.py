# ============================================================
# XFSCI Pandas Metric Processor
# ============================================================
# Modul ini mengolah data mentah dari Prometheus/Loki menjadi
# metrik terstruktur yang 100% akurat (matematika Python murni).
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

import time
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
    yang 100% akurat dan deterministik.
    
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
        
        self.prometheus_url = self.config["monitoring"]["prometheus"]["url"]
        self.target_namespace = self.config["data"]["target_namespace"]
        
        logger.info(f"PandasMetricProcessor initialized | Prometheus: {self.prometheus_url}")
    
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
            logger.warning(f"Prometheus query failed: {e}")
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
            logger.warning(f"Prometheus range query failed: {e}")
            return None
    
    def get_cpu_metrics(self, pod_name: str) -> dict:
        """
        Hitung rata-rata CPU usage 5m dan 15m untuk pod tertentu.
        
        Rumus: rate(container_cpu_usage_seconds_total[5m]) * 100
        Ini adalah perhitungan rate murni — 0% halusinasi.
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
        """Hitung metrik jaringan: RPS, error rate, latency."""
        # Request rate (if using Istio/service mesh metrics)
        query_rps = (
            f'sum(rate(http_server_requests_seconds_count{{'
            f'namespace="{self.target_namespace}",'
            f'deployment="{deployment_name}"'
            f'}}[5m]))'
        )
        df_rps = self._query_prometheus(query_rps)
        rps = 0.0
        if df_rps is not None and not df_rps.empty:
            rps = df_rps["value"].sum()
        
        # Error rate
        query_errors = (
            f'sum(rate(http_server_requests_seconds_count{{'
            f'namespace="{self.target_namespace}",'
            f'deployment="{deployment_name}",'
            f'status=~"5.."'
            f'}}[5m]))'
        )
        df_errors = self._query_prometheus(query_errors)
        error_rate = 0.0
        if df_errors is not None and not df_errors.empty and rps > 0:
            error_rate = (df_errors["value"].sum() / rps) * 100
        
        # Latency P50 dan P99
        query_p50 = (
            f'histogram_quantile(0.50, sum(rate('
            f'http_server_requests_seconds_bucket{{'
            f'namespace="{self.target_namespace}",'
            f'deployment="{deployment_name}"'
            f'}}[5m])) by (le)) * 1000'
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
            PandasMetrics — Laporan numerik 100% akurat
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
