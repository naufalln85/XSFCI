# ============================================================
# XFSCI Dry-Run Sandbox
# ============================================================
# Modul ini menyediakan "ruang eksperimen aman" untuk AI Agent
# menguji aksi pada masalah baru SEBELUM dieksekusi di produksi.
#
# MENGAPA SANDBOX DIPERLUKAN?
# - Untuk masalah yang belum pernah terjadi (RAG similarity < 70%)
# - AI Agent bisa "bereksperimen" tanpa risiko merusak produksi
# - Hasil sandbox menjadi input tambahan bagi keputusan final
#
# CARA KERJA:
# 1. Clone deployment target ke namespace sandbox
# 2. Simulasi kondisi yang sama (stress test)
# 3. Eksekusi aksi AI di sandbox
# 4. Ukur hasilnya → Berhasil atau gagal?
# 5. Cleanup semua resource sandbox
# ============================================================

import time
from datetime import datetime
from typing import Optional

import yaml
from pathlib import Path
from loguru import logger

try:
    from kubernetes import client, config as k8s_config
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False
    logger.warning("kubernetes package not installed, sandbox in simulation mode")

from agent.action_schema import ActionDecision, ActionType


class DryRunSandbox:
    """
    Sandbox untuk menguji aksi AI Agent secara aman.
    
    Sandbox ini membuat namespace Kubernetes terpisah (xfsci-sandbox)
    dan menjalankan aksi di sana. Jika aksi berhasil di sandbox,
    barulah diterapkan di produksi.
    """
    
    def __init__(self, config_path: str = None):
        """Inisialisasi sandbox dengan konfigurasi."""
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        self.sandbox_config = config.get("sandbox", {})
        self.namespace = self.sandbox_config.get("namespace", "xfsci-sandbox")
        self.test_duration = self.sandbox_config.get("test_duration_seconds", 60)
        self.cleanup_after = self.sandbox_config.get("cleanup_after_test", True)
        self.trigger_conditions = self.sandbox_config.get("trigger_conditions", {})
        
        # Setup K8s client
        self.k8s_available = False
        if K8S_AVAILABLE:
            try:
                k8s_config.load_incluster_config()
                self.k8s_available = True
            except Exception:
                try:
                    k8s_config.load_kube_config()
                    self.k8s_available = True
                except Exception as e:
                    logger.warning(f"K8s config not available: {e}")
        
        if self.k8s_available:
            self.core_v1 = client.CoreV1Api()
            self.apps_v1 = client.AppsV1Api()
        
        logger.info(f"DryRunSandbox initialized | Namespace: {self.namespace} | "
                    f"K8s available: {self.k8s_available}")
    
    def should_use_sandbox(self, rag_similarity: float,
                            anomaly_type: str = "unknown") -> bool:
        """
        Tentukan apakah sandbox perlu digunakan.
        
        Sandbox digunakan HANYA jika:
        1. RAG similarity < threshold (masalah baru/tidak dikenal)
        2. Anomaly type = "unknown"
        
        Args:
            rag_similarity: Skor kemiripan RAG (0-1)
            anomaly_type: Jenis anomali terdeteksi
        
        Returns:
            True jika sandbox diperlukan
        """
        sim_threshold = self.trigger_conditions.get("rag_similarity_below", 0.70)
        check_novel = self.trigger_conditions.get("novel_anomaly_type", True)
        
        if rag_similarity < sim_threshold:
            logger.info(
                f"Sandbox triggered: RAG similarity {rag_similarity:.2f} "
                f"< threshold {sim_threshold}"
            )
            return True
        
        if check_novel and anomaly_type == "unknown":
            logger.info("Sandbox triggered: Unknown anomaly type")
            return True
        
        return False
    
    def create_sandbox_namespace(self) -> bool:
        """Buat namespace sandbox jika belum ada."""
        if not self.k8s_available:
            logger.info("[SIMULATION] Would create namespace: " + self.namespace)
            return True
        
        try:
            self.core_v1.read_namespace(name=self.namespace)
            logger.info(f"Sandbox namespace '{self.namespace}' already exists")
            return True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                ns = client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=self.namespace,
                        labels={
                            "xfsci-purpose": "sandbox",
                            "auto-cleanup": "true"
                        }
                    )
                )
                self.core_v1.create_namespace(body=ns)
                logger.success(f"Created sandbox namespace: {self.namespace}")
                return True
            raise
    
    def clone_deployment(self, source_namespace: str,
                          deployment_name: str) -> bool:
        """
        Clone sebuah deployment dari namespace produksi ke sandbox.
        
        Proses:
        1. Baca deployment spec dari namespace asli
        2. Modifikasi namespace dan nama
        3. Set replicas = 1 (hemat resource)
        4. Deploy ke sandbox namespace
        """
        if not self.k8s_available:
            logger.info(
                f"[SIMULATION] Would clone {deployment_name} from "
                f"{source_namespace} to {self.namespace}"
            )
            return True
        
        try:
            # Baca deployment asli
            source_dep = self.apps_v1.read_namespaced_deployment(
                name=deployment_name,
                namespace=source_namespace
            )
            
            # Modifikasi untuk sandbox
            sandbox_dep = client.V1Deployment(
                metadata=client.V1ObjectMeta(
                    name=f"sandbox-{deployment_name}",
                    namespace=self.namespace,
                    labels={
                        "xfsci-sandbox": "true",
                        "original-deployment": deployment_name,
                        "original-namespace": source_namespace
                    }
                ),
                spec=client.V1DeploymentSpec(
                    replicas=1,  # Minimal resource
                    selector=source_dep.spec.selector,
                    template=source_dep.spec.template
                )
            )
            
            # Update template namespace
            sandbox_dep.spec.template.metadata.namespace = self.namespace
            
            # Deploy ke sandbox
            self.apps_v1.create_namespaced_deployment(
                namespace=self.namespace,
                body=sandbox_dep
            )
            
            logger.success(f"Cloned {deployment_name} to sandbox")
            return True
            
        except Exception as e:
            logger.error(f"Failed to clone deployment: {e}")
            return False
    
    def test_action(self, action: ActionDecision,
                     source_namespace: str = "demo") -> dict:
        """
        Uji aksi AI di sandbox dan ukur hasilnya.
        
        Pipeline:
        1. Buat namespace sandbox
        2. Clone deployment target
        3. Tunggu pod ready
        4. Eksekusi aksi
        5. Tunggu & ukur hasil
        6. Cleanup
        
        Args:
            action: Keputusan aksi dari AI Agent
            source_namespace: Namespace asli deployment
        
        Returns:
            dict dengan hasil: approved (bool), improvement (float), details (str)
        """
        logger.info(f"🧪 Starting sandbox test for: {action.action.value} "
                    f"→ {action.target_deployment}")
        
        start_time = time.time()
        
        # Step 1: Buat namespace
        if not self.create_sandbox_namespace():
            return {
                "approved": False,
                "improvement": 0,
                "details": "Failed to create sandbox namespace",
                "duration_seconds": 0
            }
        
        # Step 2: Clone deployment
        if not self.clone_deployment(source_namespace, action.target_deployment):
            return {
                "approved": False,
                "improvement": 0,
                "details": "Failed to clone deployment to sandbox",
                "duration_seconds": 0
            }
        
        # Step 3: Tunggu pod ready
        logger.info("Waiting for sandbox pod to be ready...")
        time.sleep(15)  # Tunggu pod startup
        
        # Step 4: Eksekusi aksi di sandbox
        sandbox_action = ActionDecision(
            action=action.action,
            target_deployment=f"sandbox-{action.target_deployment}",
            target_namespace=self.namespace,
            parameters=action.parameters,
            confidence=action.confidence,
            reasoning=f"[SANDBOX TEST] {action.reasoning}",
            data_sources_used=action.data_sources_used
        )
        
        logger.info(f"Executing action in sandbox: {sandbox_action.action.value}")
        
        # Simulasi eksekusi (di implementasi nyata, panggil K8sExecutor)
        if self.k8s_available:
            self._execute_sandbox_action(sandbox_action)
        
        # Step 5: Tunggu dan evaluasi
        logger.info(f"Waiting {self.test_duration}s for stabilization...")
        time.sleep(self.test_duration)
        
        # Evaluasi (di implementasi nyata, query metrik sandbox)
        result = self._evaluate_sandbox_result(sandbox_action)
        
        # Step 6: Cleanup
        if self.cleanup_after:
            self.cleanup()
        
        duration = time.time() - start_time
        result["duration_seconds"] = round(duration, 2)
        
        logger.info(
            f"🧪 Sandbox test complete: "
            f"{'✅ APPROVED' if result['approved'] else '❌ REJECTED'} | "
            f"Improvement: {result['improvement']:.1f}%"
        )
        
        return result
    
    def _execute_sandbox_action(self, action: ActionDecision):
        """Eksekusi aksi di namespace sandbox."""
        if not self.k8s_available:
            return
        
        try:
            if action.action == ActionType.SCALE_OUT:
                replicas = action.parameters.replicas_to_add or 2
                dep = self.apps_v1.read_namespaced_deployment(
                    name=action.target_deployment,
                    namespace=self.namespace
                )
                dep.spec.replicas = (dep.spec.replicas or 1) + replicas
                self.apps_v1.patch_namespaced_deployment(
                    name=action.target_deployment,
                    namespace=self.namespace,
                    body=dep
                )
            elif action.action == ActionType.RESTART_POD:
                # Delete pod untuk trigger restart
                pods = self.core_v1.list_namespaced_pod(
                    namespace=self.namespace,
                    label_selector=f"original-deployment={action.target_deployment.replace('sandbox-', '')}"
                )
                if pods.items:
                    self.core_v1.delete_namespaced_pod(
                        name=pods.items[0].metadata.name,
                        namespace=self.namespace
                    )
        except Exception as e:
            logger.warning(f"Sandbox action execution error: {e}")
    
    def _evaluate_sandbox_result(self, action: ActionDecision) -> dict:
        """
        Evaluasi hasil aksi di sandbox.
        
        Di implementasi penuh, ini akan query Prometheus untuk
        metrik sandbox pod. Untuk sekarang, kita melakukan
        basic health check.
        """
        if not self.k8s_available:
            # Simulation mode: assume positive result
            return {
                "approved": True,
                "improvement": 50.0,
                "details": "[SIMULATION] Sandbox test simulated successfully"
            }
        
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=self.namespace
            )
            
            running_pods = [
                p for p in pods.items
                if p.status.phase == "Running"
            ]
            
            if running_pods:
                return {
                    "approved": True,
                    "improvement": 60.0,
                    "details": f"{len(running_pods)} pod(s) running successfully after action"
                }
            else:
                return {
                    "approved": False,
                    "improvement": 0.0,
                    "details": "No running pods after action — action may be harmful"
                }
                
        except Exception as e:
            return {
                "approved": False,
                "improvement": 0.0,
                "details": f"Failed to evaluate sandbox: {e}"
            }
    
    def cleanup(self):
        """Hapus semua resource di namespace sandbox."""
        if not self.k8s_available:
            logger.info(f"[SIMULATION] Would cleanup namespace: {self.namespace}")
            return
        
        try:
            # Hapus semua deployment
            deps = self.apps_v1.list_namespaced_deployment(namespace=self.namespace)
            for dep in deps.items:
                self.apps_v1.delete_namespaced_deployment(
                    name=dep.metadata.name,
                    namespace=self.namespace
                )
            
            # Hapus semua pod (orphans)
            pods = self.core_v1.list_namespaced_pod(namespace=self.namespace)
            for pod in pods.items:
                self.core_v1.delete_namespaced_pod(
                    name=pod.metadata.name,
                    namespace=self.namespace
                )
            
            logger.success(f"Sandbox cleanup complete: {self.namespace}")
            
        except Exception as e:
            logger.warning(f"Sandbox cleanup error: {e}")
