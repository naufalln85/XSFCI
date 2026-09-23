"""
============================================================
XFSCI Metrics Scraper - Layer 1: Monitoring
============================================================
Mengumpulkan metrik telemetri real-time dari Prometheus API
menggunakan PromQL queries, dan menyimpan hasilnya ke CSV.

Metrik yang dikumpulkan per pod:
  - CPU usage (cores)
  - Memory usage (bytes & percent)
  - Pod restart count
  - Network RX/TX bytes per second
  - Request rate (ops/sec) - jika tersedia
  - Error rate (%) - jika tersedia
  - Latency P50/P95/P99 (ms) - jika tersedia

Cara pakai:
  python data/collectors/metrics_scraper.py

Flags:
  --test          Jalankan 1x scrape saja (untuk testing)
  --interval 5    Interval scrape dalam detik (default: 5)
  --duration 60   Durasi collection dalam menit (default: 60)
  --output FILE   Path output CSV (default: data/raw/metrics_TIMESTAMP.csv)
============================================================
"""

import os
import sys
import time
import argparse
import signal
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd
from loguru import logger

# ============================================================
# CONFIGURATION
# ============================================================

# Prometheus API endpoint
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
PROMETHEUS_QUERY_API = f"{PROMETHEUS_URL}/api/v1/query"

# Target namespace untuk monitoring
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "demo")

# Output directory
OUTPUT_DIR = Path(__file__).parent.parent / "raw"


# ============================================================
# PROMQL QUERIES
# ============================================================
# Setiap query mengembalikan metrik per-pod di namespace target.
# Label 'pod' digunakan sebagai identifier unik.

QUERIES = {
    # --- Resource Metrics ---
    
    # CPU usage per pod (dalam cores, rate over 1m)
    "cpu_usage": (
        f'sum(rate(container_cpu_usage_seconds_total{{'
        f'namespace="{TARGET_NAMESPACE}", container!="", container!="POD"'
        f'}}[1m])) by (pod)'
    ),
    
    # Memory working set per pod (dalam bytes)
    "memory_usage": (
        f'sum(container_memory_working_set_bytes{{'
        f'namespace="{TARGET_NAMESPACE}", container!="", container!="POD"'
        f'}}) by (pod)'
    ),
    
    # Memory usage percentage per pod (working set / limit)
    "memory_usage_percent": (
        f'sum(container_memory_working_set_bytes{{'
        f'namespace="{TARGET_NAMESPACE}", container!="", container!="POD"'
        f'}}) by (pod) / '
        f'sum(container_spec_memory_limit_bytes{{'
        f'namespace="{TARGET_NAMESPACE}", container!="", container!="POD"'
        f'}}) by (pod) * 100'
    ),
    
    # Pod restart count (kumulatif)
    "pod_restarts": (
        f'sum(kube_pod_container_status_restarts_total{{'
        f'namespace="{TARGET_NAMESPACE}"'
        f'}}) by (pod)'
    ),
    
    # --- Network Metrics ---
    
    # Network receive bytes per second
    "net_rx_bytes": (
        f'sum(rate(container_network_receive_bytes_total{{'
        f'namespace="{TARGET_NAMESPACE}"'
        f'}}[1m])) by (pod)'
    ),
    
    # Network transmit bytes per second
    "net_tx_bytes": (
        f'sum(rate(container_network_transmit_bytes_total{{'
        f'namespace="{TARGET_NAMESPACE}"'
        f'}}[1m])) by (pod)'
    ),
    
    # --- Application Metrics (jika service mengexpose metrics) ---
    
    # HTTP request rate per pod (jika ada)
    "request_rate": (
        f'sum(rate(http_server_requests_seconds_count{{'
        f'namespace="{TARGET_NAMESPACE}"'
        f'}}[1m])) by (pod)'
    ),
    
    # HTTP error rate per pod (4xx + 5xx / total)
    "error_rate": (
        f'sum(rate(http_server_requests_seconds_count{{'
        f'namespace="{TARGET_NAMESPACE}", status=~"4..|5.."'
        f'}}[1m])) by (pod) / '
        f'sum(rate(http_server_requests_seconds_count{{'
        f'namespace="{TARGET_NAMESPACE}"'
        f'}}[1m])) by (pod) * 100'
    ),
}


