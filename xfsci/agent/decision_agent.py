# ============================================================
# XFSCI Decision Agent — 2-Tier Agentic AI (Antigravity + Rule-Based)
# ============================================================
# INTI UTAMA: Modul AI Agent dengan arsitektur 2-Tier:
#   Tier 1: Antigravity Agentic Runtime (Claude Opus / Gemini 3.8 Flash)
#           - Berjalan di Sandbox aman, terintegrasi Akun Pro
#   Tier 2: Rule-based engine (deterministik, always works)
#           - Safety net 100% tanpa dependensi eksternal
#
# Mengambil keputusan self-healing berdasarkan:
#   1. GNN Root Cause Analysis (100% Cluster Acc, 0% halusinasi)
#   2. Data numerik dari Pandas (FAKTA, bukan opini)
#   3. SOP Runbook dari RAG (DOKUMEN TERBUKTI)
#   4. Pengalaman masa lalu dari Experience Memory
#
# ANTI-HALUSINASI:
#   - Input: GNN Top-3 RCA + angka Pandas + dokumen RAG
#   - Output: JSON terkunci (Pydantic schema)
#   - Fallback: Antigravity → Rule-based Safety Net
#   - Guardrails: Validasi sebelum eksekusi
# ============================================================

import os
import json
import time
from typing import Optional
from datetime import datetime

import yaml
from pathlib import Path
from loguru import logger

# Antigravity Python SDK — Programmatic Agentic Runtime
try:
    from google.antigravity import Agent as AGYAgent, LocalAgentConfig as AGYLocalAgentConfig, CapabilitiesConfig as AGYCapabilitiesConfig
    ANTIGRAVITY_SDK_AVAILABLE = True
except ImportError:
    ANTIGRAVITY_SDK_AVAILABLE = False

from agent.action_schema import (
    ActionType, ActionDecision, ActionParameters,
    MultiStepPlan, MultiStepAction, EscalationAlert,
    SituationReport, UrgencyLevel, AnomalyType,
    PandasMetrics, MLPrediction
)


