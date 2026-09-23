"""
============================================================
XFSCI Log Scraper - Layer 1: Monitoring (Log Collection)
============================================================
Mengumpulkan log teks dari Grafana Loki menggunakan LogQL API.
Mengekstrak event-event penting untuk dataset:
  - Error messages
  - OOMKilled events
  - CrashLoopBackOff
  - Connection refused / timeout
  - Pod restart events

Cara pakai:
  python data/collectors/log_scraper.py

Flags:
  --test          Jalankan 1x query saja
  --interval 30   Interval query dalam detik (default: 30)
  --duration 60   Durasi collection dalam menit (default: 60)
============================================================
"""

import os
import sys
import time
import json
import argparse
import signal
from datetime import datetime, timedelta
from pathlib import Path

import requests
import pandas as pd
from loguru import logger


# ============================================================
# CONFIGURATION
# ============================================================

LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
LOKI_QUERY_API = f"{LOKI_URL}/loki/api/v1/query_range"

TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "demo")

OUTPUT_DIR = Path(__file__).parent.parent / "raw"


# ============================================================
# LOGQL QUERIES
# ============================================================
# LogQL digunakan untuk mengambil log dari Loki.
# Format: {label_selector} |= "filter_text"

LOG_QUERIES = {
    # Semua error logs dari namespace demo
    "error_logs": f'{{namespace="{TARGET_NAMESPACE}"}} |= "error" or "Error" or "ERROR"',
    
    # OOMKilled events
    "oom_events": f'{{namespace="{TARGET_NAMESPACE}"}} |= "OOMKilled" or "oom" or "OutOfMemory"',
    
    # CrashLoopBackOff
    "crash_events": f'{{namespace="{TARGET_NAMESPACE}"}} |= "CrashLoopBackOff" or "crash" or "BackOff"',
    
    # Connection refused / timeout
    "connection_errors": f'{{namespace="{TARGET_NAMESPACE}"}} |= "connection refused" or "timeout" or "deadline exceeded"',
    
    # Exception / panic
    "exception_events": f'{{namespace="{TARGET_NAMESPACE}"}} |= "exception" or "panic" or "fatal"',
}


# ============================================================
# LOG SCRAPER CLASS
# ============================================================

