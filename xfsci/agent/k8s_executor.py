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
import re
import sys
from datetime import datetime, timezone
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
        self.approval_required_actions = set(
            healing_cfg.get("approval_required_actions", ["scale_in", "migrate_pod"])
        )
        self.allowed_namespaces = set(
            healing_cfg.get("allowed_namespaces", [self.default_namespace])
        )
        self.max_autonomous_scale_out = int(k8s_cfg.get("max_autonomous_scale_out", 2))
        self.restart_cooldown_seconds = int(k8s_cfg.get("restart_cooldown_seconds", 900))

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

    @staticmethod
    def _redact_log_text(value: str, limit: int = 3000) -> str:
        """Batasi dan sensor nilai sensitif sebelum bukti dikirim ke LLM."""
        value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
        value = re.sub(
            r"(?i)([\"']?(?:password|passwd|token|secret|api[_-]?key|access[_-]?key|credential|private[_-]?key)[\"']?\s*:\s*[\"'])[^\"']*([\"'])",
            r"\1[REDACTED]\2", value,
        )
        value = re.sub(
            r"(?i)(password|passwd|token|secret|api[_-]?key|access[_-]?key|credential|private[_-]?key)(\s*[:=]\s*)[^\s,;]+",
            r"\1\2[REDACTED]", value,
        )
        return value[-limit:]

    def collect_incident_evidence(self, deployment: str, namespace: str,
                                  pod_name: str = None) -> dict:
        """Ambil bukti Kubernetes read-only, dibatasi dan disensor, untuk diagnosis."""
        evidence = {
            "available": False, "namespace": namespace,
            "deployment": deployment, "pods": [], "events": [],
            "diagnosis": "unknown", "diagnosis_summary": "Bukti Kubernetes tidak tersedia.",
        }
        if not self.k8s_ready:
            evidence["diagnosis_summary"] = "Kubernetes API tidak terhubung; jangan jalankan remediasi otomatis berbasis asumsi."
            return evidence

        try:
            dep = self.apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)
            selector = (dep.spec.selector.match_labels or {}) if dep.spec.selector else {}
            label_selector = ",".join(f"{key}={value}" for key, value in selector.items())
            pods = []
            if pod_name:
                try:
                    requested_pod = self.core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                    pod_labels = requested_pod.metadata.labels or {}
                    if all(pod_labels.get(key) == value for key, value in selector.items()):
                        pods = [requested_pod]
                except ApiException:
                    pods = []
            if not pods and label_selector:
                pods = self.core_v1.list_namespaced_pod(
                    namespace=namespace, label_selector=label_selector, limit=3
                ).items

            evidence["available"] = True
            evidence["deployment_state"] = {
                "desired_replicas": dep.spec.replicas or 0,
                "ready_replicas": dep.status.ready_replicas or 0,
                "available_replicas": dep.status.available_replicas or 0,
                "strategy": getattr(getattr(dep.spec, "strategy", None), "type", None),
            }
            evidence["pods"] = []
            for pod in pods[:5]:
                item = {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "node": pod.spec.node_name,
                    "conditions": [
                        {"type": c.type, "status": c.status, "reason": c.reason}
                        for c in (pod.status.conditions or [])
                        if c.type in ("Ready", "ContainersReady", "PodScheduled")
                    ],
                    "containers": [],
                    "events": [],
                }
                for status in (pod.status.container_statuses or []):
                    state = getattr(status, "state", None)
                    last_state = getattr(status, "last_state", None)
                    terminated = getattr(state, "terminated", None) if state else None
                    if not terminated and last_state:
                        terminated = getattr(last_state, "terminated", None)
                    waiting = getattr(state, "waiting", None) if state else None
                    container = {
                        "name": getattr(status, "name", ""),
                        "ready": getattr(status, "ready", False),
                        "restart_count": getattr(status, "restart_count", 0),
                        "waiting_reason": getattr(waiting, "reason", None) if waiting else None,
                        "waiting_message": self._redact_log_text(getattr(waiting, "message", "") or "", 500) if waiting else None,
                        "last_termination_reason": getattr(terminated, "reason", None) if terminated else None,
                        "last_exit_code": getattr(terminated, "exit_code", None) if terminated else None,
                        "last_finished_at": str(getattr(terminated, "finished_at", None)) if terminated and getattr(terminated, "finished_at", None) else None,
                    }
                    try:
                        current_log = self.core_v1.read_namespaced_pod_log(
                            name=pod.metadata.name, namespace=namespace,
                            container=status.name, tail_lines=30, timestamps=True,
                        )
                        container["current_log"] = self._redact_log_text(current_log or "")
                    except Exception as exc:
                        container["current_log_error"] = type(exc).__name__
                    if status.restart_count:
                        try:
                            previous_log = self.core_v1.read_namespaced_pod_log(
                                name=pod.metadata.name, namespace=namespace,
                                container=status.name, tail_lines=30, timestamps=True,
                                previous=True,
                            )
                            container["previous_log"] = self._redact_log_text(previous_log or "")
                        except Exception as exc:
                            container["previous_log_error"] = type(exc).__name__
                    item["containers"].append(container)

                try:
                    pod_events = self.core_v1.list_namespaced_event(
                        namespace=namespace,
                        field_selector=f"involvedObject.name={pod.metadata.name}",
                        limit=30,
                    ).items
                    item["events"] = [
                        {"type": ev.type, "reason": ev.reason,
                         "message": self._redact_log_text(ev.message or "", 300),
                         "count": ev.count, "last_timestamp": str(ev.last_timestamp or ev.event_time or "")}
                        for ev in pod_events[-8:]
                    ]
                except Exception:
                    pass
                evidence["pods"].append(item)

            # Klasifikasi berbasis bukti eksplisit; tidak mengasumsikan restart akan menyelesaikan masalah.
            texts = []
            for pod in evidence["pods"]:
                texts.extend(str(c.get(k) or "") for c in pod["containers"]
                             for k in ("waiting_reason", "waiting_message", "last_termination_reason", "current_log", "previous_log"))
                texts.extend(str(ev.get("reason", "")) + " " + str(ev.get("message", "")) for ev in pod["events"])
            diagnostic_text = "\n".join(texts).lower()
            if "oomkilled" in diagnostic_text:
                diagnosis = "oom_killed"
                summary = "Container terakhir dihentikan karena OOMKilled; restart/scale-out saja tidak memperbaiki batas memori atau kebocoran."
            elif any(x in diagnostic_text for x in ("imagepullbackoff", "errimagepull", "failed to pull image")):
                diagnosis = "image_pull"
                summary = "Bukti menunjukkan image gagal ditarik; periksa image/tag dan kredensial registry."
            elif any(x in diagnostic_text for x in ("failedmount", "failed to mount", "failed to attach", "configmap", "secret not found")):
                diagnosis = "config_mount"
                summary = "Bukti menunjukkan volume/config/secret gagal dipasang; periksa resource konfigurasi."
            elif any(x in diagnostic_text for x in ("connection refused", "name or service not known", "temporary failure in name resolution", "deadline exceeded", "connection reset")):
                diagnosis = "dependency_or_network"
                summary = "Log menunjukkan kegagalan koneksi/dependency; verifikasi dependency dan jaringan sebelum restart."
            elif any(x in diagnostic_text for x in ("traceback", "uncaught exception", "fatal error", "syntaxerror", "unhandled exception")):
                diagnosis = "application_error"
                summary = "Log menunjukkan error aplikasi; jika berulang, perlu investigasi kode/config sebelum remediasi otomatis."
            elif (
                any(ev.get("reason") == "Unhealthy" for pod in evidence["pods"] for ev in pod["events"])
                and any(
                    condition.get("type") == "Ready" and condition.get("status") != "True"
                    for pod in evidence["pods"] for condition in pod["conditions"]
                )
            ):
                diagnosis = "probe_failure"
                summary = "Event Unhealthy dan status Ready saat ini gagal; restart terbatas dapat dipertimbangkan bila deployment masih memiliki pod Ready lain."
            elif any((c.get("waiting_reason") == "CrashLoopBackOff") for pod in evidence["pods"] for c in pod["containers"]):
                diagnosis = "crash_loop_unknown"
                summary = "CrashLoopBackOff terkonfirmasi, tetapi bukti belum cukup untuk memastikan penyebabnya."
            else:
                diagnosis = "unknown"
                summary = "Tidak ditemukan penyebab eksplisit pada log/event yang diambil; jangan mengarang root cause."
            evidence["diagnosis"] = diagnosis
            evidence["diagnosis_summary"] = summary
            return evidence
        except Exception as exc:
            logger.warning(f"Failed to collect K8s incident evidence: {type(exc).__name__} - {exc}")
            evidence["error"] = type(exc).__name__
            evidence["diagnosis_summary"] = f"Gagal membaca bukti Kubernetes ({type(exc).__name__}: {exc}); tindakan otomatis berbasis diagnosis diblokir."
            return evidence

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

        if action in (ActionType.MIGRATE_POD, ActionType.RATE_LIMIT):
            return {"success": False, "action": action.value,
                    "details": f"Blocked: {action.value} has no safe operational implementation; no cluster change was made.",
                    "simulated": dry_run}

        if dry_run:
            return self._simulate(action, target, ns, params)

        if ns not in self.allowed_namespaces:
            return {"success": False, "action": action.value,
                    "details": f"Blocked: namespace '{ns}' is outside the healing allowlist {sorted(self.allowed_namespaces)}"}
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", target or ""):
            return {"success": False, "action": action.value,
                    "details": "Blocked: invalid Kubernetes deployment name"}
        if action == ActionType.SCALE_OUT and (params.replicas_to_add or self.default_scale_add) > self.max_autonomous_scale_out:
            approval_reason = (
                f"Permintaan scale_out +{params.replicas_to_add} melebihi batas otomatis "
                f"+{self.max_autonomous_scale_out}."
            )
        else:
            approval_reason = None

        if action.value in self.approval_required_actions or approval_reason:
            if not self._request_operator_approval(decision, approval_reason):
                return {"success": False, "action": action.value,
                        "details": "Blocked: persetujuan operator tidak diberikan atau terminal tidak interaktif",
                        "approval_required": True}

        if action == ActionType.RESTART_POD:
            preflight = self._restart_preflight(target, ns)
            if not preflight["allowed"]:
                return {"success": False, "action": action.value,
                        "details": preflight["reason"],
                        "approval_required": preflight.get("approval_required", False)}

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

    @staticmethod
    def _request_operator_approval(decision: ActionDecision, extra_reason: str = None) -> bool:
        """Require a human at the terminal for configured high-impact changes."""
        reason = extra_reason or "aksi ini dapat mengurangi kapasitas atau mengganggu banyak pod"
        prompt = (
            f"\nPERSETUJUAN DIPERLUKAN: {decision.action.value} "
            f"{decision.target_deployment}/{decision.target_namespace}\n"
            f"Alasan: {reason}\nAI: {decision.reasoning}\n"
            "Ketik 'approve' untuk menjalankan aksi ini: "
        )
        if not sys.stdin.isatty():
            logger.warning("High-impact action blocked: no interactive operator approval channel")
            return False
        try:
            return input(prompt).strip().lower() == "approve"
        except (EOFError, KeyboardInterrupt):
            return False

    def _restart_preflight(self, target: str, ns: str) -> dict:
        """Guard restart by availability and a durable per-deployment cooldown."""
        try:
            dep = self.apps_v1.read_namespaced_deployment(name=target, namespace=ns)
            desired = dep.spec.replicas or 0
            ready = dep.status.ready_replicas or 0
            if ready < 1 or desired < 1:
                return {"allowed": False, "reason": "Restart blocked: deployment has no Ready pod; collect evidence and escalate."}
            strategy = getattr(getattr(dep.spec, "strategy", None), "type", None)
            if strategy not in (None, "RollingUpdate"):
                return {"allowed": False,
                        "reason": f"Restart blocked: deployment strategy '{strategy}' is not confirmed as RollingUpdate."}
            if desired < 2:
                ok = self._request_operator_approval(
                    ActionDecision(action=ActionType.RESTART_POD, target_deployment=target,
                                   target_namespace=ns, confidence=1.0,
                                   reasoning="Restart deployment with fewer than two replicas may cause downtime.")
                )
                if not ok:
                    return {"allowed": False, "approval_required": True,
                            "reason": "Restart blocked: one-replica deployment needs explicit operator approval."}
            annotations = (dep.spec.template.metadata.annotations or {}) if dep.spec.template.metadata else {}
            last_restart = annotations.get("xfsci.io/last-auto-restart-at")
            if last_restart:
                try:
                    last = datetime.fromisoformat(last_restart.replace("Z", "+00:00"))
                    if (datetime.now(last.tzinfo) - last).total_seconds() < self.restart_cooldown_seconds:
                        return {"allowed": False,
                                "reason": f"Restart blocked: deployment cooldown is {self.restart_cooldown_seconds}s."}
                except ValueError:
                    logger.warning("Invalid restart cooldown annotation; continuing with availability guard")
            return {"allowed": True}
        except Exception as exc:
            return {"allowed": False, "reason": f"Restart blocked: preflight could not verify deployment ({type(exc).__name__})."}

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
        now = datetime.now(timezone.utc).isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now,
                            "xfsci.io/restarted-by": "ai-agent",
                            "xfsci.io/last-auto-restart-at": now,
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
            "success": False, "action": "rate_limit",
            "details": f"Rate limit logged (requires service mesh). Target RPS: {params.rate_limit_rps}",
        }

    def _do_migrate(self, target: str, ns: str, params: ActionParameters) -> dict:
        """Migration needs explicit node-affinity/eviction support; do not delete pods as a proxy."""
        logger.error("migrate_pod is disabled until a safe node-targeted eviction implementation exists")
        return {
            "success": False, "action": "migrate_pod",
            "details": "Migration is not implemented safely; no pods were deleted.",
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
        requires_approval = action.value in self.approval_required_actions
        if action == ActionType.SCALE_OUT and (params.replicas_to_add or self.default_scale_add) > self.max_autonomous_scale_out:
            requires_approval = True
        if requires_approval:
            detail += " (operator approval required before live execution)"
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
