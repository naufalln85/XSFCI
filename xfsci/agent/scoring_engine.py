# ============================================================
# XFSCI Deterministic Scoring Engine
# ============================================================
# Modul ini menggantikan RL Reward Function dengan sistem
# scoring yang 100% deterministik dan transparan.
#
# PERBEDAAN DENGAN RL REWARD:
# - RL Reward: Dipelajari lewat trial-error jutaan episode
# - Scoring Engine: Rumus matematika tetap, langsung akurat
# - Bisa diaudit, dimodifikasi tanpa re-training
#
# Tiga jenis skor:
# 1. Urgency Score (0-100): Seberapa darurat situasi
# 2. Action Priority Score: Ranking aksi terbaik
# 3. Post-Action Score (0-100): Evaluasi keberhasilan aksi
# ============================================================

from typing import Optional

import yaml
from pathlib import Path
from loguru import logger

from agent.action_schema import (
    PandasMetrics, MLPrediction, UrgencyLevel,
    ActionType, PostActionResult
)


class DeterministicScoringEngine:
    """
    Menghitung skor urgensi dan evaluasi keberhasilan secara
    deterministik (100% rumus matematika Python).
    
    Tidak menggunakan neural network atau LLM — setiap skor
    bisa diverifikasi secara manual dengan kalkulator.
    """
    
    def __init__(self, config_path: str = None):
        """Load scoring weights dari config.yaml."""
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        self.scoring_config = config.get("scoring", {})
        self.urgency_config = self.scoring_config.get("urgency", {})
        self.levels_config = self.scoring_config.get("levels", {})
        self.post_action_config = self.scoring_config.get("post_action", {})
        
        logger.info("DeterministicScoringEngine initialized")
    
    def calculate_urgency_score(self, ml_prediction: MLPrediction,
                                 pandas_metrics: PandasMetrics) -> float:
        """
        Hitung Urgency Score (0-100) berdasarkan gabungan output ML
        dan metrik Pandas.
        
        RUMUS TRANSPARAN:
          score = (ml_risk × 40) + (ml_conf × 20) + leak_bonus + error_bonus
                  + latency_bonus + restart_bonus
        
        Setiap komponen bisa diaudit satu per satu.
        
        Args:
            ml_prediction: Output dari ML Prediction model
            pandas_metrics: Output dari Pandas Metric Processor
        
        Returns:
            Float 0-100 (semakin tinggi = semakin darurat)
        """
        score = 0.0
        breakdown = {}
        
        # --- Komponen 1: ML Risk Score (max 40 poin) ---
        ml_weight = self.urgency_config.get("ml_risk_score_weight", 40)
        ml_component = ml_prediction.risk_score * ml_weight
        score += ml_component
        breakdown["ml_risk"] = round(ml_component, 2)
        
        # --- Komponen 2: ML Confidence (max 20 poin) ---
        conf_weight = self.urgency_config.get("ml_confidence_weight", 20)
        conf_component = ml_prediction.confidence * conf_weight
        score += conf_component
        breakdown["ml_confidence"] = round(conf_component, 2)
        
        # --- Komponen 3: Memory Leak Detection (max 15 poin) ---
        mem_threshold = self.urgency_config.get("memory_leak_threshold_mb_min", 5)
        mem_score = self.urgency_config.get("memory_leak_score", 15)
        if pandas_metrics.memory_growth_rate_mb_per_min > mem_threshold:
            score += mem_score
            breakdown["memory_leak"] = mem_score
        else:
            breakdown["memory_leak"] = 0
        
        # --- Komponen 4: Error Rate (max 10 poin) ---
        err_threshold = self.urgency_config.get("error_rate_threshold_pct", 5)
        err_score = self.urgency_config.get("error_rate_score", 10)
        if pandas_metrics.error_rate_percent > err_threshold:
            score += err_score
            breakdown["error_rate"] = err_score
        else:
            breakdown["error_rate"] = 0
        
        # --- Komponen 5: Latency (max 10 poin) ---
        lat_threshold = self.urgency_config.get("latency_p99_threshold_ms", 500)
        lat_score = self.urgency_config.get("latency_score", 10)
        if pandas_metrics.latency_p99_ms > lat_threshold:
            score += lat_score
            breakdown["latency"] = lat_score
        else:
            breakdown["latency"] = 0
        
        # --- Komponen 6: Restart Count (max 5 poin) ---
        rst_threshold = self.urgency_config.get("restart_count_threshold", 3)
        rst_score = self.urgency_config.get("restart_score", 5)
        if pandas_metrics.pod_restarts_1h >= rst_threshold:
            score += rst_score
            breakdown["restarts"] = rst_score
        else:
            breakdown["restarts"] = 0
        
        # Clamp ke 0-100
        final_score = min(max(score, 0.0), 100.0)
        
        logger.info(
            f"Urgency Score: {final_score:.1f}/100 | "
            f"Breakdown: {breakdown}"
        )
        
        return round(final_score, 2)
    
    def get_urgency_level(self, score: float) -> UrgencyLevel:
        """
        Konversi skor numerik ke level urgensi.
        
        Thresholds dari config.yaml:
          < 30  = LOW
          30-50 = MEDIUM
          50-70 = HIGH
          > 85  = CRITICAL
        """
        critical = self.levels_config.get("critical", 85)
        high = self.levels_config.get("high", 70)
        medium = self.levels_config.get("medium", 50)
        
        if score >= critical:
            return UrgencyLevel.CRITICAL
        elif score >= high:
            return UrgencyLevel.HIGH
        elif score >= medium:
            return UrgencyLevel.MEDIUM
        else:
            return UrgencyLevel.LOW
    
    def calculate_action_priority(self, pandas_metrics: PandasMetrics,
                                    urgency_level: UrgencyLevel) -> list[dict]:
        """
        Ranking aksi berdasarkan situasi — rule-based, bukan AI.
        
        Ini memberikan AI Agent "petunjuk" aksi mana yang paling
        logis untuk situasi tertentu, tapi AI Agent tetap yang
        memutuskan final.
        
        Returns:
            List of {action, priority, reason} sorted by priority
        """
        priorities = []
        
        # Memory leak → Scale Out lalu Restart
        if pandas_metrics.memory_growth_rate_mb_per_min > 5:
            priorities.append({
                "action": ActionType.SCALE_OUT,
                "priority": 90,
                "reason": f"Memory growth {pandas_metrics.memory_growth_rate_mb_per_min:.1f} MB/min (leak detected)"
            })
            priorities.append({
                "action": ActionType.RESTART_POD,
                "priority": 80,
                "reason": "Restart pod untuk clear memory leak setelah scale out"
            })
        
        # CPU Overload → Scale Out atau Rate Limit
        if pandas_metrics.cpu_usage_avg_5m > 80:
            priorities.append({
                "action": ActionType.SCALE_OUT,
                "priority": 85,
                "reason": f"CPU {pandas_metrics.cpu_usage_avg_5m:.1f}% (overloaded)"
            })
            if pandas_metrics.request_rate_rps > 100:
                priorities.append({
                    "action": ActionType.RATE_LIMIT,
                    "priority": 75,
                    "reason": f"High RPS ({pandas_metrics.request_rate_rps:.0f}) causing CPU pressure"
                })
        
        # Crash Loop → Scale Out (jaga availability)
        if pandas_metrics.pod_restarts_1h >= 3:
            priorities.append({
                "action": ActionType.SCALE_OUT,
                "priority": 88,
                "reason": f"Pod restart {pandas_metrics.pod_restarts_1h}x (crash loop)"
            })
            priorities.append({
                "action": ActionType.ESCALATE,
                "priority": 70,
                "reason": "Crash loop mungkin butuh investigasi kode"
            })
        
        # Latency tinggi → Rate Limit atau Migrate
        if pandas_metrics.latency_p99_ms > 500:
            priorities.append({
                "action": ActionType.RATE_LIMIT,
                "priority": 72,
                "reason": f"Latency P99 {pandas_metrics.latency_p99_ms:.0f}ms (SLA violation)"
            })
            priorities.append({
                "action": ActionType.MIGRATE_POD,
                "priority": 65,
                "reason": "Migrasi ke node yang lebih senggang"
            })
        
        # Jika LOW urgency → No-op
        if urgency_level == UrgencyLevel.LOW:
            priorities.append({
                "action": ActionType.NO_OP,
                "priority": 95,
                "reason": "Semua metrik dalam batas normal"
            })
        
        # Sort by priority (descending)
        priorities.sort(key=lambda x: x["priority"], reverse=True)
        
        return priorities
    
    def calculate_post_action_score(self, metrics_before: PandasMetrics,
                                      metrics_after: PandasMetrics,
                                      action_taken: ActionType) -> float:
        """
        Evaluasi keberhasilan aksi setelah dieksekusi.
        
        Dibandingkan metrik SEBELUM dan SESUDAH aksi:
        - Error rate turun? → +30 poin
        - Latency membaik? → +30 poin
        - Memory stabil? → +40 poin
        
        MENGAPA INI LEBIH BAIK DARI RL REWARD?
        - RL reward dihitung selama training (bisa bias)
        - Post-action score dihitung dari FAKTA NYATA di produksi
        - Tidak bisa di-hack atau over-fitted
        
        Returns:
            Float 0-100 (semakin tinggi = semakin berhasil)
        """
        score = 0.0
        details = {}
        
        # --- Error Rate ---
        err_weight = self.post_action_config.get("error_rate_improved", 30)
        if metrics_after.error_rate_percent < metrics_before.error_rate_percent:
            # Proporsional: semakin besar penurunan, semakin tinggi skor
            reduction = metrics_before.error_rate_percent - metrics_after.error_rate_percent
            ratio = min(reduction / max(metrics_before.error_rate_percent, 0.01), 1.0)
            err_score = err_weight * ratio
            score += err_score
            details["error_improvement"] = round(err_score, 2)
        else:
            details["error_improvement"] = 0
        
        # --- Latency ---
        lat_weight = self.post_action_config.get("latency_improved", 30)
        if metrics_after.latency_p99_ms < metrics_before.latency_p99_ms:
            reduction = metrics_before.latency_p99_ms - metrics_after.latency_p99_ms
            ratio = min(reduction / max(metrics_before.latency_p99_ms, 0.01), 1.0)
            lat_score = lat_weight * ratio
            score += lat_score
            details["latency_improvement"] = round(lat_score, 2)
        else:
            details["latency_improvement"] = 0
        
        # --- Memory Stabilization ---
        mem_weight = self.post_action_config.get("memory_stabilized", 40)
        if metrics_after.memory_growth_rate_mb_per_min <= 0:
            score += mem_weight
            details["memory_stabilized"] = mem_weight
        elif (metrics_after.memory_growth_rate_mb_per_min <
              metrics_before.memory_growth_rate_mb_per_min):
            # Partially improved
            ratio = 1.0 - (metrics_after.memory_growth_rate_mb_per_min /
                          max(metrics_before.memory_growth_rate_mb_per_min, 0.01))
            partial = mem_weight * max(ratio, 0)
            score += partial
            details["memory_stabilized"] = round(partial, 2)
        else:
            details["memory_stabilized"] = 0
        
        # Bonus: No-op saat healthy (reward correct inaction)
        if (action_taken == ActionType.NO_OP and
            metrics_after.error_rate_percent < 1 and
            metrics_after.latency_p99_ms < 200):
            score = 100.0  # Perfect score for correct inaction
            details["correct_no_op"] = True
        
        final_score = min(max(score, 0.0), 100.0)
        
        logger.info(
            f"Post-Action Score: {final_score:.1f}/100 | "
            f"Action: {action_taken.value} | Details: {details}"
        )
        
        return round(final_score, 2)
