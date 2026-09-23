"""
============================================================
XFSCI Topology Scraper - Layer 2: Cloud Intelligence
============================================================
Membangun graf topologi (adjacency matrix) dari hubungan
antar-service di namespace target, berdasarkan metrik
lalu lintas jaringan yang dikumpulkan oleh Prometheus.

Output:
  - topology_TIMESTAMP.json: adjacency list + metadata
  - Digunakan oleh GNN di Layer 2 untuk membuat node embeddings

Cara pakai:
  python data/collectors/topology_scraper.py
  python data/collectors/topology_scraper.py --test
============================================================
"""

import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import requests
from loguru import logger


# ============================================================
# CONFIGURATION
# ============================================================

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "demo")
OUTPUT_DIR = Path(__file__).parent.parent / "raw"


# ============================================================
# TOPOLOGY SCRAPER
# ============================================================

class TopologyScraper:
    """
    Membangun graf topologi service-to-service berdasarkan 
    metrik network traffic dari Prometheus.
    
    Output format (JSON):
    {
        "timestamp": "2026-09-17T16:00:00",
        "nodes": [
            {"id": "frontend", "type": "service", "cpu": 0.15, "memory": 65000000},
            {"id": "cartservice", "type": "service", "cpu": 0.08, "memory": 32000000},
            ...
        ],
        "edges": [
            {"source": "frontend", "target": "cartservice", "weight": 1500.0, "latency_ms": 12.5},
            {"source": "frontend", "target": "productcatalogservice", "weight": 800.0, "latency_ms": 8.2},
            ...
        ]
    }
    """
    
    def __init__(self, prometheus_url: str = PROMETHEUS_URL):
        self.prometheus_url = prometheus_url
        self.query_api = f"{prometheus_url}/api/v1/query"
        self.output_dir = OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def query_prometheus(self, promql: str) -> dict:
        """Execute PromQL query and return raw results."""
        try:
            resp = requests.get(self.query_api, params={"query": promql}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    return data.get("data", {}).get("result", [])
            return []
        except Exception as e:
            logger.error(f"Query failed: {e}")
            return []
    
    def get_service_nodes(self) -> list[dict]:
        """
        Dapatkan daftar semua service/pod aktif beserta metrik dasar.
        """
        # CPU usage per pod
        cpu_query = (
            f'sum(rate(container_cpu_usage_seconds_total{{'
            f'namespace="{TARGET_NAMESPACE}", container!="", container!="POD"'
            f'}}[1m])) by (pod)'
        )
        cpu_results = self.query_prometheus(cpu_query)
        
        # Memory usage per pod
        mem_query = (
            f'sum(container_memory_working_set_bytes{{'
            f'namespace="{TARGET_NAMESPACE}", container!="", container!="POD"'
            f'}}) by (pod)'
        )
        mem_results = self.query_prometheus(mem_query)
        
        # Restart count per pod
        restart_query = (
            f'sum(kube_pod_container_status_restarts_total{{'
            f'namespace="{TARGET_NAMESPACE}"'
            f'}}) by (pod)'
        )
        restart_results = self.query_prometheus(restart_query)
        
        # Parse results
        cpu_map = {r["metric"].get("pod", ""): float(r["value"][1]) for r in cpu_results}
        mem_map = {r["metric"].get("pod", ""): float(r["value"][1]) for r in mem_results}
        restart_map = {r["metric"].get("pod", ""): int(float(r["value"][1])) for r in restart_results}
        
        # Combine
        all_pods = set(cpu_map.keys()) | set(mem_map.keys())
        all_pods = {p for p in all_pods if p and not p.startswith("prometheus")}
        
        nodes = []
        for pod in sorted(all_pods):
            # Extract service name from pod name (e.g., "frontend-abc123" -> "frontend")
            service_name = pod.rsplit("-", 2)[0] if "-" in pod else pod
            
            nodes.append({
                "id": pod,
                "service": service_name,
                "type": "service",
                "cpu_usage": round(cpu_map.get(pod, 0.0), 6),
                "memory_usage": round(mem_map.get(pod, 0.0), 2),
                "restart_count": restart_map.get(pod, 0),
                "status": self._determine_status(
                    cpu_map.get(pod, 0.0),
                    mem_map.get(pod, 0.0),
                    restart_map.get(pod, 0)
                )
            })
        
        return nodes
    
    def _determine_status(self, cpu: float, memory: float, restarts: int) -> str:
        """Determine node health status based on metrics."""
        if restarts > 3:
            return "critical"
        if cpu > 0.8 or memory > 500_000_000:  # >80% CPU or >500MB RAM
            return "warning"
        return "healthy"
    
    def get_service_edges(self) -> list[dict]:
        """
        Bangun edges berdasarkan known dependencies dari 
        Online Boutique architecture.
        
        Dalam production environment, edges bisa dibangun dari:
        - Istio/Envoy sidecar metrics (istio_requests_total)
        - OpenTelemetry distributed traces
        - Network flow data
        
        Untuk demo, kita menggunakan static topology dari Online Boutique
        yang diperkaya dengan metrik network aktual.
        """
        # Known service dependencies (Online Boutique architecture)
        # Format: (source_service, target_service)
        known_dependencies = [
            ("frontend", "cartservice"),
            ("frontend", "productcatalogservice"),
            ("frontend", "currencyservice"),
            ("frontend", "recommendationservice"),
            ("frontend", "shippingservice"),
            ("frontend", "checkoutservice"),
            ("frontend", "adservice"),
            ("checkoutservice", "cartservice"),
            ("checkoutservice", "productcatalogservice"),
            ("checkoutservice", "currencyservice"),
            ("checkoutservice", "shippingservice"),
            ("checkoutservice", "paymentservice"),
            ("checkoutservice", "emailservice"),
            ("recommendationservice", "productcatalogservice"),
            ("cartservice", "redis-cart"),
        ]
        
        # Get network traffic data per pod
        rx_query = (
            f'sum(rate(container_network_receive_bytes_total{{'
            f'namespace="{TARGET_NAMESPACE}"'
            f'}}[1m])) by (pod)'
        )
        rx_results = self.query_prometheus(rx_query)
        rx_map = {r["metric"].get("pod", ""): float(r["value"][1]) for r in rx_results}
        
        edges = []
        for source_svc, target_svc in known_dependencies:
            # Estimate edge weight from network traffic
            # (Simplified: use target's rx bytes as proxy for traffic volume)
            target_traffic = 0.0
            for pod, rx in rx_map.items():
                if pod.startswith(target_svc):
                    target_traffic = rx
                    break
            
            edges.append({
                "source": source_svc,
                "target": target_svc,
                "weight": round(target_traffic, 2),
                "type": "dependency",
            })
        
        return edges
    
    def build_topology(self) -> dict:
        """Bangun graf topologi lengkap."""
        logger.info("Building service topology graph...")
        
        nodes = self.get_service_nodes()
        edges = self.get_service_edges()
        
        topology = {
            "timestamp": datetime.now().isoformat(),
            "namespace": TARGET_NAMESPACE,
            "num_nodes": len(nodes),
            "num_edges": len(edges),
            "nodes": nodes,
            "edges": edges,
        }
        
        logger.info(f"  Nodes: {len(nodes)} pods/services")
        logger.info(f"  Edges: {len(edges)} dependencies")
        
        return topology
    
    def save_topology(self, topology: dict, filepath: Path = None) -> Path:
        """Simpan topologi ke JSON file."""
        if filepath is None:
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = self.output_dir / f"topology_{timestamp_str}.json"
        
        with open(filepath, "w") as f:
            json.dump(topology, f, indent=2)
        
        logger.success(f"💾 Topology saved to {filepath}")
        return filepath
    
    def run(self, test_mode: bool = False):
        """Build and save topology."""
        topology = self.build_topology()
        
        if topology["num_nodes"] == 0:
            logger.warning("No nodes found! Check namespace and Prometheus.")
            if test_mode:
                # Generate example topology for testing
                logger.info("Generating example topology for testing...")
                topology = self._generate_example_topology()
        
        filepath = self.save_topology(topology)
        
        # Print summary
        logger.info("")
        logger.info("Topology Summary:")
        for node in topology["nodes"]:
            status_icon = {"healthy": "🟢", "warning": "🟡", "critical": "🔴"}.get(
                node.get("status", "healthy"), "⚪"
            )
            logger.info(f"  {status_icon} {node['id']} (CPU: {node.get('cpu_usage', 0):.4f})")
        
        return topology
    
    def _generate_example_topology(self) -> dict:
        """Generate example topology untuk testing tanpa cluster."""
        services = [
            "frontend", "cartservice", "redis-cart", "productcatalogservice",
            "currencyservice", "paymentservice", "shippingservice",
            "emailservice", "checkoutservice", "recommendationservice", "adservice"
        ]
        
        import random
        nodes = []
        for svc in services:
            nodes.append({
                "id": f"{svc}-{random.randint(1000,9999)}",
                "service": svc,
                "type": "service",
                "cpu_usage": round(random.uniform(0.01, 0.5), 6),
                "memory_usage": round(random.uniform(20_000_000, 300_000_000), 2),
                "restart_count": random.randint(0, 2),
                "status": "healthy"
            })
        
        edges = [
            {"source": "frontend", "target": "cartservice", "weight": 1500, "type": "dependency"},
            {"source": "frontend", "target": "productcatalogservice", "weight": 800, "type": "dependency"},
            {"source": "frontend", "target": "currencyservice", "weight": 600, "type": "dependency"},
            {"source": "frontend", "target": "checkoutservice", "weight": 400, "type": "dependency"},
            {"source": "frontend", "target": "recommendationservice", "weight": 700, "type": "dependency"},
            {"source": "frontend", "target": "adservice", "weight": 300, "type": "dependency"},
            {"source": "checkoutservice", "target": "paymentservice", "weight": 350, "type": "dependency"},
            {"source": "checkoutservice", "target": "shippingservice", "weight": 250, "type": "dependency"},
            {"source": "checkoutservice", "target": "emailservice", "weight": 150, "type": "dependency"},
            {"source": "cartservice", "target": "redis-cart", "weight": 2000, "type": "dependency"},
            {"source": "recommendationservice", "target": "productcatalogservice", "weight": 500, "type": "dependency"},
        ]
        
        return {
            "timestamp": datetime.now().isoformat(),
            "namespace": TARGET_NAMESPACE,
            "num_nodes": len(nodes),
            "num_edges": len(edges),
            "nodes": nodes,
            "edges": edges,
        }


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="XFSCI Topology Scraper - Build service dependency graph"
    )
    parser.add_argument("--test", action="store_true", help="Test mode (generate example)")
    
    args = parser.parse_args()
    
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    
    scraper = TopologyScraper()
    scraper.run(test_mode=args.test)


if __name__ == "__main__":
    main()
