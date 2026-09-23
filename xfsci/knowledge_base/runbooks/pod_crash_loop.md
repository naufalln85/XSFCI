# SOP: Pod Crash Loop Detection & Remediation
## Runbook ID: PCL-001
## Severity: HIGH to CRITICAL
## Last Updated: 2026-09-19

---

## Gejala (Symptoms)
- Pod restart count meningkat cepat (> 3 restart dalam 1 jam)
- Pod status berganti antara CrashLoopBackOff dan Running
- Error rate meningkat tajam
- Service intermittent (kadang jalan, kadang mati)

## Metrik Pemicu (Trigger Metrics)
- `pod_restarts_1h >= 3`
- `error_rate_percent > 10%`
- Pod age sangat muda (< 5 menit, terus restart)

## Diagnosis
1. Crash loop biasanya disebabkan oleh:
   - OOM Kill (memory limit terlampaui)
   - Failed health check (liveness/readiness probe gagal)
   - Dependency service down (database, cache, atau upstream)
   - Bad configuration atau missing environment variables
   - Bad deployment (image baru bermasalah)
2. Cek event log pod: `kubectl describe pod <pod-name>`
3. Cek apakah dependency service (Redis, DB) masih healthy

## Langkah Penanganan (Remediation Steps)

### Skenario A: OOM Kill (memory limit terlalu kecil)
1. **Scale Out** (+2 replicas) untuk menjaga availability
2. **Restart** pod yang crash dengan rolling restart
3. **Monitor**: Jika terus OOM → Escalate untuk naikkan memory limit

### Skenario B: Dependency Down
1. Cek status dependency service (DB, Redis, upstream)
2. Jika dependency down: **Escalate** (masalah bukan di pod ini)
3. Jika dependency recovery: Pod biasanya auto-recover

### Skenario C: Bad Deployment (crash setelah update image)
1. **Rate Limit** trafik untuk minimalisir impact
2. **Escalate** ke operator untuk rollback deployment
3. Jangan restart terus-menerus (akan tetap crash jika bug di kode)

### Skenario D: Intermittent Crash (jarang, tapi terjadi)
1. **Scale Out** (+1-2 replicas) untuk redundansi
2. **Monitor** ketat selama 30 menit
3. Jika crash berhenti → Kemungkinan transient issue, kembali normal

## Kriteria Sukses
- `pod_restarts_1h == 0` (tidak ada crash baru)
- `error_rate_percent < 1%`
- Pod age > 15 menit (stabil, tidak restart lagi)

## Peringatan
- JANGAN restart pod yang sedang crash loop tanpa investigasi penyebab
- Jika restart count > 5 dalam 30 menit → WAJIB escalate
- Crash loop karena bad image TIDAK bisa diperbaiki dengan restart
