# ============================================================
# XFSCI Decision Agent — Gemini Flash AI Agent
# ============================================================
# INTI UTAMA: Modul AI Agent yang menggunakan Google Gemini Flash
# untuk mengambil keputusan self-healing berdasarkan:
#   1. Data numerik dari Pandas (FAKTA, bukan opini)
#   2. SOP Runbook dari RAG (DOKUMEN TERBUKTI)
#   3. Pengalaman masa lalu dari Experience Memory
#
# ANTI-HALUSINASI:
#   - Input: Hanya angka dari Pandas + dokumen dari RAG
#   - Output: JSON terkunci (Pydantic schema)
#   - Fallback: Rule-based jika API tidak tersedia
#   - Guardrails: Validasi sebelum eksekusi
# ============================================================

import os
import json
from typing import Optional
from datetime import datetime

import yaml
from pathlib import Path
from loguru import logger

import google.generativeai as genai

from agent.action_schema import (
    ActionType, ActionDecision, ActionParameters,
    MultiStepPlan, MultiStepAction, EscalationAlert,
    SituationReport, UrgencyLevel, AnomalyType,
    PandasMetrics, MLPrediction
)


class XFSCIDecisionAgent:
    """
    AI Agent berbasis Gemini Flash untuk pengambilan keputusan
    self-healing di infrastruktur cloud Kubernetes.
    
    Prinsip desain:
    1. Gemini HANYA melihat data yang sudah disiapkan (tidak query sendiri)
    2. Gemini HANYA boleh memilih aksi dari enum terkunci
    3. Gemini WAJIB menjelaskan alasannya berdasarkan data & SOP
    4. Jika Gemini API gagal → Fallback ke rule-based engine
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
        Inisialisasi Gemini Flash AI Agent.
        
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
        
        # Setup Gemini
        self._setup_gemini()
        
        logger.info(f"XFSCIDecisionAgent initialized | Model: {self.llm_config.get('model')}")
    
    def _setup_gemini(self):
        """Konfigurasi Gemini Flash API."""
        api_key_env = self.llm_config.get("api_key_env", "GEMINI_API_KEY")
        api_key = os.environ.get(api_key_env, "")
        
        if not api_key:
            logger.warning(
                f"⚠️ {api_key_env} not set! AI Agent will use rule-based fallback. "
                f"Set it with: export {api_key_env}=your_api_key"
            )
            self.gemini_available = False
            return
        
        try:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(
                model_name=self.llm_config.get("model", "gemini-3.6-flash"),
                system_instruction=self.SYSTEM_PROMPT,
            )
            self.generation_config = genai.GenerationConfig(
                temperature=self.llm_config.get("temperature", 0.1),
                max_output_tokens=self.llm_config.get("max_tokens", 1024),
                response_mime_type="application/json",
            )
            self.gemini_available = True
            logger.success("Gemini Flash API configured successfully")
        except Exception as e:
            logger.error(f"Failed to setup Gemini: {e}")
            self.gemini_available = False
    
    def _build_prompt(self, situation: SituationReport,
                       action_priorities: list[dict] = None) -> str:
        """
        Bangun prompt terstruktur untuk Gemini.
        
        Prompt ini berisi:
        1. Data metrik EKSAK dari Pandas (bukan estimasi)
        2. Prediksi ML (risk score, anomaly type)
        3. Skor urgensi deterministik
        4. SOP Runbook dari RAG
        5. Pengalaman masa lalu (jika ada)
        6. Prioritas aksi dari Scoring Engine
        """
        prompt = f"""Analisis situasi berikut dan tentukan aksi terbaik.

## DATA SITUASI SAAT INI (dari Pandas — angka ini 100% akurat):
- Pod: {situation.pandas_metrics.target_pod}
- Node: {situation.pandas_metrics.target_node}
- Namespace: {situation.pandas_metrics.namespace}
- CPU (5m avg): {situation.pandas_metrics.cpu_usage_avg_5m}%
- CPU (15m avg): {situation.pandas_metrics.cpu_usage_avg_15m}%
- Memory: {situation.pandas_metrics.memory_usage_mb:.1f} MB ({situation.pandas_metrics.memory_usage_percent:.1f}%)
- Memory Growth: {situation.pandas_metrics.memory_growth_rate_mb_per_min:+.1f} MB/min
- Pod Restarts (1h): {situation.pandas_metrics.pod_restarts_1h}
- Current Replicas: {situation.pandas_metrics.current_replicas}
- Request Rate: {situation.pandas_metrics.request_rate_rps:.1f} RPS
- Error Rate: {situation.pandas_metrics.error_rate_percent:.1f}%
- Latency P50: {situation.pandas_metrics.latency_p50_ms:.0f} ms
- Latency P99: {situation.pandas_metrics.latency_p99_ms:.0f} ms

## PREDIKSI ML:
- Risk Score: {situation.ml_prediction.risk_score:.2f}
- Anomaly Type: {situation.ml_prediction.anomaly_type.value}
- ML Confidence: {situation.ml_prediction.confidence:.2f}
- Time to Failure: {situation.ml_prediction.time_to_failure_minutes or 'N/A'} minutes
- Cascade Risk: {', '.join(situation.ml_prediction.cascade_risk) or 'None'}

