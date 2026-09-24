# ============================================================
# XFSCI Orchestrator — Main Pipeline
# ============================================================
# OTAK UTAMA yang menyatukan seluruh komponen AI Agent:
#
#  ┌─────────┐  ┌────────┐  ┌─────────┐  ┌──────────┐
#  │ Pandas  │→ │Scoring │→ │  RAG +  │→ │Guardrails│→ EXECUTE
#  │Processor│  │ Engine │  │ Gemini  │  │  Check   │
#  └─────────┘  └────────┘  │ Agent   │  └──────────┘
#                            └─────────┘
#                               │ ↑
#                    ┌──────────┘ └──────────┐
#                    │                       │
#              ┌───────────┐          ┌────────────┐
#              │ Sandbox   │          │ Experience │
#              │ (if new)  │          │  Memory    │
#              └───────────┘          └────────────┘
#
# Pipeline lengkap:
# 1. Terima alert/trigger
# 2. Pandas menganalisis metrik (100% eksak)
# 3. Scoring Engine menghitung urgency (deterministik)
# 4. RAG mengambil runbook SOP yang relevan
# 5. Experience Memory mencari pengalaman serupa
# 6. AI Agent (Gemini) memilih aksi
# 7. Jika masalah baru → Sandbox test dulu
# 8. Jika multi-step → Planner dengan checkpoint
# 9. Guardrails memvalidasi keputusan
# 10. Eksekusi + Evaluasi + Simpan ke Experience Memory
# ============================================================

import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import yaml
from pathlib import Path
from loguru import logger

# Pastikan root direktori proyek ada di sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.action_schema import (
    ActionDecision, ActionType, SituationReport,
    PandasMetrics, MLPrediction, UrgencyLevel,
    AnomalyType, PostActionResult, EscalationAlert
)
from agent.pandas_processor import PandasMetricProcessor
from agent.scoring_engine import DeterministicScoringEngine
from agent.decision_agent import XFSCIDecisionAgent
from agent.sandbox import DryRunSandbox
from agent.precision_optimizer import PrecisionOptimizer
from agent.multi_step_planner import MultiStepPlanner
from agent.experience_memory import ExperienceMemory

# Conditional import for RAG
try:
    from knowledge_base.rag_indexer import query_runbooks
    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False
    logger.warning("RAG indexer not available, running without runbook retrieval")


