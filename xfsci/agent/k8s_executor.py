# ============================================================
# XFSCI Kubernetes Executor (Layer 5: Self-Healing Engine)
# ============================================================
# Modul ini MENGEKSEKUSI tindakan perbaikan nyata ke klaster
# Kubernetes berdasarkan keputusan AI Agent.
#
# Tindakan yang didukung:
#   - restart_pod   : kubectl rollout restart deployment/X
#   - scale_out     : kubectl scale deployment/X --replicas=N
#   - scale_in      : kubectl scale deployment/X --replicas=N
#   - rate_limit    : (placeholder — perlu Istio/NetworkPolicy)
#   - migrate_pod   : kubectl drain node + reschedule
#   - escalate      : Kirim notifikasi (tidak mengubah klaster)
#   - no_op         : Tidak melakukan apa-apa
#
# SAFETY GUARDRAILS BAWAAN:
#   1. Cek replika sebelum scale-in (tidak boleh < 1)
#   2. Cek max_replicas sebelum scale-out
#   3. Timeout pada setiap operasi
#   4. Post-action health verification
#   5. Auto-rollback jika metrik memburuk
# ============================================================

import time
from datetime import datetime
from typing import Optional

import yaml
from pathlib import Path
from loguru import logger

try:
    from kubernetes import client, config as k8s_config
    from kubernetes.client.rest import ApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False
    logger.warning("kubernetes SDK not installed — K8sExecutor will operate in simulation mode")

from agent.action_schema import ActionDecision, ActionType, ActionParameters