## URGENCY:
- Score: {situation.urgency_score}/100
- Level: {situation.urgency_level.value}

## SOP RUNBOOK (dari RAG Knowledge Base):
{situation.rag_runbook_content or 'Tidak ada runbook yang cocok ditemukan.'}
RAG Similarity: {situation.rag_similarity_score:.2f}
"""
        
        if situation.past_experiences:
            prompt += "\n## PENGALAMAN MASA LALU:\n"
            for exp in situation.past_experiences[:3]:
                prompt += f"- {exp}\n"
        
        if action_priorities:
            prompt += "\n## PRIORITAS AKSI (dari Scoring Engine):\n"
            for p in action_priorities[:5]:
                prompt += f"- {p['action'].value} (priority: {p['priority']}) — {p['reason']}\n"
        
        prompt += """
## INSTRUKSI:
Berdasarkan SEMUA data di atas, kembalikan JSON dengan format:
{
    "action": "<salah satu dari: no_op, restart_pod, scale_out, scale_in, rate_limit, migrate_pod, escalate>",
    "target_deployment": "<nama deployment>",
    "target_namespace": "<namespace>",
    "parameters": {
        "replicas_to_add": <number or null>,
        "replicas_to_remove": <number or null>,
        "target_node": "<node name or null>",
        "rate_limit_rps": <number or null>,
        "restart_strategy": "<rolling or immediate>"
    },
    "confidence": <0.0 - 1.0>,
    "reasoning": "<penjelasan logis berdasarkan DATA dan SOP>",
    "data_sources_used": ["<list sumber data yang digunakan>"]
}"""
        
        return prompt
    
    def select_action(self, situation: SituationReport,
                       action_priorities: list[dict] = None) -> ActionDecision:
        """
        Fungsi utama: Pilih aksi terbaik untuk situasi yang diberikan.
        
        Pipeline:
        1. Cek apakah Gemini tersedia
        2. Jika ya → Kirim prompt ke Gemini → Parse JSON response
        3. Jika tidak → Fallback ke rule-based engine
        4. Validasi output dengan Pydantic schema
        
        Args:
            situation: Laporan situasi lengkap
            action_priorities: Prioritas aksi dari Scoring Engine
        
        Returns:
            ActionDecision — Keputusan tervalidasi
        """
        if self.gemini_available:
            try:
                return self._select_with_gemini(situation, action_priorities)
            except Exception as e:
                logger.error(f"Gemini failed: {e}. Falling back to rule-based.")
                if self.fallback_enabled:
                    return self._select_with_rules(situation, action_priorities)
                raise
        else:
            logger.info("Gemini not available, using rule-based fallback")
            return self._select_with_rules(situation, action_priorities)
    
    def _select_with_gemini(self, situation: SituationReport,
                             action_priorities: list[dict] = None) -> ActionDecision:
        """
        Kirim prompt ke Gemini Flash dan parse response JSON.
        
        Gemini dikonfigurasi dengan:
        - response_mime_type="application/json" → Paksa output JSON
        - temperature=0.1 → Konsisten dan deterministik
        - system_instruction → Aturan ketat anti-halusinasi
        """
        prompt = self._build_prompt(situation, action_priorities)
        
        logger.info("Sending prompt to Gemini Flash...")
        
        timeout = self.llm_config.get("timeout_seconds", 10)
        retry_attempts = self.llm_config.get("retry_attempts", 3)
        
        for attempt in range(retry_attempts):
            try:
                response = self.model.generate_content(
                    prompt,
                    generation_config=self.generation_config,
                    request_options={"timeout": timeout}
                )
                
                # Parse JSON response
                response_text = response.text.strip()
                response_data = json.loads(response_text)
                
                # Validasi dan konversi ke Pydantic model
                decision = ActionDecision(
                    action=ActionType(response_data["action"]),
                    target_deployment=response_data.get("target_deployment", situation.pandas_metrics.target_pod),
                    target_namespace=response_data.get("target_namespace", "demo"),
                    parameters=ActionParameters(**response_data.get("parameters", {})),
                    confidence=float(response_data.get("confidence", 0.5)),
                    reasoning=response_data.get("reasoning", "No reasoning provided"),
                    data_sources_used=response_data.get("data_sources_used", [])
                )
                
                logger.success(
                    f"Gemini Decision: {decision.action.value} | "
                    f"Target: {decision.target_deployment} | "
                    f"Confidence: {decision.confidence:.2f}"
                )
                
                return decision
                
            except json.JSONDecodeError as e:
                logger.warning(f"Attempt {attempt + 1}: Invalid JSON from Gemini: {e}")
                continue
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}: Gemini error: {e}")
                continue
        
        # Semua retry gagal → fallback
        logger.warning("All Gemini retries failed, using rule-based fallback")
        return self._select_with_rules(situation, action_priorities)
    
    def _select_with_rules(self, situation: SituationReport,
                            action_priorities: list[dict] = None) -> ActionDecision:
        """
        Rule-based fallback jika Gemini API tidak tersedia.
        
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
