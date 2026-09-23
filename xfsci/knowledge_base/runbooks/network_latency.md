# SOP: Network Latency & Connectivity Issues
## Runbook ID: NET-001
## Severity: MEDIUM to HIGH
## Last Updated: 2026-09-19

---

## Gejala (Symptoms)
- Latency P99 meningkat drastis (> 500ms, normal < 200ms)
- Request timeout meningkat
- Inter-service communication lambat
- Packet loss atau connection reset

## Metrik Pemicu (Trigger Metrics)
- `latency_p99_ms > 500`
- `latency_p99_ms / latency_p50_ms > 5` (distribusi latency sangat lebar)
- `error_rate_percent > 3%` (kemungkinan timeout errors)

## Diagnosis
1. Network latency bisa disebabkan oleh:
   - Congestion pada node (terlalu banyak pod di satu node)
   - DNS resolution lambat
   - Service mesh (Istio sidecar) overloaded
   - Cross-node communication lambat (node di zone berbeda)
   - Upstream service lambat (cascading latency)
2. Cek apakah latency tinggi hanya ke satu service atau ke semua
3. Cek node placement: Apakah pod caller dan callee di node berbeda?

## Langkah Penanganan (Remediation Steps)

### Skenario A: Congestion di Satu Node
1. **Migrate Pod** ke node yang lebih senggang
2. **Verify**: Apakah latency membaik setelah migrasi?
3. Jika belum: Kemungkinan masalah bukan di node placement

### Skenario B: Upstream Service Lambat (Cascading)
1. **Rate Limit** trafik ke upstream yang lambat
2. **Scale Out** upstream service jika CPU/Memory-nya tinggi
3. Gunakan circuit breaker pattern jika tersedia
4. **Monitor** cascading effect ke service lain

### Skenario C: General Network Degradation
1. **Rate Limit** trafik masuk untuk mengurangi beban jaringan
2. **Scale Out** service yang terdampak paling berat
3. Jika latency > 2000ms dan tidak membaik → **Escalate**

## Kriteria Sukses
- `latency_p99_ms < 300ms`
- `error_rate_percent < 1%`
- `latency_p99_ms / latency_p50_ms < 3` (distribusi normal)

## Peringatan
- Network issue bisa jadi gejala dari masalah lain (CPU overload, memory pressure)
- Selalu cek root cause sebelum hanya mengobati gejala latency
- Jika SEMUA service terkena → Kemungkinan masalah infrastructure level
