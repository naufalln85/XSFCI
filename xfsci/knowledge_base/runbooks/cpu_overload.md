# SOP: CPU Overload Detection & Remediation
## Runbook ID: CPU-001
## Severity: MEDIUM to CRITICAL
## Last Updated: 2026-09-19

---

## Gejala (Symptoms)
- CPU usage rata-rata > 80% selama > 5 menit
- Request latency meningkat tajam (P99 > 500ms)
- Pod menunjukkan CPU throttling
- Request queue menumpuk

## Metrik Pemicu (Trigger Metrics)
- `cpu_usage_avg_5m > 80%`
- `latency_p99_ms > 500`
- `request_rate_rps` meningkat signifikan dibanding baseline

## Diagnosis
1. CPU overload bisa disebabkan oleh:
   - Traffic spike (peningkatan request mendadak)
   - Proses heavy computation (batch job, ETL)
   - Infinite loop atau deadlock dalam kode
   - Noisy neighbor (pod lain di node yang sama)
2. Cek apakah node-level CPU juga tinggi atau hanya pod tertentu

## Langkah Penanganan (Remediation Steps)

### Skenario A: Traffic Spike (request_rate naik > 50% dari baseline)
1. **Scale Out** deployment (+2-3 replicas) untuk distribusi beban
2. **Monitor** 5 menit: Apakah latency membaik?
3. Jika sudah stabil dan traffic turun: **Scale In** kembali

### Skenario B: CPU Tinggi tanpa Traffic Spike (kemungkinan bug)
1. **Rate Limit** trafik masuk untuk stabilisasi
2. **Restart** pod yang CPU-nya tertinggi (kemungkinan stuck process)
3. **Verify**: Apakah pod baru memiliki CPU normal?
4. Jika CPU tetap tinggi di pod baru → **Escalate** (bug kode)

### Skenario C: Node-Level CPU Exhaustion
1. **Migrate Pod** ke node lain yang lebih senggang
2. **Scale Out** untuk distribusi beban
3. Jika semua node penuh → **Escalate** untuk penambahan resource

## Kriteria Sukses
- `cpu_usage_avg_5m < 70%`
- `latency_p99_ms < 300ms`
- `error_rate_percent < 1%`

## Peringatan
- JANGAN langsung restart jika traffic memang tinggi (lebih baik scale out dulu)
- Perhatikan resource request/limit pod agar scheduler K8s efektif
