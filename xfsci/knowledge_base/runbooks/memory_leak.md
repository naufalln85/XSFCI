# SOP: Memory Leak Detection & Remediation
## Runbook ID: ML-001
## Severity: HIGH to CRITICAL
## Last Updated: 2026-09-19

---

## Gejala (Symptoms)
- Memory usage meningkat secara konsisten tanpa turun (growth rate > 5 MB/menit)
- Pod tidak pernah melepas memori meskipun request menurun
- Grafik memory usage terlihat seperti "tangga naik" atau "garis miring ke atas"
- Pod mendekati memory limit dan berpotensi OOM Kill

## Metrik Pemicu (Trigger Metrics)
- `memory_growth_rate_mb_per_min > 5.0`
- `memory_usage_percent > 80%`
- `pod_restarts_1h >= 2` (kemungkinan sudah OOM sebelumnya)

## Diagnosis
1. Memory leak biasanya disebabkan oleh:
   - Koneksi database/HTTP yang tidak ditutup
   - Cache yang tumbuh tanpa batas (unbounded cache)
   - Goroutine/thread leak
   - Buffer yang tidak di-flush
2. Cek apakah semua pod dari deployment yang sama terkena (shared code issue) atau hanya satu pod (instance-specific)

## Langkah Penanganan (Remediation Steps)

### Skenario A: Memory Usage < 85% (Masih ada waktu)
1. **Scale Out** deployment (+2 replicas) untuk menjaga availability
2. **Restart** pod yang terkena memory leak secara rolling
3. **Verify**: Cek apakah pod baru memiliki memory usage yang stabil
4. **Monitor**: Pantau 15 menit, pastikan growth rate kembali normal

### Skenario B: Memory Usage > 85% (Darurat)
1. **Rate Limit** trafik masuk untuk mengurangi beban
2. **Scale Out** deployment (+3 replicas) 
3. **Restart** pod lama secara rolling
4. **Remove Rate Limit** setelah pod baru stabil
5. **Verify**: Pastikan memory usage kembali normal

### Skenario C: Pod sudah OOM Crash Loop
1. **Scale Out** terlebih dahulu untuk menjaga SLA
2. Jika pod terus crash: **Escalate** ke operator untuk investigasi kode
3. Pertimbangkan rollback ke versi deployment sebelumnya

## Kriteria Sukses
- `memory_growth_rate_mb_per_min <= 0` (tidak ada leak)
- `error_rate_percent < 1%`
- `latency_p99_ms` kembali ke baseline

## Peringatan
- JANGAN restart semua pod sekaligus (bisa downtime total)
- JANGAN scale beyond max_replicas tanpa approval manusia
- Jika memory leak terjadi di SEMUA pod baru → ini bug kode, ESCALATE
