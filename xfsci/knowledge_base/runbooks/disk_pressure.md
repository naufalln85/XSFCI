# SOP: Disk Pressure Detection & Remediation
## Runbook ID: DSK-001
## Severity: MEDIUM to HIGH
## Last Updated: 2026-09-19

---

## Gejala (Symptoms)
- Disk usage mendekati atau melebihi 85%
- Node mendapat taint `DiskPressure` dari kubelet
- Pod mulai di-evict oleh Kubernetes
- Write operations menjadi lambat atau gagal

## Metrik Pemicu (Trigger Metrics)
- `disk_usage_percent > 85%`
- Kubernetes node condition: `DiskPressure = True`

## Diagnosis
1. Disk pressure disebabkan oleh:
   - Log files yang terlalu besar (tanpa rotasi)
   - Container images yang menumpuk (unused images)
   - PersistentVolume yang penuh
   - Temporary files dari aplikasi yang tidak di-cleanup
2. Cek komponen mana yang paling banyak makan disk

## Langkah Penanganan (Remediation Steps)

### Skenario A: Log Files Membengkak
1. **Escalate** ke operator untuk cleanup log files
2. Pertimbangkan konfigurasi log rotation
3. Pastikan Promtail/Fluentd mengirim log ke Loki (bukan disimpan lokal)

### Skenario B: Container Images Menumpuk
1. **Escalate** ke operator untuk jalankan `docker system prune`
2. Atau `crictl rmi --prune` jika menggunakan containerd

### Skenario C: PV Penuh
1. **Migrate Pod** ke node lain yang masih ada space
2. **Escalate** untuk resize PVC atau tambah storage

## Kriteria Sukses
- `disk_usage_percent < 75%`
- Node tidak lagi bertaint `DiskPressure`
- Pod tidak di-evict

## Peringatan
- Disk pressure bisa menyebabkan Kubernetes menghapus pod secara otomatis
- JANGAN abaikan — ini bisa cascade menjadi cluster-wide issue
- Kebanyakan solusi disk pressure membutuhkan intervensi manusia