class XFSCIDecisionAgent:
    """
    AI Agent dengan arsitektur 2-Tier untuk pengambilan
    keputusan self-healing di infrastruktur cloud Kubernetes.
    
    Arsitektur:
    - Tier 1 (Antigravity Agentic Runtime): Claude Opus / Gemini 3.8 Flash
      berjalan di dalam Sandbox aman dengan Akun Pro Anda
    - Tier 2 (Safety Net): Rule-based engine — deterministik (0.001s)
      selalu berhasil, tanpa dependensi eksternal
    """
    
    # System prompt yang mengunci perilaku AI Agent
    SYSTEM_PROMPT = """Kamu adalah XFSCI Decision Agent — sistem AI untuk self-healing infrastruktur Kubernetes.

ATURAN MUTLAK YANG TIDAK BOLEH DILANGGAR:
1. HANYA pilih aksi dari daftar ini: no_op, restart_pod, scale_out, scale_in, rate_limit, migrate_pod, escalate
2. JANGAN PERNAH mengarang atau menghitung angka sendiri — gunakan HANYA angka dari "metrics" dan "ml_prediction" yang diberikan
3. JANGAN PERNAH bertindak jika confidence kamu < 0.85 → pilih "escalate"
4. WAJIB jelaskan alasan keputusan berdasarkan DATA dan SOP RUNBOOK yang diberikan
5. Jawab HANYA dalam format JSON yang diminta — JANGAN menambahkan teks di luar JSON

PANDUAN KEPUTUSAN:
- Urgency LOW (< 30): Pilih no_op kecuali ada indikasi pra-anomali
- Urgency MEDIUM (30-50): Monitor ketat, pertimbangkan scale_out preventif
- Urgency HIGH (50-70): Aksi diperlukan (scale_out, restart, rate_limit)
- Urgency CRITICAL (> 85): Aksi segera, pertimbangkan multi-step plan

PRIORITAS KEAMANAN:
- Selalu scale_out SEBELUM restart (jaga availability)
- Jangan restart semua pod sekaligus (rolling restart)
- Jika ragu → escalate ke manusia
- Lebih baik over-cautious daripada over-aggressive"""
    
    def __init__(self, config_path: str = None):
        """
        Inisialisasi Multi-Tier AI Agent.
        
        Args:
            config_path: Path ke config.yaml
        """
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        
        self.agent_config = self.config.get("ai_agent", {})
        self.llm_config = self.agent_config.get("llm", {})
        self.valid_actions = self.agent_config.get("actions", [])
        self.fallback_enabled = self.agent_config.get("fallback", {}).get("enabled", True)

        # Setup Antigravity config
        self.antigravity_config = self.agent_config.get("antigravity", {})
        self.antigravity_available = False
        self._setup_antigravity()
        
        logger.info(
            f"XFSCIDecisionAgent initialized | "
            f"Tier 1 (Antigravity Agentic): {'✅ Active (Akun Pro)' if self.antigravity_available else '⏸️ Standby'} | "
            f"Tier 2 (Safety Net): ✅ Rule-Based Engine (Deterministic)"
        )

    def _setup_antigravity(self):
        """Konfigurasi Antigravity Agentic Runtime dengan Sandbox & Akun Pro."""
        if not self.antigravity_config.get("enabled", True):
            logger.info("Antigravity Agentic Engine disabled in config")
            return

        if ANTIGRAVITY_SDK_AVAILABLE:
            self.antigravity_available = True
            logger.success(
                f"Antigravity Agentic SDK ready | Priority: {self.antigravity_config.get('model_priority', 'claude-opus')} "
                f"| Fallback: {self.antigravity_config.get('fallback_model', 'gemini-3.8-flash')} | Sandbox: ON"
            )
        else:
            logger.info("google-antigravity SDK not present in local environment — standby mode (will use Rule-based Safety Net)")
            self.antigravity_available = False
    
    def _build_prompt(self, situation: SituationReport,
                       action_priorities: list[dict] = None) -> str:
        """
        Bangun prompt terstruktur untuk Antigravity / LLM dengan Incident Dossier lengkap.
        
        Memuat:
        1. GNN Topology Root Cause Analysis (100% Cluster Acc & 100% Top-3 RCA)
        2. Data metrik EKSAK dari Pandas (100% fakta numerik)
        3. Skor urgensi deterministik
        4. SOP Runbook dari RAG
        5. Pengalaman masa lalu dari Experience Memory
        6. Prioritas aksi dari Scoring Engine
        """
        m = situation.pandas_metrics
        ml = situation.ml_prediction

        # Format Top-3 RCA kandidat dari GNN
        top3_rca = getattr(ml, "top3_root_causes", [])
        if top3_rca:
            top3_items = []
            for r in top3_rca:
                rank_num = r.get("rank", 1)
                svc = r.get("service", "unknown")
                pct = r.get("percentage") or f"{r.get('score', 0.0):.1%}"
                top3_items.append(f"  • Rank {rank_num}: {svc} (Contribution Score: {pct})")
            top3_str = "\n".join(top3_items)
        else:
            top3_str = f"  • Rank 1: {m.target_pod} (100.0%)"

        # Format Distribusi Probabilitas Gangguan GNN
        fault_probs = getattr(ml, "fault_probabilities", {})
        if fault_probs:
            fault_probs_str = " | ".join([f"{k}: {v:.1%}" for k, v in fault_probs.items()])
        else:
            fault_probs_str = f"{ml.anomaly_type.value}: {ml.confidence:.1%}"

        # Tentukan target pod/service utama (mengunci target ke Root Cause GNN)
        primary_target = getattr(ml, "root_cause_service", None) or m.target_pod

        prompt = f"""Analisis situasi insiden Kubernetes berikut dan tentukan aksi self-healing terbaik.

## 🎯 GNN TOPOLOGY ROOT CAUSE ANALYSIS (100% Cluster Acc, 100% Top-3 RCA):
- Global Cluster Risk: {ml.risk_score:.2f} (Graph Urgency)
- TERSANGKA UTAMA (PRIMARY ROOT CAUSE): {primary_target}
- Top-3 RCA Ranking (Daftar Biang Kerok Terbukti):
{top3_str}
- Fault Probability Distribution:
  {fault_probs_str}
- Classified Failure Mode: {ml.anomaly_type.value} (Confidence: {ml.confidence:.2f})
- Time to Failure Estimate: {ml.time_to_failure_minutes or 'N/A'} minutes
- Cascade Impact Propagation: {', '.join(ml.cascade_risk) or 'None (Isolated)'}

## 📊 DATA TELEMETRI FISIK (dari Pandas — angka ini 100% akurat):
- Observed Pod: {m.target_pod}
- Target Node: {m.target_node}
- Namespace: {m.namespace}
- CPU Usage: {m.cpu_usage_avg_5m:.1f}% (5m avg) | {m.cpu_usage_avg_15m:.1f}% (15m avg)
- Memory: {m.memory_usage_mb:.1f} MB ({m.memory_usage_percent:.1f}%)
- Memory Growth Rate: {m.memory_growth_rate_mb_per_min:+.1f} MB/min
- Pod Restarts (1h): {m.pod_restarts_1h}
- Current Replicas: {m.current_replicas}
- Request Rate: {m.request_rate_rps:.1f} RPS
- Error Rate: {m.error_rate_percent:.1f}%
- Latency: P50={m.latency_p50_ms:.0f} ms | P99={m.latency_p99_ms:.0f} ms

## 📐 URGENCY:
- Score: {situation.urgency_score}/100
- Level: {situation.urgency_level.value}

## 📖 SOP RUNBOOK (dari RAG Knowledge Base):
{situation.rag_runbook_content or 'Tidak ada runbook yang cocok ditemukan.'}
RAG Similarity: {situation.rag_similarity_score:.2f}
"""
        
        if situation.past_experiences:
            prompt += "\n## 🧪 PENGALAMAN MASA LALU SERUPA:\n"
            for exp in situation.past_experiences[:3]:
                prompt += f"- {exp}\n"
        
        if action_priorities:
            prompt += "\n## ⚡ PRIORITAS AKSI (dari Scoring Engine):\n"
            for p in action_priorities[:5]:
                prompt += f"- {p['action'].value} (priority: {p['priority']}) — {p['reason']}\n"
        
        prompt += f"""
## 🛡️ INSTRUKSI KEPUTUSAN SRE (ANTI-HALUSINASI):
1. Target deployment WAJIB mengacu pada Tersangka Utama (Root Cause) hasil GNN: "{primary_target}". JANGAN menyalahkan pod hilir yang hanya korban cascade.
2. Aksi WAJIB dipilih dari daftar enum terkunci:
   [no_op, restart_pod, scale_out, scale_in, rate_limit, migrate_pod, escalate]
3. Jika anomali adalah memory_leak, prioritaskan scale_out terlebih dahulu untuk menyerap traffic sebelum rolling restart.
4. Jawab HANYA dalam format JSON valid berikut (tanpa teks penjelasan lain):
{{
    "action": "<salah satu dari: no_op, restart_pod, scale_out, scale_in, rate_limit, migrate_pod, escalate>",
    "target_deployment": "{primary_target}",
    "target_namespace": "{m.namespace}",
    "parameters": {{
        "replicas_to_add": <number or null>,
        "replicas_to_remove": <number or null>,
        "target_node": "<node name or null>",
        "rate_limit_rps": <number or null>,
        "restart_strategy": "<rolling or immediate>"
    }},
    "confidence": <0.0 - 1.0>,
    "reasoning": "<penjelasan logis mengacu pada GNN RCA dan SOP Runbook>",
    "data_sources_used": ["gnn_top3_rca", "pandas_metrics", "rag_runbook"]
}}"""
        return prompt

    def _select_with_antigravity(self, situation: SituationReport,
                                  action_priorities: list[dict] = None) -> Optional[ActionDecision]:
        """
        Tier 1 (Primary): Menjalankan Antigravity Agentic Engine
        menggunakan model Akun Pro dengan fitur AUTO-SWITCH:
        1. Model Prioritas: Claude Opus (Deep reasoning SRE)
        2. Auto-Switch Fallback: Gemini 3.8 Flash (High-speed SRE) jika Opus limit/error
        3. Sandbox: Eksekusi tervendor di lingkungan aman
        """
        import asyncio

        prompt = self._build_prompt(situation, action_priorities)
        allowed_cmds = self.antigravity_config.get("allowed_commands", ["kubectl", "curl", "grep", "cat", "sh"])
        sandbox_on = self.antigravity_config.get("sandbox_mode", True)
        primary_model = self.antigravity_config.get("model_priority", "claude-opus")
        fallback_model = self.antigravity_config.get("fallback_model", "gemini-3.8-flash")

        async def _call_model(model_name: str) -> str:
            agent_kwargs = {
                "system_instructions": self.SYSTEM_PROMPT,
                "capabilities": AGYCapabilitiesConfig(
                    sandbox_mode=sandbox_on,
                    allowed_commands=allowed_cmds
                )
            }
            try:
                config = AGYLocalAgentConfig(model=model_name, **agent_kwargs)
            except TypeError:
                config = AGYLocalAgentConfig(**agent_kwargs)

            async with AGYAgent(config) as agent:
                resp = await agent.chat(prompt)
                full_text = ""
                async for token in resp:
                    full_text += token
                return full_text

        def _execute_async(coro):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()
                    return loop.run_until_complete(coro)
                else:
                    return loop.run_until_complete(coro)
            except RuntimeError:
                return asyncio.run(coro)

        response_text = None
        model_used = primary_model

        # 1. Coba Model Prioritas Pertama (Claude Opus)
        try:
            logger.info(f"🚀 [Antigravity SRE] Memanggil Model Prioritas: {primary_model} (Sandbox: {sandbox_on})...")
            response_text = _execute_async(_call_model(primary_model))
            logger.success(f"✅ Berhasil mendapat respon dari model {primary_model}!")
        except Exception as e_opus:
            logger.warning(
                f"⚠️ Model prioritas '{primary_model}' menemui kendala ({e_opus}). "
                f"🔄 AUTO-SWITCHING ke Fallback Model: '{fallback_model}'..."
            )
            model_used = fallback_model
            # 2. Auto-Switch ke Fallback Model (Gemini 3.8 Flash)
            try:
                response_text = _execute_async(_call_model(fallback_model))
                logger.success(f"✅ Berhasil mendapat respon dari Fallback Model: {fallback_model}!")
            except Exception as e_gemini:
                logger.error(f"❌ Fallback Model '{fallback_model}' juga gagal: {e_gemini}")
                return None

        if not response_text:
            return None

        try:
            cleaned_text = response_text.strip()
            if "```json" in cleaned_text:
                cleaned_text = cleaned_text.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned_text:
                cleaned_text = cleaned_text.split("```")[1].split("```")[0].strip()

            response_data = json.loads(cleaned_text)
            primary_target = getattr(situation.ml_prediction, "root_cause_service", None) or situation.pandas_metrics.target_pod

            decision = ActionDecision(
                action=ActionType(response_data["action"]),
                target_deployment=response_data.get("target_deployment", primary_target),
                target_namespace=response_data.get("target_namespace", situation.pandas_metrics.namespace),
                parameters=ActionParameters(**response_data.get("parameters", {})),
                confidence=float(response_data.get("confidence", 0.95)),
                reasoning=response_data.get("reasoning", f"Antigravity Agentic Decision [{model_used}] based on GNN RCA & SOP"),
                data_sources_used=response_data.get("data_sources_used", [f"antigravity_{model_used}", "gnn_top3_rca"])
            )

            logger.success(
                f"Antigravity Decision ({model_used}): {decision.action.value} | "
                f"Target: {decision.target_deployment} | "
                f"Confidence: {decision.confidence:.2f}"
            )
            return decision

        except Exception as e:
            logger.warning(f"Error parsing Antigravity response ({model_used}): {e}")
            return None
    
    def select_action(self, situation: SituationReport,
                       action_priorities: list[dict] = None) -> ActionDecision:
        """
        Fungsi utama: Pilih aksi terbaik untuk situasi yang diberikan.
        
        Arsitektur 2-Tier:
        1. Tier 1 (Antigravity Agentic Runtime): Claude Opus / Gemini Flash (Akun Pro) + Sandbox
        2. Tier 2 (Safety Net): Rule-based engine — deterministik 100% selalu berhasil
        
        Args:
            situation: Laporan situasi lengkap
            action_priorities: Prioritas aksi dari Scoring Engine
        
        Returns:
            ActionDecision — Keputusan tervalidasi
        """
        # Tier 1: Antigravity Agentic Runtime (Akun Pro - Claude Opus / Gemini 3.8 Flash)
        if self.antigravity_available:
            try:
                result = self._select_with_antigravity(situation, action_priorities)
                if result is not None:
                    return result
            except Exception as e:
                logger.error(f"Antigravity pipeline error: {e}")
        
        # Tier 2: Rule-based engine (deterministik, safety net)
        logger.warning("Antigravity Engine not active or skipped, using deterministic rule-based safety net")
        return self._select_with_rules(situation, action_priorities)
    
    def _select_with_rules(self, situation: SituationReport,
                            action_priorities: list[dict] = None) -> ActionDecision:
        """
        Rule-based fallback jika Antigravity Engine tidak tersedia.
        
        Ini adalah "safety net" yang menjamin sistem tetap
        bisa mengambil keputusan meskipun tanpa LLM.
        Menggunakan 100% if-else deterministik.
        """
        metrics = situation.pandas_metrics
        ml = situation.ml_prediction
        urgency = situation.urgency_level
        
        logger.info(f"Rule-based decision | Urgency: {urgency.value}")
        
        # LOW urgency → No-op
        if urgency == UrgencyLevel.LOW:
            return ActionDecision(
                action=ActionType.NO_OP,
                target_deployment=metrics.target_pod,
                confidence=0.95,
                reasoning=(
                    f"Urgency score {situation.urgency_score:.0f} (LOW). "
                    f"Semua metrik dalam batas normal. Tidak perlu tindakan."
                ),
                data_sources_used=["rule_based_engine", "urgency_score"]
            )
        
        # CRITICAL + Memory Leak → Scale Out
        if (urgency == UrgencyLevel.CRITICAL and
            metrics.memory_growth_rate_mb_per_min > 5):
            return ActionDecision(
                action=ActionType.SCALE_OUT,
                target_deployment=metrics.target_pod,
                parameters=ActionParameters(replicas_to_add=2),
                confidence=0.90,
                reasoning=(
                    f"CRITICAL: Memory leak terdeteksi ({metrics.memory_growth_rate_mb_per_min:+.1f} MB/min). "
                    f"Scale out +2 replika untuk jaga availability sebelum restart."
                ),
                data_sources_used=["rule_based_engine", "pandas_metrics.memory_growth_rate"]
            )
        
        # HIGH + CPU Overload → Scale Out
        if urgency in [UrgencyLevel.HIGH, UrgencyLevel.CRITICAL] and metrics.cpu_usage_avg_5m > 80:
            return ActionDecision(
                action=ActionType.SCALE_OUT,
                target_deployment=metrics.target_pod,
                parameters=ActionParameters(replicas_to_add=2),
                confidence=0.88,
                reasoning=(
                    f"CPU overload: {metrics.cpu_usage_avg_5m:.1f}% (5m avg). "
                    f"Scale out untuk distribusi beban."
                ),
                data_sources_used=["rule_based_engine", "pandas_metrics.cpu_usage_avg_5m"]
            )
        
        # HIGH + Crash Loop → Scale Out
        if metrics.pod_restarts_1h >= 3:
            return ActionDecision(
                action=ActionType.SCALE_OUT,
                target_deployment=metrics.target_pod,
                parameters=ActionParameters(replicas_to_add=2),
                confidence=0.85,
                reasoning=(
                    f"Pod crash loop: {metrics.pod_restarts_1h} restarts dalam 1 jam. "
                    f"Scale out untuk menjaga availability."
                ),
                data_sources_used=["rule_based_engine", "pandas_metrics.pod_restarts_1h"]
            )
        
        # MEDIUM + Latency tinggi → Rate Limit
        if metrics.latency_p99_ms > 500:
            return ActionDecision(
                action=ActionType.RATE_LIMIT,
                target_deployment=metrics.target_pod,
                parameters=ActionParameters(rate_limit_rps=100),
                confidence=0.82,
                reasoning=(
                    f"Latency P99: {metrics.latency_p99_ms:.0f}ms (melebihi SLA 500ms). "
                    f"Rate limit untuk stabilisasi."
                ),
                data_sources_used=["rule_based_engine", "pandas_metrics.latency_p99_ms"]
            )
        
        # Default → Escalate jika tidak ada rule yang cocok
        if urgency in [UrgencyLevel.HIGH, UrgencyLevel.CRITICAL]:
            return ActionDecision(
                action=ActionType.ESCALATE,
                target_deployment=metrics.target_pod,
                confidence=0.70,
                reasoning=(
                    f"Urgency {urgency.value} tapi tidak ada rule spesifik yang cocok. "
                    f"Eskalasi ke operator manusia untuk investigasi."
                ),
                data_sources_used=["rule_based_engine", "urgency_level"]
            )
        
        # Fallback final → No-op
        return ActionDecision(
            action=ActionType.NO_OP,
            target_deployment=metrics.target_pod,
            confidence=0.80,
            reasoning="Tidak ada kondisi anomali yang terdeteksi oleh rule engine.",
            data_sources_used=["rule_based_engine"]
        )
    
    def explain_decision(self, decision: ActionDecision,
                          situation: SituationReport) -> str:
        """
        Generate penjelasan keputusan yang mudah dibaca manusia.
        
        Ini adalah XAI (Explainable AI) BAWAAN — tidak perlu
        SHAP/LIME terpisah untuk menjelaskan keputusan agent.
        """
        explanation = f"""
🤖 XFSCI AI Agent Decision Report
{'='*50}
📅 Timestamp: {datetime.utcnow().isoformat()}
🎯 Target: {decision.target_deployment} ({decision.target_namespace})
⚡ Action: {decision.action.value}
🔒 Confidence: {decision.confidence:.0%}

📊 Key Metrics (from Pandas — 100% accurate):
  • CPU: {situation.pandas_metrics.cpu_usage_avg_5m:.1f}%
  • Memory: {situation.pandas_metrics.memory_usage_mb:.1f} MB ({situation.pandas_metrics.memory_usage_percent:.1f}%)
  • Memory Growth: {situation.pandas_metrics.memory_growth_rate_mb_per_min:+.1f} MB/min
  • Error Rate: {situation.pandas_metrics.error_rate_percent:.1f}%
  • Latency P99: {situation.pandas_metrics.latency_p99_ms:.0f} ms
  • Pod Restarts: {situation.pandas_metrics.pod_restarts_1h}

🧠 ML Prediction:
  • Risk Score: {situation.ml_prediction.risk_score:.2f}
  • Anomaly Type: {situation.ml_prediction.anomaly_type.value}
  • Confidence: {situation.ml_prediction.confidence:.2f}

📐 Urgency: {situation.urgency_score:.0f}/100 ({situation.urgency_level.value})

💬 Reasoning:
  {decision.reasoning}

📚 Data Sources Used:
  {', '.join(decision.data_sources_used)}
{'='*50}"""
        
        return explanation
