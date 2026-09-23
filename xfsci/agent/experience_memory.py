# ============================================================
# XFSCI Experience Memory — AI Agent Learning System
# ============================================================
# Modul ini menyimpan pengalaman aksi AI Agent (berhasil/gagal)
# ke ChromaDB, sehingga AI Agent bisa "belajar" dari masa lalu.
#
# PENGGANTI RL REPLAY BUFFER:
# - RL Replay Buffer: Menyimpan (state, action, reward, next_state)
#   di RAM, dipakai untuk training neural network
# - Experience Memory: Menyimpan (situation, action, result, score)
#   di ChromaDB, dipakai untuk RAG retrieval oleh AI Agent
#
# KEUNGGULAN:
# - Tidak perlu re-training model (instant learning)
# - Bisa di-query secara semantic ("cari pengalaman serupa")
# - Auto-generate runbook baru dari pengalaman sukses
# - Persistent (tidak hilang saat restart)
# ============================================================

import json
from datetime import datetime
from typing import Optional
from pathlib import Path

import yaml
import chromadb
from chromadb.utils import embedding_functions
from loguru import logger

from agent.action_schema import (
    ActionDecision, ActionType, PostActionResult,
    PandasMetrics, MLPrediction, UrgencyLevel
)


class ExperienceMemory:
    """
    Sistem memori pengalaman untuk AI Agent.
    
    Setiap kali AI Agent mengambil aksi, hasilnya (berhasil/gagal)
    disimpan ke sini. Di masa depan, AI Agent bisa:
    1. Mencari pengalaman serupa sebelum mengambil keputusan
    2. Mengetahui aksi mana yang paling sering berhasil
    3. Auto-generate runbook baru dari pola sukses berulang
    """
    
    def __init__(self, config_path: str = None):
        """Inisialisasi Experience Memory dengan ChromaDB."""
        if config_path is None:
            config_path = str(Path(__file__).parent.parent / "configs" / "config.yaml")
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        self.mem_config = config.get("experience_memory", {})
        self.max_entries = self.mem_config.get("max_entries", 10000)
        self.auto_runbook = self.mem_config.get("auto_generate_runbook", True)
        self.min_success_count = self.mem_config.get("min_success_count", 3)
        
        # Setup ChromaDB
        base_dir = Path(__file__).parent.parent
        persist_dir = base_dir / self.mem_config.get(
            "persist_directory", "knowledge_base/experience_db"
        )
        persist_dir.mkdir(parents=True, exist_ok=True)
        
        embedding_model = config.get("rag", {}).get("embedding_model", "all-MiniLM-L6-v2")
        self.embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=embedding_model
        )
        
        self.client = chromadb.PersistentClient(path=str(persist_dir))
        collection_name = self.mem_config.get("collection_name", "xfsci_experience")
        
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            embedding_function=self.embed_fn,
            metadata={"description": "XFSCI AI Agent Experience Memory"}
        )
        
        logger.info(
            f"ExperienceMemory initialized | "
            f"Entries: {self.collection.count()} | "
            f"Max: {self.max_entries}"
        )
    
    def record_action(self, decision: ActionDecision,
                       result: PostActionResult,
                       metrics: PandasMetrics,
                       ml_prediction: MLPrediction,
                       urgency_score: float):
        """
        Simpan pengalaman aksi ke ChromaDB.
        
        Data yang disimpan:
        - Konteks situasi (metrik, prediksi ML, urgency)
        - Aksi yang diambil
        - Hasil (berhasil/gagal, skor)
        - Timestamp
        
        Ini disimpan sebagai dokumen teks yang bisa di-query
        secara semantic oleh AI Agent di masa depan.
        """
        # Buat deskripsi teks untuk embedding
        description = (
            f"Deployment: {decision.target_deployment}. "
            f"Anomaly: {ml_prediction.anomaly_type.value}. "
            f"CPU: {metrics.cpu_usage_avg_5m:.0f}%. "
            f"Memory: {metrics.memory_usage_mb:.0f}MB ({metrics.memory_usage_percent:.0f}%). "
            f"Memory growth: {metrics.memory_growth_rate_mb_per_min:+.1f} MB/min. "
            f"Error rate: {metrics.error_rate_percent:.1f}%. "
            f"Latency P99: {metrics.latency_p99_ms:.0f}ms. "
            f"Restarts: {metrics.pod_restarts_1h}. "
            f"Urgency: {urgency_score:.0f}/100. "
            f"Action taken: {decision.action.value}. "
            f"Result: {'SUCCESS' if result.success else 'FAILED'}. "
            f"Score: {result.score:.0f}/100. "
            f"Reasoning: {decision.reasoning}"
        )
        
        # Metadata terstruktur
        metadata = {
            "deployment": decision.target_deployment,
            "namespace": decision.target_namespace,
            "anomaly_type": ml_prediction.anomaly_type.value,
            "action": decision.action.value,
            "success": result.success,
            "score": result.score,
            "urgency_score": urgency_score,
            "confidence": decision.confidence,
            "cpu_usage": metrics.cpu_usage_avg_5m,
            "memory_percent": metrics.memory_usage_percent,
            "memory_growth": metrics.memory_growth_rate_mb_per_min,
            "error_rate": metrics.error_rate_percent,
            "latency_p99": metrics.latency_p99_ms,
            "pod_restarts": metrics.pod_restarts_1h,
            "timestamp": datetime.utcnow().isoformat(),
            "type": "experience"
        }
        
        # Generate unique ID
        exp_id = f"exp-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{decision.action.value}"
        
        # Simpan ke ChromaDB
        self.collection.add(
            ids=[exp_id],
            documents=[description],
            metadatas=[metadata]
        )
        
        logger.info(
            f"Experience recorded: {exp_id} | "
            f"Action: {decision.action.value} | "
            f"{'✅ SUCCESS' if result.success else '❌ FAILED'} | "
            f"Score: {result.score:.0f}/100"
        )
        
        # Check jika perlu auto-generate runbook
        if self.auto_runbook and result.success:
            self._check_auto_runbook(decision, ml_prediction.anomaly_type.value)
        
        # Enforce max entries
        if self.collection.count() > self.max_entries:
            self._cleanup_oldest()
    
    def query_similar_experience(self, situation_description: str,
                                   top_k: int = 5,
                                   success_only: bool = False) -> list[dict]:
        """
        Cari pengalaman serupa di masa lalu.
        
        AI Agent memanggil fungsi ini SEBELUM mengambil keputusan
        untuk melihat: "Apa yang terjadi terakhir kali situasi
        seperti ini muncul? Aksi apa yang berhasil?"
        
        Args:
            situation_description: Deskripsi situasi saat ini
            top_k: Jumlah hasil teratas
            success_only: Jika True, hanya kembalikan pengalaman sukses
        
        Returns:
            List of experience dictionaries
        """
        if self.collection.count() == 0:
            return []
        
        # Query semantic search
        where_filter = {"success": True} if success_only else None
        
        results = self.collection.query(
            query_texts=[situation_description],
            n_results=min(top_k, self.collection.count()),
            where=where_filter,
            include=["documents", "metadatas", "distances"]
        )
        
        experiences = []
        if results and results["ids"] and results["ids"][0]:
            for i in range(len(results["ids"][0])):
                exp = {
                    "id": results["ids"][0][i],
                    "description": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i],
                    "similarity": max(0, 1.0 - results["distances"][0][i])
                }
                experiences.append(exp)
        
        logger.info(
            f"Found {len(experiences)} similar experiences "
            f"(success_only={success_only})"
        )
        
        return experiences
    
    def get_success_rate(self, action: ActionType,
                          anomaly_type: str = None) -> dict:
        """
        Hitung statistik keberhasilan untuk aksi tertentu.
        
        Returns:
            dict dengan total, successes, failures, success_rate, avg_score
        """
        where_filter = {"action": action.value}
        if anomaly_type:
            where_filter = {
                "$and": [
                    {"action": action.value},
                    {"anomaly_type": anomaly_type}
                ]
            }
        
        try:
            results = self.collection.get(
                where=where_filter,
                include=["metadatas"]
            )
        except Exception:
            return {"total": 0, "success_rate": 0.0, "avg_score": 0.0}
        
        if not results or not results["metadatas"]:
            return {"total": 0, "success_rate": 0.0, "avg_score": 0.0}
        
        total = len(results["metadatas"])
        successes = sum(1 for m in results["metadatas"] if m.get("success"))
        scores = [m.get("score", 0) for m in results["metadatas"]]
        
        stats = {
            "total": total,
            "successes": successes,
            "failures": total - successes,
            "success_rate": round(successes / max(total, 1) * 100, 1),
            "avg_score": round(sum(scores) / max(len(scores), 1), 1)
        }
        
        logger.info(
            f"Stats for {action.value}"
            f"{f' on {anomaly_type}' if anomaly_type else ''}: "
            f"{stats['success_rate']}% success ({stats['successes']}/{stats['total']})"
        )
        
        return stats
    
    def format_experiences_for_prompt(self, experiences: list[dict]) -> list[str]:
        """
        Format pengalaman menjadi teks yang bisa dimasukkan ke prompt AI Agent.
        
        Returns:
            List of formatted experience strings
        """
        formatted = []
        for exp in experiences:
            meta = exp.get("metadata", {})
            status = "✅ BERHASIL" if meta.get("success") else "❌ GAGAL"
            text = (
                f"[{status}] {meta.get('action', '?')} pada "
                f"{meta.get('deployment', '?')} "
                f"(anomali: {meta.get('anomaly_type', '?')}, "
                f"skor: {meta.get('score', 0):.0f}/100) — "
                f"Similarity: {exp.get('similarity', 0):.0%}"
            )
            formatted.append(text)
        
        return formatted
    
    def _check_auto_runbook(self, decision: ActionDecision,
                             anomaly_type: str):
        """
        Cek apakah pola sukses sudah cukup untuk auto-generate runbook.
        
        Jika aksi X berhasil >= min_success_count kali untuk anomaly Y,
        maka buat file runbook baru secara otomatis.
        """
        stats = self.get_success_rate(decision.action, anomaly_type)
        
        if stats["successes"] >= self.min_success_count:
            # Cek apakah runbook sudah pernah di-generate
            runbook_name = f"auto_{anomaly_type}_{decision.action.value}"
            runbook_dir = Path(__file__).parent.parent / "knowledge_base" / "runbooks"
            runbook_path = runbook_dir / f"{runbook_name}.md"
            
            if not runbook_path.exists():
                self._generate_runbook(
                    runbook_path, decision, anomaly_type, stats
                )
    
    def _generate_runbook(self, path: Path, decision: ActionDecision,
                           anomaly_type: str, stats: dict):
        """Auto-generate runbook dari pola pengalaman sukses."""
        content = f"""# SOP: Auto-Generated — {anomaly_type.replace('_', ' ').title()}
## Runbook ID: AUTO-{anomaly_type.upper()}-{decision.action.value.upper()}
## Generated: {datetime.utcnow().isoformat()}
## Based on: {stats['successes']} successful experiences

---

## Auto-Generated dari Experience Memory

Runbook ini dibuat secara otomatis karena aksi `{decision.action.value}`
berhasil {stats['successes']} kali untuk anomali `{anomaly_type}`.

## Statistik
- Total percobaan: {stats['total']}
- Berhasil: {stats['successes']} ({stats['success_rate']}%)
- Rata-rata skor: {stats['avg_score']}/100

## Rekomendasi Aksi
1. **{decision.action.value}** — Aksi utama yang terbukti efektif
2. Monitor selama 30 detik setelah eksekusi
3. Verifikasi metrik kembali normal

## Catatan
- Runbook ini di-generate otomatis oleh XFSCI Experience Memory
- Perlu review manusia sebelum dijadikan SOP resmi
"""
        
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        
        logger.success(f"Auto-generated runbook: {path.name}")
    
    def _cleanup_oldest(self):
        """Hapus pengalaman terlama jika melebihi max_entries."""
        try:
            results = self.collection.get(include=["metadatas"])
            if not results or not results["ids"]:
                return
            
            # Sort by timestamp, hapus 10% terlama
            entries = list(zip(results["ids"], results["metadatas"]))
            entries.sort(key=lambda x: x[1].get("timestamp", ""))
            
            to_remove = len(entries) - self.max_entries
            if to_remove > 0:
                ids_to_remove = [e[0] for e in entries[:to_remove]]
                self.collection.delete(ids=ids_to_remove)
                logger.info(f"Cleaned up {to_remove} oldest experience entries")
        except Exception as e:
            logger.warning(f"Cleanup error: {e}")