class XFSCIOrchestrator:
    """
    Pipeline utama XFSCI yang mengorkestrasi seluruh komponen
    AI Agent dari input (alert) hingga output (aksi tereksekusi).
    
    Ini adalah titik masuk tunggal (single entry point) untuk
    seluruh sistem self-healing.
    """
    
    def __init__(self, config_path: str = None):
        """Inisialisasi semua komponen."""
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        
        self.healing_config = self.config.get("healing", {})
        self.guardrail_config = self.healing_config.get("guardrails", {})
        
        # Inisialisasi semua komponen
        logger.info("=" * 60)
        logger.info("Initializing XFSCI Orchestrator...")
        logger.info("=" * 60)
        
        self.pandas_processor = PandasMetricProcessor(config_path)
        self.scoring_engine = DeterministicScoringEngine(config_path)
        self.decision_agent = XFSCIDecisionAgent(config_path)
        self.sandbox = DryRunSandbox(config_path)
        self.precision_optimizer = PrecisionOptimizer(config_path)
        self.multi_step_planner = MultiStepPlanner(config_path)
        self.experience_memory = ExperienceMemory(config_path)
        
        # Action history untuk cooldown tracking
        self.action_history: list[dict] = []
        # Circuit breaker counter
        self.consecutive_failures: dict[str, int] = {}
        
        logger.success("XFSCI Orchestrator fully initialized!")
    
    def handle_alert(self, deployment_name: str,
                      pod_name: str = None,
                      node_name: str = "unknown",
                      ml_prediction: MLPrediction = None) -> dict:
        """
        FUNGSI UTAMA: Handle alert dari monitoring system.
        
        Ini adalah satu-satunya fungsi yang perlu dipanggil
        dari luar. Seluruh pipeline dijalankan secara otomatis.
        
        Args:
            deployment_name: Nama deployment yang bermasalah
            pod_name: Nama pod spesifik (opsional)
            node_name: Nama node tempat pod berjalan
            ml_prediction: Prediksi dari ML model (opsional)
        
        Returns:
            dict berisi: decision, explanation, post_action_result
        """
        start_time = time.time()
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🚨 ALERT RECEIVED: {deployment_name}")
        logger.info(f"{'='*60}")
        
        # ===== TAHAP 1: Pandas Metric Analysis =====
        logger.info("[1/7] 📊 Collecting metrics via Pandas...")
        try:
            pandas_metrics = self.pandas_processor.process_realtime_metrics(
                deployment_name=deployment_name,
                pod_name=pod_name,
                node_name=node_name
            )
        except Exception as e:
            logger.error(f"Pandas processing failed: {e}")
            # Fallback: Buat metrik default
            pandas_metrics = PandasMetrics(
                timestamp=datetime.utcnow(),
                target_pod=pod_name or deployment_name,
                target_node=node_name,
                cpu_usage_avg_5m=0, cpu_usage_avg_15m=0,
                memory_usage_mb=0, memory_growth_rate_mb_per_min=0,
                memory_usage_percent=0, pod_restarts_1h=0,
                pod_age_minutes=0, current_replicas=1,
                request_rate_rps=0, error_rate_percent=0,
                latency_p50_ms=0, latency_p99_ms=0
            )
        
        # ===== TAHAP 2: ML Prediction (jika tidak disediakan) =====
        if ml_prediction is None:
            logger.info("[2/7] 🧠 Using Pandas-detected anomaly (no ML model yet)...")
            anomaly = self.pandas_processor.detect_anomaly_pattern(
                pandas_metrics.model_dump()
            )
            ml_prediction = MLPrediction(
                risk_score=0.5 if anomaly != AnomalyType.NORMAL else 0.1,
                anomaly_type=anomaly,
                confidence=0.80,
                time_to_failure_minutes=None,
                cascade_risk=[]
            )
        else:
            logger.info("[2/7] 🧠 Using provided ML prediction")
        
        # ===== TAHAP 3: Urgency Scoring =====
        logger.info("[3/7] 📐 Calculating urgency score...")
        urgency_score = self.scoring_engine.calculate_urgency_score(
            ml_prediction, pandas_metrics
        )
        urgency_level = self.scoring_engine.get_urgency_level(urgency_score)
        action_priorities = self.scoring_engine.calculate_action_priority(
            pandas_metrics, urgency_level
        )
        
        # ===== TAHAP 4: RAG Runbook Retrieval =====
        logger.info("[4/7] 📚 Searching RAG runbooks...")
        rag_content = ""
        rag_similarity = 0.0
        
        if RAG_AVAILABLE:
            try:
                query = (
                    f"{ml_prediction.anomaly_type.value} "
                    f"CPU {pandas_metrics.cpu_usage_avg_5m:.0f}% "
                    f"memory growth {pandas_metrics.memory_growth_rate_mb_per_min:+.1f} MB/min "
                    f"error rate {pandas_metrics.error_rate_percent:.1f}% "
                    f"latency {pandas_metrics.latency_p99_ms:.0f}ms "
                    f"restarts {pandas_metrics.pod_restarts_1h}"
                )
                rag_results = query_runbooks(query, top_k=3)
                if rag_results:
                    rag_content = "\n---\n".join(
                        [r["document"] for r in rag_results]
                    )
                    rag_similarity = rag_results[0]["similarity"]
                    logger.info(
                        f"RAG found {len(rag_results)} relevant docs "
                        f"(top similarity: {rag_similarity:.2f})"
                    )
            except Exception as e:
                logger.warning(f"RAG query failed: {e}")
        
        # ===== TAHAP 5: Experience Memory Lookup =====
        logger.info("[5/7] 🧪 Querying past experiences...")
        experience_texts = []
        try:
            situation_desc = self.pandas_processor.generate_situation_summary(
                pandas_metrics
            )
            past_experiences = self.experience_memory.query_similar_experience(
                situation_desc, top_k=3, success_only=True
            )
            experience_texts = self.experience_memory.format_experiences_for_prompt(
                past_experiences
            )
        except Exception as e:
            logger.warning(f"Experience memory query failed: {e}")
        
        # ===== Bangun Situation Report =====
        situation = SituationReport(
            ml_prediction=ml_prediction,
            pandas_metrics=pandas_metrics,
            urgency_score=urgency_score,
            urgency_level=urgency_level,
            rag_runbook_content=rag_content,
            rag_similarity_score=rag_similarity,
            past_experiences=experience_texts
        )
        
        # ===== TAHAP 6: AI Agent Decision =====
        logger.info("[6/7] 🤖 AI Agent making decision...")
        
        # Cek guardrails sebelum keputusan
        guardrail_block = self._check_guardrails(deployment_name)
        if guardrail_block:
            logger.warning(f"Guardrails blocked: {guardrail_block}")
            decision = ActionDecision(
                action=ActionType.NO_OP,
                target_deployment=deployment_name,
                confidence=1.0,
                reasoning=f"Guardrails blocked: {guardrail_block}",
                data_sources_used=["guardrails"]
            )
        else:
            # Cek apakah multi-step diperlukan
            if self.multi_step_planner.should_use_multi_step(situation):
                logger.info("📋 Multi-step plan needed!")
                plan = self.multi_step_planner.create_plan(
                    situation, deployment_name
                )
                # Untuk sekarang, ambil langkah pertama sebagai decision
                first_step = plan.steps[0]
                decision = ActionDecision(
                    action=first_step.action,
                    target_deployment=first_step.target_deployment,
                    parameters=first_step.parameters,
                    confidence=plan.confidence,
                    reasoning=f"[Multi-Step Plan {plan.plan_id}] Step 1/{plan.total_steps}: {first_step.reason}",
                    data_sources_used=["multi_step_planner", "scoring_engine"]
                )
            else:
                # Single-step decision via AI Agent
                decision = self.decision_agent.select_action(
                    situation, action_priorities
                )
            
            # Precision optimizer: Adjust scale parameters
            if decision.action in [ActionType.SCALE_OUT, ActionType.SCALE_IN]:
                optimal_delta = self.precision_optimizer.calculate_replicas_delta(
                    pandas_metrics.current_replicas, pandas_metrics
                )
                if decision.action == ActionType.SCALE_OUT and optimal_delta > 0:
                    decision.parameters.replicas_to_add = min(optimal_delta, 5)
                elif decision.action == ActionType.SCALE_IN and optimal_delta < 0:
                    decision.parameters.replicas_to_remove = min(abs(optimal_delta), 3)
            
            # Sandbox test untuk masalah baru
            if self.sandbox.should_use_sandbox(rag_similarity, ml_prediction.anomaly_type.value):
                logger.info("🧪 Novel problem detected, testing in sandbox...")
                sandbox_result = self.sandbox.test_action(decision)
                if not sandbox_result["approved"]:
                    logger.warning(
                        f"Sandbox REJECTED action: {sandbox_result['details']}"
                    )
                    decision = ActionDecision(
                        action=ActionType.ESCALATE,
                        target_deployment=deployment_name,
                        confidence=0.60,
                        reasoning=(
                            f"Sandbox test failed: {sandbox_result['details']}. "
                            f"Escalating to human operator."
                        ),
                        data_sources_used=["sandbox_test"]
                    )
        
        # Confidence gate: Paksa eskalasi jika confidence rendah
        confidence_threshold = self.guardrail_config.get("confidence_threshold", 0.85)
        if (decision.confidence < confidence_threshold and
            decision.action != ActionType.NO_OP and
            decision.action != ActionType.ESCALATE):
            logger.warning(
                f"Confidence {decision.confidence:.2f} < threshold {confidence_threshold}. "
                f"Overriding to ESCALATE."
            )
            decision = ActionDecision(
                action=ActionType.ESCALATE,
                target_deployment=deployment_name,
                confidence=decision.confidence,
                reasoning=(
                    f"Original action: {decision.action.value} (confidence: {decision.confidence:.2f}). "
                    f"Overridden to escalate because confidence < {confidence_threshold}. "
                    f"Original reasoning: {decision.reasoning}"
                ),
                data_sources_used=decision.data_sources_used + ["confidence_gate"]
            )
        
        # ===== TAHAP 7: Generate Explanation & Log =====
        logger.info("[7/7] 📝 Generating explanation...")
        explanation = self.decision_agent.explain_decision(decision, situation)
        logger.info(explanation)
        
        # Record action in history
        self._record_action_history(deployment_name, decision)
        
        # Hitung waktu total
        elapsed = time.time() - start_time
        
        result = {
            "decision": decision.model_dump(),
            "explanation": explanation,
            "situation": {
                "urgency_score": urgency_score,
                "urgency_level": urgency_level.value,
                "anomaly_type": ml_prediction.anomaly_type.value,
                "rag_similarity": rag_similarity,
            },
            "elapsed_seconds": round(elapsed, 2),
            "timestamp": datetime.utcnow().isoformat()
        }
        
        logger.success(
            f"\n✅ Pipeline complete in {elapsed:.1f}s | "
            f"Action: {decision.action.value} | "
            f"Confidence: {decision.confidence:.0%}"
        )
        
        return result
    
    def record_post_action_result(self, decision: ActionDecision,
                                    metrics_before: PandasMetrics,
                                    metrics_after: PandasMetrics,
                                    ml_prediction: MLPrediction,
                                    urgency_score: float):
        """
        Catat hasil setelah aksi dieksekusi.
        
        Dipanggil oleh Self-Healing Engine (Layer 5) setelah aksi
        selesai dieksekusi dan metrik post-action sudah dikumpulkan.
        """
        # Hitung post-action score
        score = self.scoring_engine.calculate_post_action_score(
            metrics_before, metrics_after, decision.action
        )
        
        success = score >= 50  # ≥ 50/100 dianggap berhasil
        
        result = PostActionResult(
            action_taken=decision.action,
            target_deployment=decision.target_deployment,
            success=success,
            score=score,
            metrics_before=metrics_before.model_dump(),
            metrics_after=metrics_after.model_dump(),
            improvement_summary=(
                f"Score: {score:.0f}/100. "
                f"Error: {metrics_before.error_rate_percent:.1f}% → {metrics_after.error_rate_percent:.1f}%. "
                f"Latency: {metrics_before.latency_p99_ms:.0f}ms → {metrics_after.latency_p99_ms:.0f}ms. "
                f"Memory growth: {metrics_before.memory_growth_rate_mb_per_min:+.1f} → "
                f"{metrics_after.memory_growth_rate_mb_per_min:+.1f} MB/min."
            ),
            duration_seconds=30  # Default
        )
        
        # Simpan ke Experience Memory
        self.experience_memory.record_action(
            decision=decision,
            result=result,
            metrics=metrics_before,
            ml_prediction=ml_prediction,
            urgency_score=urgency_score
        )
        
        # Update circuit breaker
        if success:
            self.consecutive_failures[decision.target_deployment] = 0
        else:
            self.consecutive_failures[decision.target_deployment] = (
                self.consecutive_failures.get(decision.target_deployment, 0) + 1
            )
        
        return result
    
    def _check_guardrails(self, deployment_name: str) -> Optional[str]:
        """
        Cek guardrails sebelum mengambil keputusan.
        
        Returns:
            None jika boleh lanjut, atau string alasan diblokir
        """
        # Cooldown check
        cooldown_minutes = self.guardrail_config.get("cooldown_minutes", 15)
        max_actions = self.guardrail_config.get("max_actions_per_pod", 2)
        
        recent_actions = [
            a for a in self.action_history
            if (a["deployment"] == deployment_name and
                a["action"] != ActionType.NO_OP.value and
                a["action"] != ActionType.ESCALATE.value and
                (datetime.utcnow() - a["timestamp"]) < timedelta(minutes=cooldown_minutes))
        ]
        
        if len(recent_actions) >= max_actions:
            return (
                f"Cooldown active: {len(recent_actions)} actions taken on "
                f"{deployment_name} in last {cooldown_minutes} minutes "
                f"(max: {max_actions})"
            )
        
        # Circuit breaker check
        cb_threshold = self.guardrail_config.get("circuit_breaker_failures", 3)
        failures = self.consecutive_failures.get(deployment_name, 0)
        if failures >= cb_threshold:
            return (
                f"Circuit breaker OPEN: {failures} consecutive failures on "
                f"{deployment_name} (threshold: {cb_threshold}). "
                f"Manual intervention required."
            )
        
        return None
    
    def _record_action_history(self, deployment_name: str,
                                 decision: ActionDecision):
        """Catat aksi ke history untuk guardrails tracking."""
        self.action_history.append({
            "deployment": deployment_name,
            "action": decision.action.value,
            "confidence": decision.confidence,
            "timestamp": datetime.utcnow(),
        })
        
        # Bersihkan history lama (> 1 jam)
        cutoff = datetime.utcnow() - timedelta(hours=1)
        self.action_history = [
            a for a in self.action_history
            if a["timestamp"] > cutoff
        ]


# ============================================================
# Entry Point: CLI untuk testing
# ============================================================
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="XFSCI Orchestrator CLI")
    parser.add_argument(
        "--deployment", "-d",
        required=True,
        help="Nama deployment yang akan dianalisis (misal: cartservice)"
    )
    parser.add_argument(
        "--node", "-n",
        default="unknown",
        help="Nama node tempat pod berjalan"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hanya analisis, jangan eksekusi aksi"
    )
    
    args = parser.parse_args()
    
    logger.info("Starting XFSCI Orchestrator in CLI mode...")
    
    orchestrator = XFSCIOrchestrator()
    result = orchestrator.handle_alert(
        deployment_name=args.deployment,
        node_name=args.node
    )
    
    logger.info(f"\n{'='*60}")
    logger.info("FINAL RESULT:")
    logger.info(f"Action: {result['decision']['action']}")
    logger.info(f"Target: {result['decision']['target_deployment']}")
    logger.info(f"Confidence: {result['decision']['confidence']:.0%}")
    logger.info(f"Reasoning: {result['decision']['reasoning']}")
    logger.info(f"Time: {result['elapsed_seconds']}s")
    logger.info(f"{'='*60}")