# ============================================================
# SCRAPER CLASS
# ============================================================

class XFSCIMetricsScraper:
    """
    Scraper utama yang mengambil metrik dari Prometheus
    dan menyimpannya ke DataFrame/CSV.
    """
    
    def __init__(self, prometheus_url: str = PROMETHEUS_URL, output_dir: Path = OUTPUT_DIR):
        self.prometheus_url = prometheus_url
        self.query_api = f"{prometheus_url}/api/v1/query"
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.collected_rows = []
        self._running = True
        
        # Handle graceful shutdown (Ctrl+C)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
    
    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown: simpan data yang sudah dikumpulkan."""
        logger.warning("Shutdown signal received. Saving collected data...")
        self._running = False
    
    def check_prometheus_connection(self) -> bool:
        """Cek apakah Prometheus API bisa diakses."""
        try:
            resp = requests.get(f"{self.prometheus_url}/api/v1/status/config", timeout=5)
            if resp.status_code == 200:
                logger.info(f"✅ Prometheus connected at {self.prometheus_url}")
                return True
            else:
                logger.error(f"❌ Prometheus returned status {resp.status_code}")
                return False
        except requests.ConnectionError:
            logger.error(f"❌ Cannot connect to Prometheus at {self.prometheus_url}")
            logger.info("   Make sure Prometheus is running and port-forwarded:")
            logger.info("   kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring")
            return False
    
    def query_prometheus(self, promql: str) -> dict:
        """
        Eksekusi satu PromQL query dan return dict {pod_name: value}.
        
        Args:
            promql: PromQL query string
            
        Returns:
            dict: {pod_name: float_value}
        """
        try:
            response = requests.get(
                self.query_api,
                params={"query": promql},
                timeout=10
            )
            
            if response.status_code != 200:
                logger.warning(f"Prometheus query failed: {response.status_code}")
                return {}
            
            data = response.json()
            
            if data.get("status") != "success":
                logger.warning(f"Query status: {data.get('status')}")
                return {}
            
            results = data.get("data", {}).get("result", [])
            parsed = {}
            
            for result in results:
                pod_name = result["metric"].get("pod", "unknown")
                # Value format: [timestamp, "value_string"]
                value_str = result["value"][1]
                
                # Handle NaN, Inf, dll
                try:
                    value = float(value_str)
                    if value != value:  # NaN check
                        value = 0.0
                except (ValueError, TypeError):
                    value = 0.0
                
                parsed[pod_name] = value
            
            return parsed
            
        except requests.Timeout:
            logger.warning("Prometheus query timed out")
            return {}
        except Exception as e:
            logger.error(f"Error querying Prometheus: {e}")
            return {}
    
    def collect_one_step(self) -> list[dict]:
        """
        Kumpulkan semua metrik untuk 1 timestep.
        
        Returns:
            list[dict]: Satu row per pod dengan semua metrik
        """
        timestamp = datetime.now().isoformat()
        rows = []
        
        # Query semua metrik
        all_metrics = {}
        for metric_name, promql in QUERIES.items():
            all_metrics[metric_name] = self.query_prometheus(promql)
        
        # Kumpulkan semua pod names yang unik
        all_pods = set()
        for metric_data in all_metrics.values():
            all_pods.update(metric_data.keys())
        
        # Filter: hanya pod yang valid (bukan system pods)
        all_pods = {p for p in all_pods if p != "unknown" and not p.startswith("prometheus")}
        
        if not all_pods:
            logger.warning("No pods found. Check namespace and Prometheus targets.")
            return []
        
        # Build row per pod
        for pod in sorted(all_pods):
            row = {
                "timestamp": timestamp,
                "pod_name": pod,
                "cpu_usage": round(all_metrics.get("cpu_usage", {}).get(pod, 0.0), 6),
                "memory_usage": round(all_metrics.get("memory_usage", {}).get(pod, 0.0), 2),
                "memory_usage_percent": round(all_metrics.get("memory_usage_percent", {}).get(pod, 0.0), 2),
                "pod_restarts": int(all_metrics.get("pod_restarts", {}).get(pod, 0)),
                "net_rx_bytes": round(all_metrics.get("net_rx_bytes", {}).get(pod, 0.0), 2),
                "net_tx_bytes": round(all_metrics.get("net_tx_bytes", {}).get(pod, 0.0), 2),
                "request_rate": round(all_metrics.get("request_rate", {}).get(pod, 0.0), 4),
                "error_rate": round(all_metrics.get("error_rate", {}).get(pod, 0.0), 4),
                # Label awal: NORMAL (akan diupdate oleh data_labeler.py)
                "label": "NORMAL",
            }
            rows.append(row)
        
        return rows
    
    def save_to_csv(self, filepath: Path = None):
        """Simpan semua data yang terkumpul ke CSV."""
        if not self.collected_rows:
            logger.warning("No data to save!")
            return
        
        if filepath is None:
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = self.output_dir / f"metrics_{timestamp_str}.csv"
        
        df = pd.DataFrame(self.collected_rows)
        df.to_csv(filepath, index=False)
        
        logger.success(f"💾 Saved {len(df)} rows to {filepath}")
        logger.info(f"   Pods tracked: {df['pod_name'].nunique()}")
        logger.info(f"   Time range: {df['timestamp'].min()} → {df['timestamp'].max()}")
        logger.info(f"   File size: {filepath.stat().st_size / 1024:.1f} KB")
        
        return filepath
    
    def run(self, interval_seconds: int = 5, duration_minutes: int = 60, test_mode: bool = False):
        """
        Loop utama: scrape metrik setiap N detik.
        
        Args:
            interval_seconds: Jeda antar scrape (default 5s)
            duration_minutes: Durasi total collection (default 60 min)
            test_mode: Jika True, hanya jalankan 1x scrape
        """
        # Check connection
        if not self.check_prometheus_connection():
            sys.exit(1)
        
        total_steps = (duration_minutes * 60) // interval_seconds
        
        logger.info(f"🚀 Starting XFSCI Metrics Scraper")
        logger.info(f"   Interval: {interval_seconds}s")
        logger.info(f"   Duration: {duration_minutes} minutes ({total_steps} steps)")
        logger.info(f"   Namespace: {TARGET_NAMESPACE}")
        logger.info(f"   Output: {self.output_dir}")
        logger.info(f"   Press Ctrl+C to stop and save")
        logger.info("")
        
        step = 0
        start_time = time.time()
        
        while self._running:
            step += 1
            step_start = time.time()
            
            # Collect metrics
            new_rows = self.collect_one_step()
            
            if new_rows:
                self.collected_rows.extend(new_rows)
                elapsed = time.time() - start_time
                elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
                logger.info(
                    f"[{elapsed_str}] Step {step}: "
                    f"Collected {len(new_rows)} pods | "
                    f"Total rows: {len(self.collected_rows)}"
                )
            else:
                logger.warning(f"Step {step}: No data collected")
            
            # Test mode: satu kali saja
            if test_mode:
                logger.info("Test mode: single scrape completed")
                break
            
            # Check durasi
            if time.time() - start_time >= duration_minutes * 60:
                logger.info(f"⏰ Duration limit reached ({duration_minutes} minutes)")
                break
            
            # Sleep hingga interval berikutnya
            step_duration = time.time() - step_start
            sleep_time = max(0, interval_seconds - step_duration)
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        # Save data
        logger.info("")
        logger.info("=" * 50)
        filepath = self.save_to_csv()
        logger.info("=" * 50)
        
        return filepath


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="XFSCI Metrics Scraper - Collect telemetry from Prometheus"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run single scrape for testing"
    )
    parser.add_argument(
        "--interval", type=int, default=5,
        help="Scrape interval in seconds (default: 5)"
    )
    parser.add_argument(
        "--duration", type=int, default=60,
        help="Collection duration in minutes (default: 60)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output CSV file path"
    )
    parser.add_argument(
        "--prometheus-url", type=str, default=PROMETHEUS_URL,
        help=f"Prometheus URL (default: {PROMETHEUS_URL})"
    )
    
    args = parser.parse_args()
    
    # Configure logger
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    
    # Create scraper
    scraper = XFSCIMetricsScraper(
        prometheus_url=args.prometheus_url,
        output_dir=OUTPUT_DIR
    )
    
    # Run
    scraper.run(
        interval_seconds=args.interval,
        duration_minutes=args.duration,
        test_mode=args.test
    )


if __name__ == "__main__":
    main()
