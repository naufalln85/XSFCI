# ============================================================
# XFSCI Decision Agent — Multi-LLM AI Agent
# ============================================================
# INTI UTAMA: Modul AI Agent dengan fallback chain:
#   Groq LPU (cloud, ultra-fast) → Gemini Flash (cloud) → Ollama (lokal) → Rule-based
#
# Mengambil keputusan self-healing berdasarkan:
#   1. Data numerik dari Pandas (FAKTA, bukan opini)
#   2. SOP Runbook dari RAG (DOKUMEN TERBUKTI)
#   3. Pengalaman masa lalu dari Experience Memory
#
# ANTI-HALUSINASI:
#   - Input: Hanya angka dari Pandas + dokumen dari RAG
#   - Output: JSON terkunci (Pydantic schema)
#   - Fallback Chain: Groq → Gemini → Ollama → Rule-based
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
import httpx

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
        
        # Setup Groq config
        self.groq_config = self.agent_config.get("groq", {})
        self.groq_available = False
        
        # Setup Ollama config
        self.ollama_config = self.agent_config.get("ollama", {})
        self.ollama_available = False
        
        # Setup LLM providers (urutan inisialisasi)
        self._setup_groq()
        self._setup_gemini()
        self._setup_ollama()
        
        logger.info(
            f"XFSCIDecisionAgent initialized | "
            f"Groq: {'✅' if self.groq_available else '❌'} ({self.groq_config.get('model', 'N/A')}) | "
            f"Gemini: {'✅' if self.gemini_available else '❌'} ({self.llm_config.get('model')}) | "
            f"Ollama: {'✅' if self.ollama_available else '❌'} ({self.ollama_config.get('model', 'N/A')})"
        )
    
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
    
    def _setup_groq(self):
        """
        Konfigurasi Groq Cloud API (LPU ultra-fast inference).
        
        Groq menggunakan chip LPU (Language Processing Unit) yang
        mampu inferensi model 8B pada kecepatan ~800 token/detik.
        API kompatibel dengan format OpenAI (chat completions).
        """
        if not self.groq_config.get("enabled", False):
            logger.info("Groq Cloud API disabled in config")
            return
        
        api_key_env = self.groq_config.get("api_key_env", "GROQ_API_KEY")
        self.groq_api_key = os.environ.get(api_key_env, "")
        
        if not self.groq_api_key:
            logger.warning(
                f"⚠️ {api_key_env} not set! Groq will be skipped. "
                f"Get free key at: https://console.groq.com/keys"
            )
            return
        
        self.groq_model = self.groq_config.get("model", "openai/gpt-oss-20b")
        self.groq_timeout = self.groq_config.get("timeout_seconds", 15)
        self.groq_temperature = self.groq_config.get("temperature", 0.1)
        self.groq_max_tokens = self.groq_config.get("max_tokens", 512)
        self.groq_retry_attempts = self.groq_config.get("retry_attempts", 2)
        
        # Validasi koneksi ke Groq API
        try:
            resp = httpx.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {self.groq_api_key}"},
                timeout=10
            )
            if resp.status_code == 200:
                models = [m["id"] for m in resp.json().get("data", [])]
                if self.groq_model in models:
                    self.groq_available = True
                    logger.success(f"Groq Cloud API ready | Model: {self.groq_model}")
                else:
                    # Model mungkin masih valid, Groq kadang tidak list semua
                    self.groq_available = True
                    logger.success(f"Groq Cloud API connected | Model: {self.groq_model} (not in list, trying anyway)")
            elif resp.status_code == 401:
                logger.warning("Groq API key invalid (401 Unauthorized)")
            else:
                logger.warning(f"Groq API responded with status {resp.status_code}")
        except Exception as e:
            logger.info(f"Groq API not reachable: {e} — will skip Groq")
    
    def _setup_ollama(self):
        """Konfigurasi Ollama sebagai fallback lokal."""
        if not self.ollama_config.get("enabled", False):
            logger.info("Ollama fallback disabled in config")
            return
        
        self.ollama_base_url = self.ollama_config.get("base_url", "http://localhost:11434")
        self.ollama_model = self.ollama_config.get("model", "qwen2.5:1.5b")
        self.ollama_timeout = self.ollama_config.get("timeout_seconds", 180)
        self.ollama_temperature = self.ollama_config.get("temperature", 0.1)
        
        # Cek apakah Ollama server berjalan
        try:
            resp = httpx.get(f"{self.ollama_base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                if any(self.ollama_model in m for m in models):
                    self.ollama_available = True
                    logger.success(f"Ollama ready | Model: {self.ollama_model}")
                else:
                    logger.warning(
                        f"Ollama running but model '{self.ollama_model}' not found. "
                        f"Available: {models}. Run: ollama pull {self.ollama_model}"
                    )
            else:
                logger.warning(f"Ollama server responded with status {resp.status_code}")
        except Exception:
            logger.info("Ollama not running — will skip Ollama fallback")
    
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
        # === Fallback Chain: Groq → Gemini → Ollama → Rule-based ===
        
        # Step 1: Coba Groq Cloud API (ultra-fast, primary)
        if self.groq_available:
            try:
                result = self._select_with_groq(situation, action_priorities)
                if result is not None:
                    return result
            except Exception as e:
                logger.error(f"Groq pipeline error: {e}")
        
        # Step 2: Coba Gemini Flash (cloud backup)
        if self.gemini_available:
            try:
                logger.info("Falling back to Gemini Flash...")
                result = self._select_with_gemini(situation, action_priorities)
                if result is not None:
                    return result
            except Exception as e:
                logger.error(f"Gemini pipeline error: {e}")
        
        # Step 3: Coba Ollama (lokal)
        if self.ollama_available:
            try:
                logger.info("Falling back to Ollama (local)...")
                result = self._select_with_ollama(situation, action_priorities)
                if result is not None:
                    return result
            except Exception as e:
                logger.error(f"Ollama pipeline error: {e}")
        
        # Step 4: Rule-based fallback (always works)
        if not self.groq_available and not self.gemini_available and not self.ollama_available:
            logger.info("No LLM available, using rule-based fallback")
        else:
            logger.warning("All LLM attempts failed, using rule-based fallback")
        return self._select_with_rules(situation, action_priorities)
    
    def _select_with_groq(self, situation: SituationReport,
                           action_priorities: list[dict] = None) -> ActionDecision:
        """
        Primary LLM: Groq Cloud API (LPU ultra-fast inference).
        
        Groq menggunakan chip LPU (Language Processing Unit) yang
        mampu inferensi model 8B pada ~800 token/detik. API format
        kompatibel dengan OpenAI chat completions.
        
        Menggunakan compact prompt untuk efisiensi token dan
        response_format=json_object untuk paksa output JSON.
        """
        # Gunakan compact prompt (efisien, cocok untuk 8B model)
        compact_prompt = self._build_compact_prompt(situation, action_priorities)
        
        logger.info(f"Sending prompt to Groq ({self.groq_model})...")
        
        for attempt in range(self.groq_retry_attempts):
            try:
                start_time = time.time()
                
                response = httpx.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.groq_api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.groq_model,
                        "messages": [
                            {"role": "system", "content": self.SYSTEM_PROMPT},
                            {"role": "user", "content": compact_prompt}
                        ],
                        "temperature": self.groq_temperature,
                        "max_tokens": self.groq_max_tokens,
                        "response_format": {"type": "json_object"},
                        "stream": False
                    },
                    timeout=self.groq_timeout
                )
                
                elapsed = time.time() - start_time
                
                if response.status_code == 429:
                    logger.warning(f"Attempt {attempt + 1}: Groq rate limit (429)")
                    if attempt < self.groq_retry_attempts - 1:
                        backoff = 2 ** (attempt + 1)
                        logger.info(f"Retrying in {backoff}s...")
                        time.sleep(backoff)
                    continue
                
                if response.status_code != 200:
                    logger.warning(f"Attempt {attempt + 1}: Groq returned status {response.status_code}")
                    if attempt < self.groq_retry_attempts - 1:
                        time.sleep(2 ** (attempt + 1))
                    continue
                
                result = response.json()
                response_text = result["choices"][0]["message"]["content"].strip()
                
                # Log timing dan usage
                usage = result.get("usage", {})
                logger.info(
                    f"Groq responded in {elapsed:.2f}s | "
                    f"Tokens: {usage.get('prompt_tokens', '?')} in → "
                    f"{usage.get('completion_tokens', '?')} out"
                )
                
                response_data = json.loads(response_text)
                
                # Validasi dan konversi ke Pydantic model
                decision = ActionDecision(
                    action=ActionType(response_data["action"]),
                    target_deployment=response_data.get("target_deployment", situation.pandas_metrics.target_pod),
                    target_namespace=response_data.get("target_namespace", "demo"),
                    parameters=ActionParameters(**response_data.get("parameters", {})),
                    confidence=float(response_data.get("confidence", 0.5)),
                    reasoning=response_data.get("reasoning", "No reasoning provided"),
                    data_sources_used=response_data.get("data_sources_used", ["groq_cloud"])
                )
                
                logger.success(
                    f"Groq Decision: {decision.action.value} | "
                    f"Target: {decision.target_deployment} | "
                    f"Confidence: {decision.confidence:.2f} | "
                    f"Latency: {elapsed:.2f}s"
                )
                
                return decision
                
            except json.JSONDecodeError as e:
                logger.warning(f"Attempt {attempt + 1}: Invalid JSON from Groq: {e}")
                if attempt < self.groq_retry_attempts - 1:
                    time.sleep(2 ** (attempt + 1))
                continue
            except httpx.TimeoutException:
                logger.warning(f"Attempt {attempt + 1}: Groq timeout after {self.groq_timeout}s")
                if attempt < self.groq_retry_attempts - 1:
                    time.sleep(2 ** (attempt + 1))
                continue
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}: Groq error: {e}")
                if attempt < self.groq_retry_attempts - 1:
                    time.sleep(2 ** (attempt + 1))
                continue
        
        logger.warning("All Groq retries failed")
        return None
    
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
                if attempt < retry_attempts - 1:
                    time.sleep(2 ** (attempt + 1))  # Exponential backoff: 2s, 4s, 8s
                continue
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}: Gemini error: {e}")
                if attempt < retry_attempts - 1:
                    backoff = 2 ** (attempt + 1)
                    logger.info(f"Retrying in {backoff}s (backoff)...")
                    time.sleep(backoff)  # Exponential backoff: 2s, 4s, 8s
                continue
        
        # Semua retry gagal → return None agar fallback chain lanjut ke Ollama
        logger.warning("All Gemini retries failed")
        return None
    
    def _build_compact_prompt(self, situation: SituationReport,
                              action_priorities: list[dict] = None) -> str:
        """
        Prompt ringkas untuk model kecil (1.5B) di CPU-only mode.
        
        Mengurangi token count dari ~2000+ menjadi ~400 agar
        prompt eval selesai dalam waktu yang wajar di 4 CPU cores.
        RAG content di-skip karena terlalu besar untuk model kecil.
        """
        m = situation.pandas_metrics
        ml = situation.ml_prediction
        
        # Bangun string prioritas aksi (top 3 saja)
        priority_str = ""
        if action_priorities:
            top3 = action_priorities[:3]
            priority_str = " | ".join(
                f"{p['action'].value}({p['priority']})" for p in top3
            )
        
        prompt = f"""K8s pod incident. Pick the best action.

METRICS:
- pod: {m.target_pod}, node: {m.target_node}
- cpu_5m: {m.cpu_usage_avg_5m}%, mem: {m.memory_usage_mb:.0f}MB ({m.memory_usage_percent:.0f}%)
- mem_growth: {m.memory_growth_rate_mb_per_min:+.1f}MB/min, restarts_1h: {m.pod_restarts_1h}
- replicas: {m.current_replicas}, rps: {m.request_rate_rps:.0f}, errors: {m.error_rate_percent:.1f}%
- p99_latency: {m.latency_p99_ms:.0f}ms

ML: risk={ml.risk_score:.2f}, anomaly={ml.anomaly_type.value}, confidence={ml.confidence:.2f}
URGENCY: {situation.urgency_score}/100 ({situation.urgency_level.value})
PRIORITIES: {priority_str or 'none'}

RULES:
- urgency<30: no_op
- urgency 30-50: scale_out preventive
- urgency 50-70: restart_pod or scale_out
- urgency>85: immediate action
- confidence<0.85: escalate

Respond with ONLY this JSON:
{{"action":"<no_op|restart_pod|scale_out|scale_in|rate_limit|migrate_pod|escalate>","target_deployment":"{m.target_pod}","confidence":<0.0-1.0>,"reasoning":"<short reason>"}}"""
        
        return prompt
    
    def _select_with_ollama(self, situation: SituationReport,
                             action_priorities: list[dict] = None) -> ActionDecision:
        """
        Fallback ke Ollama lokal jika Gemini gagal.
        
        Menggunakan HTTP API langsung ke Ollama server.
        Prompt diringkas khusus untuk model kecil (1.5B) agar
        tidak timeout di CPU-only mode.
        """
        # Gunakan prompt ringkas untuk model kecil
        compact_prompt = self._build_compact_prompt(situation, action_priorities)
        
        logger.info(f"Sending compact prompt to Ollama ({self.ollama_model})...")
        logger.debug(f"Prompt length: ~{len(compact_prompt.split())} words")
        
        try:
            response = httpx.post(
                f"{self.ollama_base_url}/api/generate",
                json={
                    "model": self.ollama_model,
                    "prompt": compact_prompt,
                    "format": "json",
                    "stream": False,
                    "options": {
                        "temperature": self.ollama_temperature,
                        "num_predict": 256,  # Output pendek saja
                    }
                },
                timeout=self.ollama_timeout
            )
            
            if response.status_code != 200:
                logger.warning(f"Ollama returned status {response.status_code}")
                return None
            
            result = response.json()
            response_text = result.get("response", "").strip()
            
            # Log timing untuk monitoring
            total_dur = result.get("total_duration", 0) / 1e9  # ns → s
            eval_dur = result.get("eval_duration", 0) / 1e9
            logger.info(f"Ollama responded in {total_dur:.1f}s (eval: {eval_dur:.1f}s)")
            
            response_data = json.loads(response_text)
            
            # Validasi dan konversi ke Pydantic model
            decision = ActionDecision(
                action=ActionType(response_data["action"]),
                target_deployment=response_data.get("target_deployment", situation.pandas_metrics.target_pod),
                target_namespace=response_data.get("target_namespace", "demo"),
                parameters=ActionParameters(**response_data.get("parameters", {})),
                confidence=float(response_data.get("confidence", 0.5)),
                reasoning=response_data.get("reasoning", "No reasoning provided"),
                data_sources_used=response_data.get("data_sources_used", ["ollama_local"])
            )
            
            logger.success(
                f"Ollama Decision: {decision.action.value} | "
                f"Target: {decision.target_deployment} | "
                f"Confidence: {decision.confidence:.2f}"
            )
            
            return decision
            
        except json.JSONDecodeError as e:
            logger.warning(f"Ollama returned invalid JSON: {e}")
            return None
        except httpx.TimeoutException:
            logger.warning(f"Ollama timeout after {self.ollama_timeout}s")
            return None
        except Exception as e:
            logger.warning(f"Ollama error: {e}")
            return None
    
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
