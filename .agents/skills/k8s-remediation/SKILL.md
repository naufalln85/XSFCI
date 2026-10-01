---
name: k8s-remediation
description: Autonomous Kubernetes incident diagnosis and remediation skill for XFSCI SRE Agent. Activates when GNN detects cluster anomalies and root-cause candidates to inspect, remediate via safe scripts in sandbox, and verify recovery.
---

# Kubernetes Self-Healing & Remediation Skill (`k8s-remediation`)

Skill operasional untuk mendiagnosis dan memulihkan insiden anomali microservice di cluster Kubernetes XFSCI secara autonomous di dalam Sandbox.

---

## 🎯 Pemicu (Trigger)
Skill ini aktif ketika **GNN Inference Engine** mendeteksi anomali pada cluster dan mengunci **Tersangka Utama (Primary Root Cause)** serta ranking Top-3 RCA.

> **PENTING (Anti-Halusinasi 0%)**: 
> Target penyelidikan dan mitigasi WAJIB berpusat pada pod tersangka utama yang dibuktikan oleh GNN topologi (misal: jika `redis-cart` memicu memory leak yang menyebabkan `cartservice` timeout dan `frontend` 500 error, target perbaikan adalah `redis-cart`, BUKAN `frontend`).

---

## 📋 Prosedur Standar Teknis (SOP 4 Langkah)

### Langkah 1: Investigasi Telemetri & Log Pod
Ambil status pod dan 50 baris log terakhir dari pod tersangka yang dikunci oleh GNN:
```bash
./scripts/check_health.sh demo <deployment_name>
kubectl logs -n demo -l app=<deployment_name> --tail=50 --all-containers=true
```

### Langkah 2: Cocokkan Pola Gangguan (Error Signature Matching)
Analisis temuan dari log dan metrik fisik:
- **`OOMKilled` / Memory growth > 5MB/min**: Terkonfirmasi Memory Leak (`MEMORY_LEAK`).
- **CPU Throttling / CPU > 80%**: Terkonfirmasi CPU Overload (`CPU_OVERLOAD`).
- **`CrashLoopBackOff` / Exit code 1/137/139**: Terkonfirmasi Crash Loop (`POD_CRASH_LOOP`).
- **`Connection Refused` / `Context Deadline Exceeded`**: Terkonfirmasi Latensi / Dependency Bottleneck (`NETWORK_LATENCY`).

### Langkah 3: Eksekusi Mitigasi yang Sesuai SOP (Di Dalam Sandbox)
Pilih aksi mitigasi non-destruktif sesuai runbook SOP:
1. **Kasus Memory Leak**:
   - Skalakan replika terlebih dahulu untuk mengamankan kapasitas:
     ```bash
     ./scripts/safe_scale.sh demo <deployment_name> +2
     ```
   - Jalankan rolling restart bertahap:
     ```bash
     ./scripts/safe_restart.sh demo <deployment_name>
     ```
2. **Kasus CPU Overload**:
   - Jika traffic tinggi: Skalakan replika (`./scripts/safe_scale.sh demo <deployment_name> +2`).
   - Jika stuck thread: Jalankan rolling restart (`./scripts/safe_restart.sh demo <deployment_name>`).
3. **Kasus Pod Crash Loop**:
   - Periksa konfigurasi/event, lalu picu rolling restart untuk pod instance baru:
     ```bash
     ./scripts/safe_restart.sh demo <deployment_name>
     ```

### Langkah 4: Pantau & Verifikasi Pemulihan (Post-Healing Check)
Pantau metrik Prometheus dan status pod selama 15-30 detik untuk memastikan cluster sembuh total:
```bash
./scripts/check_health.sh demo <deployment_name>
```
**Kriteria Sukses**:
- Seluruh replika pod berstatus `Running` dan `Ready (1/1)`.
- Delta restart bernilai 0.
- Error rate HTTP kembali turun di bawah 1%.