class XFSCILogScraper:
    """
    Scraper log dari Grafana Loki menggunakan LogQL.
    Mengekstrak event-event penting dan menyimpannya ke CSV.
    """
    
    def __init__(self, loki_url: str = LOKI_URL, output_dir: Path = OUTPUT_DIR):
        self.loki_url = loki_url
        self.query_api = f"{loki_url}/loki/api/v1/query_range"
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.collected_rows = []
        self._running = True
        
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
    
    def _handle_shutdown(self, signum, frame):
        logger.warning("Shutdown signal received. Saving collected logs...")
        self._running = False
    
    def check_loki_connection(self) -> bool:
        """Cek apakah Loki API bisa diakses."""
        try:
            resp = requests.get(f"{self.loki_url}/ready", timeout=5)
            if resp.status_code == 200:
                logger.info(f"✅ Loki connected at {self.loki_url}")
                return True
            else:
                logger.error(f"❌ Loki returned status {resp.status_code}")
                return False
        except requests.ConnectionError:
            logger.error(f"❌ Cannot connect to Loki at {self.loki_url}")
            logger.info("   Make sure Loki is running and port-forwarded:")
            logger.info("   kubectl port-forward svc/loki 3100:3100 -n monitoring")
            return False
    
    def query_loki(self, logql: str, lookback_seconds: int = 60) -> list[dict]:
        """
        Eksekusi LogQL query dan parse hasilnya.
        
        Args:
            logql: LogQL query string
            lookback_seconds: Berapa detik ke belakang untuk query
            
        Returns:
            list[dict]: Parsed log entries
        """
        now = datetime.utcnow()
        start = now - timedelta(seconds=lookback_seconds)
        
        params = {
            "query": logql,
            "start": int(start.timestamp() * 1e9),  # Nanoseconds
            "end": int(now.timestamp() * 1e9),
            "limit": 100,
        }
        
        try:
            response = requests.get(self.query_api, params=params, timeout=15)
            
            if response.status_code != 200:
                logger.warning(f"Loki query failed: {response.status_code}")
                return []
            
            data = response.json()
            
            if data.get("status") != "success":
                return []
            
            entries = []
            results = data.get("data", {}).get("result", [])
            
            for stream in results:
                labels = stream.get("stream", {})
                pod_name = labels.get("pod", "unknown")
                container = labels.get("container", "unknown")
                app = labels.get("app", "unknown")
                
                for value in stream.get("values", []):
                    timestamp_ns = int(value[0])
                    log_line = value[1]
                    
                    entries.append({
                        "timestamp": datetime.fromtimestamp(
                            timestamp_ns / 1e9
                        ).isoformat(),
                        "pod_name": pod_name,
                        "container": container,
                        "app": app,
                        "log_line": log_line[:500],  # Truncate panjang
                    })
            
            return entries
            
        except requests.Timeout:
            logger.warning("Loki query timed out")
            return []
        except Exception as e:
            logger.error(f"Error querying Loki: {e}")
            return []
    
    def collect_one_step(self, lookback_seconds: int = 60) -> list[dict]:
        """
        Kumpulkan semua log events untuk 1 timestep.
        """
        all_entries = []
        
        for event_type, logql in LOG_QUERIES.items():
            entries = self.query_loki(logql, lookback_seconds)
            
            for entry in entries:
                entry["event_type"] = event_type
            
            all_entries.extend(entries)
        
        # Deduplicate berdasarkan timestamp + pod_name + log_line
        seen = set()
        unique_entries = []
        for entry in all_entries:
            key = (entry["timestamp"], entry["pod_name"], entry["log_line"][:100])
            if key not in seen:
                seen.add(key)
                unique_entries.append(entry)
        
        return unique_entries
    
    def save_to_csv(self, filepath: Path = None):
        """Simpan semua log entries ke CSV."""
        if not self.collected_rows:
            logger.warning("No log data to save!")
            return
        
        if filepath is None:
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = self.output_dir / f"logs_{timestamp_str}.csv"
        
        df = pd.DataFrame(self.collected_rows)
        df.to_csv(filepath, index=False)
        
        logger.success(f"💾 Saved {len(df)} log entries to {filepath}")
        
        # Summary per event type
        if "event_type" in df.columns:
            logger.info("  Log event summary:")
            for event_type, count in df["event_type"].value_counts().items():
                logger.info(f"    {event_type}: {count}")
        
        return filepath
    
    def run(self, interval_seconds: int = 30, duration_minutes: int = 60, test_mode: bool = False):
        """
        Loop utama: query logs setiap N detik.
        """
        if not self.check_loki_connection():
            sys.exit(1)
        
        logger.info(f"🚀 Starting XFSCI Log Scraper")
        logger.info(f"   Interval: {interval_seconds}s")
        logger.info(f"   Duration: {duration_minutes} minutes")
        logger.info(f"   Namespace: {TARGET_NAMESPACE}")
        logger.info(f"   Press Ctrl+C to stop and save")
        logger.info("")
        
        step = 0
        start_time = time.time()
        
        while self._running:
            step += 1
            
            new_entries = self.collect_one_step(lookback_seconds=interval_seconds + 5)
            
            if new_entries:
                self.collected_rows.extend(new_entries)
                elapsed = time.strftime("%H:%M:%S", time.gmtime(time.time() - start_time))
                logger.info(
                    f"[{elapsed}] Step {step}: "
                    f"Found {len(new_entries)} log events | "
                    f"Total: {len(self.collected_rows)}"
                )
            
            if test_mode:
                break
            
            if time.time() - start_time >= duration_minutes * 60:
                logger.info(f"⏰ Duration limit reached ({duration_minutes} minutes)")
                break
            
            time.sleep(interval_seconds)
        
        self.save_to_csv()


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="XFSCI Log Scraper - Collect logs from Loki"
    )
    parser.add_argument("--test", action="store_true", help="Single query test")
    parser.add_argument("--interval", type=int, default=30, help="Query interval (s)")
    parser.add_argument("--duration", type=int, default=60, help="Duration (min)")
    parser.add_argument("--loki-url", type=str, default=LOKI_URL, help="Loki URL")
    
    args = parser.parse_args()
    
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    
    scraper = XFSCILogScraper(loki_url=args.loki_url)
    scraper.run(
        interval_seconds=args.interval,
        duration_minutes=args.duration,
        test_mode=args.test
    )


if __name__ == "__main__":
    main()
