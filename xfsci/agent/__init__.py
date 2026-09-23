# ============================================================
# XFSCI AI Agent Module
# ============================================================
# Modul utama AI Agent yang menggantikan pure RL dengan
# arsitektur hybrid: Gemini Flash + RAG + Pandas + ML Scoring.
#
# Komponen:
#   - action_schema.py      → Pydantic structured output (anti-halusinasi)
#   - pandas_processor.py   → Analisis metrik deterministik
#   - scoring_engine.py     → Urgency scoring (pengganti RL reward)
#   - decision_agent.py     → AI Agent + Gemini Flash + RAG
#   - sandbox.py            → Dry-run sandbox untuk masalah baru
#   - precision_optimizer.py → ML Regression auto-tuning
#   - multi_step_planner.py → Plan + Checkpoint + Rollback
#   - experience_memory.py  → Belajar dari pengalaman (pengganti replay buffer)
#   - orchestrator.py       → Pipeline utama yang menyatukan semua
# ============================================================

from loguru import logger

logger.info("XFSCI Agent module loaded")
