# ============================================================
# XFSCI Multi-Step Planner — Plan + Checkpoint + Rollback
# ============================================================
# Modul ini menangani skenario kompleks yang membutuhkan
# beberapa aksi berurutan (2-10 langkah).
#
# MENGAPA LEBIH BAIK DARI RL MULTI-STEP?
# - Setiap langkah DIVALIDASI sebelum lanjut ke berikutnya
# - Jika satu langkah gagal → ROLLBACK otomatis ke checkpoint
# - RL tidak punya mekanisme rollback — sekali aksi, tidak bisa batal
#
# Contoh skenario multi-step:
#   1. Rate Limit (kurangi beban dulu)
#   2. Scale Out (tambah kapasitas)
#   3. Restart pod lama (clear memory leak)
#   4. Remove Rate Limit (buka trafik normal)
# ============================================================

import time
import uuid
import copy
from datetime import datetime
from typing import Optional

import yaml
from pathlib import Path
from loguru import logger

from agent.action_schema import (
    ActionType, ActionDecision, ActionParameters,
    MultiStepPlan, MultiStepAction,
    SituationReport, PandasMetrics
)


class Checkpoint:
    """
    Snapshot metrik pada titik waktu tertentu.
    Digunakan untuk rollback jika langkah berikutnya gagal.
    """
    
    def __init__(self, step_number: int, metrics: PandasMetrics,
                 action_taken: ActionType = None):
        self.step_number = step_number
        self.metrics = metrics
        self.action_taken = action_taken
        self.timestamp = datetime.utcnow()
    
    def __repr__(self):
        return (f"Checkpoint(step={self.step_number}, "
                f"action={self.action_taken}, "
                f"time={self.timestamp.isoformat()})")


