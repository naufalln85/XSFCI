# ============================================================
# XFSCI Action Schema — Pydantic Structured Output
# ============================================================
# File ini mendefinisikan SEMUA format output yang boleh
# dihasilkan oleh AI Agent. Dengan Pydantic schema:
#   - AI Agent TIDAK BISA mengarang aksi di luar daftar
#   - Output SELALU dalam format JSON yang valid
#   - ZERO halusinasi pada level aksi
#
# Cara kerja:
#   Gemini Flash dipaksa mengembalikan JSON yang sesuai
#   dengan schema ini. Jika tidak sesuai → ditolak.
# ============================================================

from enum import Enum
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


# --- Action Enum (Daftar Aksi yang Diizinkan) ---
class ActionType(str, Enum):
    """
    Daftar lengkap aksi yang BOLEH diambil oleh AI Agent.
    AI Agent TIDAK BISA memilih aksi di luar enum ini.
    """
    NO_OP = "no_op"                # Tidak ada tindakan (kondisi aman)
    RESTART_POD = "restart_pod"    # Restart pod yang bermasalah
    SCALE_OUT = "scale_out"        # Tambah replika pod
    SCALE_IN = "scale_in"          # Kurangi replika pod
    RATE_LIMIT = "rate_limit"      # Batasi trafik masuk ke pod
    MIGRATE_POD = "migrate_pod"    # Pindahkan pod ke node lain
    ESCALATE = "escalate"          # Eskalasi ke operator manusia


class UrgencyLevel(str, Enum):
    """Level urgensi berdasarkan skor deterministik."""
    LOW = "low"            # Skor < 30: Aman, tidak perlu aksi
    MEDIUM = "medium"      # Skor 30-50: Perlu monitoring ketat
    HIGH = "high"          # Skor 50-70: Perlu tindakan
    CRITICAL = "critical"  # Skor > 85: Tindakan segera


class AnomalyType(str, Enum):
    """Jenis anomali yang terdeteksi oleh ML model."""
    NORMAL = "normal"
    MEMORY_LEAK = "memory_leak"
    CPU_OVERLOAD = "cpu_overload"
    POD_CRASH_LOOP = "pod_crash_loop"
    NETWORK_LATENCY = "network_latency"
    DISK_PRESSURE = "disk_pressure"
    UNKNOWN = "unknown"


# --- Input Schemas (Data yang masuk ke AI Agent) ---
class MLPrediction(BaseModel):
    """Output dari ML Prediction model (Layer 3)."""
    risk_score: float = Field(..., ge=0.0, le=1.0, description="Skor risiko 0.0-1.0")
    anomaly_type: AnomalyType = Field(..., description="Jenis anomali terdeteksi")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence level model")
    time_to_failure_minutes: Optional[float] = Field(None, description="Estimasi waktu crash")
    cascade_risk: List[str] = Field(default_factory=list, description="Pod yang terdampak")


class PandasMetrics(BaseModel):
    """Output dari Pandas Metric Processor — 100% fakta numerik."""
    timestamp: datetime
    target_pod: str
    target_node: str
    namespace: str = "demo"

    # Metrik CPU
    cpu_usage_avg_5m: float = Field(..., ge=0.0, description="Rata-rata CPU 5 menit (%)")
    cpu_usage_avg_15m: float = Field(..., ge=0.0, description="Rata-rata CPU 15 menit (%)")

    # Metrik Memory
    memory_usage_mb: float = Field(..., ge=0.0, description="Memory usage saat ini (MB)")
    memory_growth_rate_mb_per_min: float = Field(..., description="Pertumbuhan RAM per menit")
    memory_usage_percent: float = Field(..., ge=0.0, le=100.0, description="Memory %")

    # Metrik Pod
    pod_restarts_1h: int = Field(..., ge=0, description="Jumlah restart 1 jam terakhir")
    pod_age_minutes: float = Field(..., ge=0, description="Umur pod (menit)")
    current_replicas: int = Field(..., ge=0, description="Jumlah replika saat ini")

    # Metrik Network
    request_rate_rps: float = Field(..., ge=0.0, description="Request per second")
    error_rate_percent: float = Field(..., ge=0.0, description="Error rate (%)")
    latency_p50_ms: float = Field(..., ge=0.0, description="Latency P50 (ms)")
    latency_p99_ms: float = Field(..., ge=0.0, description="Latency P99 (ms)")

    # Metrik Disk
    disk_usage_percent: float = Field(0.0, ge=0.0, le=100.0, description="Disk usage %")


