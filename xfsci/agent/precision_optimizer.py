# ============================================================
# XFSCI Precision Optimizer — ML Regression Auto-Tuning
# ============================================================
# Modul ini menggantikan kemampuan RL dalam optimasi presisi
# (misal: menemukan jumlah replika optimal) dengan:
#   1. ML Regression (Gradient Boosting / XGBoost)
#   2. Binary Search Auto-Tuning
#
# MENGAPA LEBIH BAIK DARI RL?
# - ML Regression: Prediksi langsung dalam 1 inference (~10ms)
# - Binary Search: Konvergen dalam max 5 langkah (bukan jutaan episode)
# - Tidak perlu reward function atau training environment
# ============================================================

import time
from typing import Optional
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
import joblib
from loguru import logger

try:
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from xgboost import XGBRegressor
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn/xgboost not installed, PrecisionOptimizer disabled")

from agent.action_schema import PandasMetrics


class PrecisionOptimizer:
    """
    ML-based optimizer untuk menghitung jumlah replika optimal
    dan resource allocation secara presisi.
    
    Dua metode:
    1. predict_optimal_replicas() — ML Regression dari data historis
    2. auto_tune_binary_search() — Binary Search iteratif
    """
    
    def __init__(self, config_path: str = None):
        """Inisialisasi optimizer dengan konfigurasi."""
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        self.optimizer_config = config.get("precision_optimizer", {})
        self.model_type = self.optimizer_config.get("model_type", "gradient_boosting")
        self.model_path = Path(__file__).parent.parent / self.optimizer_config.get(
            "model_path", "models/optimizer/replica_predictor.pkl"
        )
        self.auto_tune_config = self.optimizer_config.get("auto_tune", {})
        self.training_config = self.optimizer_config.get("training", {})
        
        # Load model jika sudah ada
        self.model = None
        self._load_model()
        
        logger.info(f"PrecisionOptimizer initialized | Model: {self.model_type}")
    
    def _load_model(self):
        """Load model yang sudah di-train dari disk."""
        if self.model_path.exists():
            try:
                self.model = joblib.load(self.model_path)
                logger.info(f"Loaded optimizer model from: {self.model_path}")
            except Exception as e:
                logger.warning(f"Failed to load model: {e}")
                self.model = None
    
    def _create_model(self):
        """Buat model ML baru berdasarkan config."""
        if not SKLEARN_AVAILABLE:
            return None
        
        if self.model_type == "gradient_boosting":
            return GradientBoostingRegressor(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                random_state=42
            )
        elif self.model_type == "random_forest":
            return RandomForestRegressor(
                n_estimators=100,
                max_depth=10,
                random_state=42
            )
        elif self.model_type == "xgboost":
            return XGBRegressor(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                random_state=42
            )
        else:
            return GradientBoostingRegressor(random_state=42)
    
    def train_model(self, historical_data: pd.DataFrame) -> bool:
        """
        Train model dari data historis.
        
        Data historis harus memiliki kolom:
        - request_rate: RPS saat itu
        - cpu_usage: CPU% saat itu
        - memory_usage: Memory% saat itu
        - error_rate: Error% saat itu
        - latency_p99: Latency P99 saat itu
        - hour: Jam saat itu (0-23, untuk pola diurnal)
        - optimal_replicas: Jumlah replika yang optimal (label/target)
        
        Args:
            historical_data: DataFrame dengan kolom di atas
        
        Returns:
            True jika training berhasil
        """
        if not SKLEARN_AVAILABLE:
            logger.error("scikit-learn not available for training")
            return False
        
        min_samples = self.training_config.get("min_samples", 100)
        if len(historical_data) < min_samples:
            logger.warning(
                f"Not enough training data: {len(historical_data)} < {min_samples}. "
                f"Using heuristic fallback."
            )
            return False
        
        # Feature columns
        feature_cols = [
            "request_rate", "cpu_usage", "memory_usage",
            "error_rate", "latency_p99", "hour"
        ]
        
        # Pastikan semua kolom ada
        missing = [c for c in feature_cols if c not in historical_data.columns]
        if missing:
            logger.error(f"Missing columns in training data: {missing}")
            return False
        
        X = historical_data[feature_cols].fillna(0)
        y = historical_data["optimal_replicas"]
        
        # Train model
        self.model = self._create_model()
        self.model.fit(X, y)
        
        # Save model
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.model, self.model_path)
        
        # Log feature importance
        if hasattr(self.model, "feature_importances_"):
            importance = dict(zip(feature_cols, self.model.feature_importances_))
            logger.info(f"Feature importance: {importance}")
        
        logger.success(
            f"Optimizer model trained on {len(historical_data)} samples, "
            f"saved to {self.model_path}"
        )
        return True
    
    def predict_optimal_replicas(self, metrics: PandasMetrics) -> int:
        """
        Prediksi jumlah replika optimal berdasarkan metrik saat ini.
        
        Jika model sudah di-train → Gunakan ML prediction
        Jika model belum ada → Gunakan heuristic sederhana
        
        Args:
            metrics: Metrik saat ini dari Pandas Processor
        
        Returns:
            Jumlah replika optimal (integer)
        """
        min_replicas = self.auto_tune_config.get("min_replicas", 1)
        max_replicas = self.auto_tune_config.get("max_replicas", 20)
        
        if self.model is not None:
            # ML Prediction
            features = pd.DataFrame([{
                "request_rate": metrics.request_rate_rps,
                "cpu_usage": metrics.cpu_usage_avg_5m,
                "memory_usage": metrics.memory_usage_percent,
                "error_rate": metrics.error_rate_percent,
                "latency_p99": metrics.latency_p99_ms,
                "hour": metrics.timestamp.hour,
            }])
            
            prediction = self.model.predict(features)[0]
            optimal = int(round(prediction))
            optimal = max(min_replicas, min(optimal, max_replicas))
            
            logger.info(
                f"ML Prediction: {optimal} replicas "
                f"(raw: {prediction:.1f}, clamped to [{min_replicas}, {max_replicas}])"
            )
            return optimal
        else:
            # Heuristic fallback
            return self._heuristic_replicas(metrics, min_replicas, max_replicas)
    
    def _heuristic_replicas(self, metrics: PandasMetrics,
                             min_r: int, max_r: int) -> int:
        """
        Heuristic sederhana untuk estimasi replika optimal.
        
        Rumus dasar:
        - Base: 2 replika
        - +1 per 50 RPS
        - +1 jika CPU > 60%
        - +1 jika memory > 70%
        - +1 jika error rate > 3%
        """
        replicas = 2  # Base
        
        # RPS-based scaling
        replicas += int(metrics.request_rate_rps / 50)
        
        # Resource pressure
        if metrics.cpu_usage_avg_5m > 60:
            replicas += 1
        if metrics.cpu_usage_avg_5m > 80:
            replicas += 1
        if metrics.memory_usage_percent > 70:
            replicas += 1
        if metrics.error_rate_percent > 3:
            replicas += 1
        if metrics.latency_p99_ms > 500:
            replicas += 1
        
        optimal = max(min_r, min(replicas, max_r))
        
        logger.info(
            f"Heuristic estimate: {optimal} replicas "
            f"(RPS={metrics.request_rate_rps:.0f}, "
            f"CPU={metrics.cpu_usage_avg_5m:.0f}%)"
        )
        return optimal
    
    def calculate_replicas_delta(self, current_replicas: int,
                                  metrics: PandasMetrics) -> int:
        """
        Hitung berapa replika yang perlu ditambah/dikurangi.
        
        Returns:
            Positive = scale out, Negative = scale in, 0 = no change
        """
        optimal = self.predict_optimal_replicas(metrics)
        delta = optimal - current_replicas
        
        logger.info(
            f"Replica delta: {delta:+d} "
            f"(current={current_replicas}, optimal={optimal})"
        )
        return delta