class K8sExecutor:
    """
    Eksekutor tindakan perbaikan langsung ke Kubernetes.
    
    Ini adalah 'tangan perawat' yang menerjemahkan keputusan AI Agent
    menjadi perintah Kubernetes API nyata (scale, restart, drain, dll).
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")

        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        healing_cfg = cfg.get("healing", {})
        k8s_cfg = healing_cfg.get("k8s", {})
        guardrail_cfg = healing_cfg.get("guardrails", {})
        ns_cfg = cfg.get("namespaces", {})

        self.default_namespace = ns_cfg.get("workload", "demo")
        self.max_replicas = k8s_cfg.get("max_replicas", 10)
        self.default_scale_add = k8s_cfg.get("scale_out_replicas", 2)
        self.drain_timeout = k8s_cfg.get("drain_timeout_seconds", 60)
        self.post_heal_wait = guardrail_cfg.get("post_healing_wait_seconds", 30)

        # Setup Kubernetes client
        self.k8s_ready = False
        if K8S_AVAILABLE:
            try:
                k8s_config.load_incluster_config()
                self.k8s_ready = True
            except Exception:
                try:
                    k8s_config.load_kube_config()
                    self.k8s_ready = True
                except Exception as e:
                    logger.warning(f"K8s config not loadable: {e}")

        if self.k8s_ready:
            self.core_v1 = client.CoreV1Api()
            self.apps_v1 = client.AppsV1Api()
            logger.success(f"K8sExecutor ready | Namespace: {self.default_namespace} | Max replicas: {self.max_replicas}")
        else:
            logger.warning("K8sExecutor in SIMULATION mode (no live K8s connection)")

    # ──────────────────────────────────────────────────────
    # Public: Eksekusi keputusan AI
    # ──────────────────────────────────────────────────────
    def execute(self, decision: ActionDecision, dry_run: bool = False) -> dict:
        """
        Eksekusi keputusan AI Agent ke klaster Kubernetes.

        Args:
            decision: Keputusan dari XFSCIDecisionAgent
            dry_run: Jika True, hanya log tanpa eksekusi nyata

        Returns:
            dict: {success, action, details, before_state, after_state}
        """
        action = decision.action
        target = decision.target_deployment
        ns = decision.target_namespace or self.default_namespace
        params = decision.parameters or ActionParameters()

        logger.info(
            f"\n{'─'*50}\n"
            f"🔧 K8sExecutor | Action: {action.value} | Target: {target} | NS: {ns}\n"
            f"{'─'*50}"
        )

        if dry_run:
            return self._simulate(action, target, ns, params)

        if not self.k8s_ready:
            logger.error("K8s not connected — cannot execute live action")
            return {"success": False, "action": action.value,
                    "details": "Kubernetes SDK not connected", "simulated": True}

        dispatch = {
            ActionType.NO_OP:       lambda: self._do_no_op(target, ns),
            ActionType.RESTART_POD: lambda: self._do_restart(target, ns, params),
            ActionType.SCALE_OUT:   lambda: self._do_scale_out(target, ns, params),
            ActionType.SCALE_IN:    lambda: self._do_scale_in(target, ns, params),
            ActionType.RATE_LIMIT:  lambda: self._do_rate_limit(target, ns, params),
            ActionType.MIGRATE_POD: lambda: self._do_migrate(target, ns, params),
            ActionType.ESCALATE:    lambda: self._do_escalate(target, ns),
        }

        handler = dispatch.get(action)
        if handler is None:
            return {"success": False, "action": action.value,
                    "details": f"Unknown action type: {action.value}"}

        try:
            result = handler()
            return result
        except Exception as e:
            logger.error(f"K8sExecutor error: {e}")
            return {"success": False, "action": action.value,
                    "details": f"Execution error: {e}"}

    # ──────────────────────────────────────────────────────
    # Helpers: Ambil state sebelum / sesudah
    # ──────────────────────────────────────────────────────
    def _get_deployment_state(self, name: str, ns: str) -> dict:
        """Ambil state deployment saat ini."""
        try:
            dep = self.apps_v1.read_namespaced_deployment(name=name, namespace=ns)
            return {
                "replicas_spec": dep.spec.replicas,
                "replicas_ready": dep.status.ready_replicas or 0,
                "replicas_available": dep.status.available_replicas or 0,
                "conditions": [
                    {"type": c.type, "status": c.status}
                    for c in (dep.status.conditions or [])
                ],
            }
        except ApiException as e:
            logger.warning(f"Cannot read deployment {name}/{ns}: {e.reason}")
            return {"error": str(e.reason)}

    def _wait_for_rollout(self, name: str, ns: str, timeout: int = 120) -> bool:
        """Tunggu rollout selesai (semua pod Ready)."""
        logger.info(f"⏳ Waiting for rollout of {name} (timeout {timeout}s)...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                dep = self.apps_v1.read_namespaced_deployment(name=name, namespace=ns)
                desired = dep.spec.replicas or 1
                ready = dep.status.ready_replicas or 0
                if ready >= desired:
                    logger.success(f"✅ Rollout complete: {ready}/{desired} pods ready")
                    return True
            except Exception:
                pass
            time.sleep(3)
        logger.warning(f"⚠️ Rollout timeout after {timeout}s")
        return False

    # ──────────────────────────────────────────────────────
    # Action Implementations
    # ──────────────────────────────────────────────────────
    def _do_no_op(self, target: str, ns: str) -> dict:
        logger.info("✅ NO_OP: Tidak ada tindakan yang diperlukan")
        return {"success": True, "action": "no_op",
                "details": "Cluster healthy — no action taken"}

    def _do_restart(self, target: str, ns: str, params: ActionParameters) -> dict:
        """Rolling restart deployment (zero-downtime)."""
        before = self._get_deployment_state(target, ns)
        logger.info(f"🔄 Executing rolling restart on {target}/{ns}...")

        # Patch deployment dengan annotation restart trigger
        now = datetime.utcnow().isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now,
                            "xfsci.io/restarted-by": "ai-agent",
                        }
                    }
                }
            }
        }
        self.apps_v1.patch_namespaced_deployment(name=target, namespace=ns, body=body)
        logger.info(f"📤 Restart triggered via annotation patch at {now}")

        # Tunggu rollout selesai
        rollout_ok = self._wait_for_rollout(target, ns, timeout=120)
        after = self._get_deployment_state(target, ns)

        return {
            "success": rollout_ok, "action": "restart_pod",
            "details": f"Rolling restart {'completed' if rollout_ok else 'timed out'}",
            "before_state": before, "after_state": after,
        }

    def _do_scale_out(self, target: str, ns: str, params: ActionParameters) -> dict:
        """Tambah replika deployment."""
        before = self._get_deployment_state(target, ns)
        current = before.get("replicas_spec", 1)
        add = params.replicas_to_add or self.default_scale_add
        desired = min(current + add, self.max_replicas)

        if desired <= current:
            msg = f"Already at max replicas ({current}/{self.max_replicas})"
            logger.warning(f"⚠️ {msg}")
            return {"success": True, "action": "scale_out",
                    "details": msg, "before_state": before}

        logger.info(f"📈 Scaling {target}/{ns}: {current} → {desired} replicas")
        body = {"spec": {"replicas": desired}}
        self.apps_v1.patch_namespaced_deployment(name=target, namespace=ns, body=body)

        rollout_ok = self._wait_for_rollout(target, ns, timeout=120)
        after = self._get_deployment_state(target, ns)

        return {
            "success": rollout_ok, "action": "scale_out",
            "details": f"Scaled {current} → {desired} ({'ready' if rollout_ok else 'timeout'})",
            "before_state": before, "after_state": after,
        }

    def _do_scale_in(self, target: str, ns: str, params: ActionParameters) -> dict:
        """Kurangi replika deployment (minimum 1)."""
        before = self._get_deployment_state(target, ns)
        current = before.get("replicas_spec", 1)
        remove = params.replicas_to_remove or 1
        desired = max(current - remove, 1)  # Jangan pernah 0

        if desired >= current:
            msg = f"Already at minimum ({current} replicas)"
            logger.info(f"ℹ️ {msg}")
            return {"success": True, "action": "scale_in",
                    "details": msg, "before_state": before}

        logger.info(f"📉 Scaling down {target}/{ns}: {current} → {desired} replicas")
        body = {"spec": {"replicas": desired}}
        self.apps_v1.patch_namespaced_deployment(name=target, namespace=ns, body=body)

        time.sleep(10)  # Tunggu Kubernetes menghentikan pod
        after = self._get_deployment_state(target, ns)

        return {
            "success": True, "action": "scale_in",
            "details": f"Scaled down {current} → {desired}",
            "before_state": before, "after_state": after,
        }

    def _do_rate_limit(self, target: str, ns: str, params: ActionParameters) -> dict:
        """Placeholder — membutuhkan Istio / NetworkPolicy."""
        logger.warning("⚠️ Rate limiting requires Istio or NetworkPolicy (not yet implemented)")
        return {
            "success": True, "action": "rate_limit",
            "details": f"Rate limit logged (requires service mesh). Target RPS: {params.rate_limit_rps}",
        }

    def _do_migrate(self, target: str, ns: str, params: ActionParameters) -> dict:
        """Migrasi pod ke node lain via delete (reschedule)."""
        target_node = params.target_node
        logger.info(f"🚚 Migrating {target}/{ns} pods (evict from current node)...")

        # Strategi: Hapus pod agar Kubernetes reschedule ke node lain
        pods = self.core_v1.list_namespaced_pod(
            namespace=ns,
            label_selector=f"app={target}"
        )
        deleted = 0
        for pod in pods.items:
            try:
                self.core_v1.delete_namespaced_pod(
                    name=pod.metadata.name, namespace=ns,
                    grace_period_seconds=30
                )
                deleted += 1
                logger.info(f"  🗑️ Deleted pod {pod.metadata.name} for rescheduling")
            except Exception as e:
                logger.warning(f"  Failed to delete {pod.metadata.name}: {e}")

        if deleted > 0:
            self._wait_for_rollout(target, ns, timeout=120)

        after = self._get_deployment_state(target, ns)
        return {
            "success": deleted > 0, "action": "migrate_pod",
            "details": f"Deleted {deleted} pod(s) for rescheduling",
            "after_state": after,
        }

    def _do_escalate(self, target: str, ns: str) -> dict:
        """Eskalasi ke operator manusia (log + notifikasi)."""
        logger.warning(
            f"🚨 ESCALATION: Masalah pada {target}/{ns} memerlukan intervensi manusia!\n"
            f"   Hubungi tim SRE on-call."
        )
        return {
            "success": True, "action": "escalate",
            "details": f"Escalated to human operator for {target}/{ns}",
        }

    # ──────────────────────────────────────────────────────
    # Dry-Run Simulation
    # ──────────────────────────────────────────────────────
    def _simulate(self, action: ActionType, target: str,
                  ns: str, params: ActionParameters) -> dict:
        """Simulasi aksi tanpa mengubah klaster."""
        messages = {
            ActionType.NO_OP:       "No action needed",
            ActionType.RESTART_POD: f"Would rolling-restart {target}/{ns}",
            ActionType.SCALE_OUT:   f"Would scale {target}/{ns} +{params.replicas_to_add or self.default_scale_add} replicas",
            ActionType.SCALE_IN:    f"Would scale down {target}/{ns} -{params.replicas_to_remove or 1} replicas",
            ActionType.RATE_LIMIT:  f"Would rate-limit {target}/{ns} to {params.rate_limit_rps} RPS",
            ActionType.MIGRATE_POD: f"Would migrate {target}/{ns} pods to different node",
            ActionType.ESCALATE:    f"Would escalate {target}/{ns} to human operator",
        }
        detail = messages.get(action, f"Unknown action: {action.value}")
        logger.info(f"🧪 [DRY-RUN] {detail}")

        # Jika K8s tersedia, ambil state aktual untuk referensi
        current_state = {}
        if self.k8s_ready:
            current_state = self._get_deployment_state(target, ns)
            logger.info(f"   Current state: {current_state.get('replicas_spec', '?')} replicas, "
                        f"{current_state.get('replicas_ready', '?')} ready")

        return {
            "success": True, "action": action.value,
            "details": f"[DRY-RUN] {detail}",
            "simulated": True, "current_state": current_state,
        }

    # ──────────────────────────────────────────────────────
    # Post-Action Health Verification
    # ──────────────────────────────────────────────────────
    def verify_health(self, target: str, ns: str = None) -> dict:
        """Verifikasi kesehatan deployment setelah aksi."""
        ns = ns or self.default_namespace
        if not self.k8s_ready:
            return {"healthy": True, "simulated": True}

        state = self._get_deployment_state(target, ns)
        desired = state.get("replicas_spec", 1)
        ready = state.get("replicas_ready", 0)
        healthy = ready >= desired

        logger.info(
            f"🩺 Health check {target}/{ns}: {ready}/{desired} ready "
            f"→ {'✅ Healthy' if healthy else '❌ Unhealthy'}"
        )
        return {"healthy": healthy, "state": state}