class SituationReport(BaseModel):
    """Laporan situasi lengkap yang dikirim ke AI Agent."""
    ml_prediction: MLPrediction
    pandas_metrics: PandasMetrics
    urgency_score: float = Field(..., ge=0.0, le=100.0, description="Skor urgensi 0-100")
    urgency_level: UrgencyLevel
    rag_runbook_content: str = Field("", description="Isi runbook SOP dari RAG")
    rag_similarity_score: float = Field(0.0, ge=0.0, le=1.0, description="Kemiripan RAG")
    past_experiences: List[str] = Field(default_factory=list, description="Pengalaman serupa")


# --- Output Schemas (Keputusan AI Agent) ---
class ActionParameters(BaseModel):
    """Parameter spesifik untuk setiap jenis aksi."""
    replicas_to_add: Optional[int] = Field(None, ge=1, le=10, description="Jumlah replika tambahan")
    replicas_to_remove: Optional[int] = Field(None, ge=1, le=5, description="Jumlah replika dikurangi")
    target_node: Optional[str] = Field(None, description="Node tujuan migrasi")
    rate_limit_rps: Optional[int] = Field(None, ge=1, description="Batas RPS")
    restart_strategy: Optional[str] = Field("rolling", description="rolling | immediate")


class ActionDecision(BaseModel):
    """
    Keputusan tunggal dari AI Agent.
    Format ini WAJIB diikuti — AI Agent tidak bisa mengembalikan
    format lain selain ini.
    """
    action: ActionType = Field(..., description="Aksi yang dipilih dari enum terkunci")
    target_deployment: str = Field(..., description="Nama deployment target (misal: cartservice)")
    target_namespace: str = Field("demo", description="Kubernetes namespace")
    parameters: ActionParameters = Field(default_factory=ActionParameters)
    confidence: float = Field(..., ge=0.0, le=1.0, description="Kepercayaan AI terhadap keputusan")
    reasoning: str = Field(..., min_length=10, description="Penjelasan logis keputusan")
    data_sources_used: List[str] = Field(
        default_factory=list,
        description="Sumber data yang digunakan (misal: pandas_metrics.cpu, runbook/ML-001)"
    )

    @field_validator("confidence")
    @classmethod
    def validate_escalation_confidence(cls, v, info):
        """Jika confidence terlalu rendah, paksa eskalasi."""
        # Validasi ini dilakukan di Guardrails, bukan di sini
        return v


class MultiStepAction(BaseModel):
    """Satu langkah dalam rencana multi-step."""
    step_number: int = Field(..., ge=1)
    action: ActionType
    target_deployment: str
    parameters: ActionParameters = Field(default_factory=ActionParameters)
    reason: str = Field(..., description="Alasan langkah ini")
    success_criteria: str = Field(..., description="Kriteria sukses sebelum lanjut ke step berikut")
    rollback_action: Optional[ActionType] = Field(None, description="Aksi rollback jika gagal")
    wait_seconds: int = Field(30, ge=5, le=300, description="Tunggu berapa detik sebelum evaluasi")


class MultiStepPlan(BaseModel):
    """
    Rencana multi-langkah dengan checkpoint di setiap tahap.
    Digunakan untuk skenario kompleks yang butuh 2-10 aksi berurutan.
    """
    plan_id: str = Field(..., description="ID unik rencana")
    total_steps: int = Field(..., ge=1, le=10, description="Total langkah")
    steps: List[MultiStepAction] = Field(..., min_length=1, max_length=10)
    overall_reasoning: str = Field(..., description="Penjelasan keseluruhan rencana")
    estimated_duration_seconds: int = Field(..., description="Estimasi durasi total")
    confidence: float = Field(..., ge=0.0, le=1.0)


class EscalationAlert(BaseModel):
    """
    Alert eskalasi ke operator manusia.
    Digunakan jika AI Agent tidak yakin atau masalah di luar kemampuan.
    """
    severity: UrgencyLevel
    target_deployment: str
    reason: str = Field(..., description="Mengapa AI perlu bantuan manusia")
    suggested_action: Optional[ActionType] = Field(None, description="Saran aksi jika ada")
    ml_prediction_summary: str = Field(..., description="Ringkasan prediksi ML")
    metrics_summary: str = Field(..., description="Ringkasan metrik Pandas")


class PostActionResult(BaseModel):
    """Hasil evaluasi setelah aksi dieksekusi."""
    action_taken: ActionType
    target_deployment: str
    success: bool = Field(..., description="Apakah aksi berhasil memperbaiki situasi")
    score: float = Field(..., ge=0.0, le=100.0, description="Skor keberhasilan 0-100")
    metrics_before: dict = Field(..., description="Metrik sebelum aksi")
    metrics_after: dict = Field(..., description="Metrik setelah aksi")
    improvement_summary: str = Field(..., description="Ringkasan perbaikan")
    duration_seconds: float = Field(..., description="Durasi dari aksi hingga evaluasi")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