class MultiStepPlanner:
    """
    Membuat dan mengeksekusi rencana multi-langkah dengan
    checkpoint di setiap tahap dan rollback jika gagal.
    """
    
    # Template rencana untuk skenario umum
    PLAN_TEMPLATES = {
        "memory_leak_critical": [
            {
                "action": ActionType.RATE_LIMIT,
                "reason": "Kurangi beban masuk untuk stabilisasi",
                "success_criteria": "error_rate < 10%",
                "rollback": ActionType.NO_OP,
                "wait": 15
            },
            {
                "action": ActionType.SCALE_OUT,
                "reason": "Tambah kapasitas sebelum restart pod bermasalah",
                "success_criteria": "replicas >= current + 2",
                "rollback": ActionType.SCALE_IN,
                "wait": 30
            },
            {
                "action": ActionType.RESTART_POD,
                "reason": "Restart pod yang terkena memory leak",
                "success_criteria": "memory_growth_rate <= 0",
                "rollback": ActionType.NO_OP,
                "wait": 30
            },
            {
                "action": ActionType.NO_OP,
                "reason": "Hapus rate limit, buka trafik normal kembali",
                "success_criteria": "latency_p99 < 300ms",
                "rollback": ActionType.RATE_LIMIT,
                "wait": 15
            }
        ],
        "cpu_overload_with_traffic": [
            {
                "action": ActionType.SCALE_OUT,
                "reason": "Tambah kapasitas untuk absorb traffic spike",
                "success_criteria": "cpu_usage < 70%",
                "rollback": ActionType.SCALE_IN,
                "wait": 30
            },
            {
                "action": ActionType.RATE_LIMIT,
                "reason": "Batasi trafik jika scale out belum cukup",
                "success_criteria": "latency_p99 < 500ms",
                "rollback": ActionType.NO_OP,
                "wait": 20
            }
        ],
        "crash_loop_investigation": [
            {
                "action": ActionType.SCALE_OUT,
                "reason": "Jaga availability selama investigasi crash",
                "success_criteria": "at least 2 healthy pods",
                "rollback": ActionType.SCALE_IN,
                "wait": 30
            },
            {
                "action": ActionType.ESCALATE,
                "reason": "Crash loop membutuhkan investigasi kode oleh developer",
                "success_criteria": "alert sent to operator",
                "rollback": ActionType.NO_OP,
                "wait": 5
            }
        ]
    }
    
    def __init__(self, config_path: str = None):
        """Inisialisasi planner."""
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        self.healing_config = config.get("healing", {})
        self.guardrails = self.healing_config.get("guardrails", {})
        self.checkpoints: list[Checkpoint] = []
        
        logger.info("MultiStepPlanner initialized")
    
    def should_use_multi_step(self, situation: SituationReport) -> bool:
        """
        Tentukan apakah situasi membutuhkan rencana multi-step.
        
        Multi-step digunakan jika:
        1. Lebih dari 1 jenis anomali terdeteksi bersamaan
        2. Urgency CRITICAL dan ada multiple issues
        3. Memory leak + high traffic (butuh rate limit dulu)
        """
        metrics = situation.pandas_metrics
        issues = 0
        
        if metrics.memory_growth_rate_mb_per_min > 5:
            issues += 1
        if metrics.cpu_usage_avg_5m > 80:
            issues += 1
        if metrics.pod_restarts_1h >= 3:
            issues += 1
        if metrics.latency_p99_ms > 500:
            issues += 1
        if metrics.error_rate_percent > 5:
            issues += 1
        
        # Multi-step jika ada 2+ masalah bersamaan
        needs_multi = issues >= 2
        
        if needs_multi:
            logger.info(f"Multi-step plan needed: {issues} concurrent issues detected")
        
        return needs_multi
    
    def select_template(self, situation: SituationReport) -> str:
        """Pilih template rencana berdasarkan situasi."""
        metrics = situation.pandas_metrics
        
        if (metrics.memory_growth_rate_mb_per_min > 5 and
            metrics.memory_usage_percent > 80):
            return "memory_leak_critical"
        
        if metrics.cpu_usage_avg_5m > 80 and metrics.request_rate_rps > 50:
            return "cpu_overload_with_traffic"
        
        if metrics.pod_restarts_1h >= 3:
            return "crash_loop_investigation"
        
        return "memory_leak_critical"  # Default
    
    def create_plan(self, situation: SituationReport,
                     deployment_name: str) -> MultiStepPlan:
        """
        Buat rencana multi-langkah berdasarkan situasi.
        
        Args:
            situation: Laporan situasi lengkap
            deployment_name: Nama deployment target
        
        Returns:
            MultiStepPlan dengan langkah-langkah tervalidasi
        """
        template_name = self.select_template(situation)
        template = self.PLAN_TEMPLATES.get(template_name, [])
        
        logger.info(f"Creating multi-step plan: {template_name} ({len(template)} steps)")
        
        steps = []
        total_wait = 0
        
        for i, step_template in enumerate(template):
            params = ActionParameters()
            
            # Set parameter khusus berdasarkan aksi
            if step_template["action"] == ActionType.SCALE_OUT:
                params.replicas_to_add = 2
            elif step_template["action"] == ActionType.SCALE_IN:
                params.replicas_to_remove = 2
            elif step_template["action"] == ActionType.RATE_LIMIT:
                params.rate_limit_rps = 100
            elif step_template["action"] == ActionType.RESTART_POD:
                params.restart_strategy = "rolling"
            
            step = MultiStepAction(
                step_number=i + 1,
                action=step_template["action"],
                target_deployment=deployment_name,
                parameters=params,
                reason=step_template["reason"],
                success_criteria=step_template["success_criteria"],
                rollback_action=step_template.get("rollback"),
                wait_seconds=step_template.get("wait", 30)
            )
            steps.append(step)
            total_wait += step.wait_seconds
        
        plan = MultiStepPlan(
            plan_id=f"plan-{uuid.uuid4().hex[:8]}",
            total_steps=len(steps),
            steps=steps,
            overall_reasoning=(
                f"Template: {template_name} | "
                f"Situation: urgency={situation.urgency_score:.0f}, "
                f"anomaly={situation.ml_prediction.anomaly_type.value}"
            ),
            estimated_duration_seconds=total_wait + 30,
            confidence=0.85
        )
        
        logger.info(
            f"Plan created: {plan.plan_id} | "
            f"{plan.total_steps} steps | "
            f"~{plan.estimated_duration_seconds}s estimated"
        )
        
        return plan
    
    def save_checkpoint(self, step_number: int,
                         metrics: PandasMetrics,
                         action_taken: ActionType = None):
        """Simpan checkpoint (snapshot metrik sebelum aksi)."""
        checkpoint = Checkpoint(
            step_number=step_number,
            metrics=copy.deepcopy(metrics),
            action_taken=action_taken
        )
        self.checkpoints.append(checkpoint)
        logger.info(f"Checkpoint saved: {checkpoint}")
    
    def evaluate_step_success(self, step: MultiStepAction,
                                metrics_before: PandasMetrics,
                                metrics_after: PandasMetrics) -> bool:
        """
        Evaluasi apakah langkah berhasil berdasarkan perbandingan metrik.
        
        Kriteria umum:
        - Error rate tidak naik lebih dari threshold
        - Latency tidak memburuk
        - Pod tidak tambah crash
        """
        # Error rate check
        err_increase_pct = self.guardrails.get("rollback_error_rate_increase_pct", 10)
        if (metrics_after.error_rate_percent >
            metrics_before.error_rate_percent + err_increase_pct):
            logger.warning(
                f"Step {step.step_number} FAILED: Error rate increased "
                f"{metrics_before.error_rate_percent:.1f}% → "
                f"{metrics_after.error_rate_percent:.1f}%"
            )
            return False
        
        # Latency check
        lat_increase_pct = self.guardrails.get("rollback_latency_increase_pct", 20)
        if metrics_before.latency_p99_ms > 0:
            lat_change = (
                (metrics_after.latency_p99_ms - metrics_before.latency_p99_ms) /
                metrics_before.latency_p99_ms * 100
            )
            if lat_change > lat_increase_pct:
                logger.warning(
                    f"Step {step.step_number} FAILED: Latency increased "
                    f"{lat_change:.0f}% (threshold: {lat_increase_pct}%)"
                )
                return False
        
        # Restart count check (tidak boleh bertambah)
        if metrics_after.pod_restarts_1h > metrics_before.pod_restarts_1h + 1:
            logger.warning(
                f"Step {step.step_number} FAILED: Pod restarts increased"
            )
            return False
        
        logger.success(f"Step {step.step_number} PASSED validation")
        return True
    
    def get_rollback_actions(self, failed_step: int) -> list[ActionDecision]:
        """
        Generate daftar aksi rollback dari checkpoint terakhir
        hingga checkpoint awal.
        
        Rollback dilakukan secara terbalik (LIFO):
        Jika step 1→2→3 gagal di step 3, rollback: undo 3 → undo 2 → undo 1
        """
        rollback_actions = []
        
        # Ambil checkpoint dari yang terbaru hingga terlama
        relevant_checkpoints = [
            cp for cp in reversed(self.checkpoints)
            if cp.step_number <= failed_step and cp.action_taken is not None
        ]
        
        for cp in relevant_checkpoints:
            # Tentukan aksi rollback berdasarkan aksi asli
            rollback_map = {
                ActionType.SCALE_OUT: ActionType.SCALE_IN,
                ActionType.SCALE_IN: ActionType.SCALE_OUT,
                ActionType.RATE_LIMIT: ActionType.NO_OP,  # Remove rate limit
                ActionType.RESTART_POD: ActionType.NO_OP,  # Can't un-restart
                ActionType.MIGRATE_POD: ActionType.NO_OP,  # Complex, skip
            }
            
            rollback_action = rollback_map.get(cp.action_taken)
            if rollback_action and rollback_action != ActionType.NO_OP:
                params = ActionParameters()
                if rollback_action == ActionType.SCALE_IN:
                    params.replicas_to_remove = 2
                elif rollback_action == ActionType.SCALE_OUT:
                    params.replicas_to_add = 2
                
                rollback_actions.append(ActionDecision(
                    action=rollback_action,
                    target_deployment=cp.metrics.target_pod,
                    parameters=params,
                    confidence=0.95,
                    reasoning=f"Rollback step {cp.step_number}: undo {cp.action_taken.value}",
                    data_sources_used=["multi_step_planner", "checkpoint_rollback"]
                ))
        
        if rollback_actions:
            logger.warning(
                f"Rollback plan: {len(rollback_actions)} actions to undo "
                f"(from step {failed_step} back to start)"
            )
        
        return rollback_actions
    
    def clear_checkpoints(self):
        """Hapus semua checkpoint setelah plan selesai."""
        count = len(self.checkpoints)
        self.checkpoints.clear()
        logger.info(f"Cleared {count} checkpoints")
