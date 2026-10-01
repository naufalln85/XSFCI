# Kubernetes SRE Safety Guardrails for Antigravity Agent

Aturan Mutlak (Guardrails) yang dibaca otomatis oleh Antigravity di setiap siklus berpikir:

## 1. Aturan Larangan Mutlak (Forbidden Actions)
- ❌ **DILARANG** menghapus pod, deployment, atau resource apa pun di namespace `kube-system`, `kube-public`, `kube-node-lease`, atau `monitoring` (Prometheus, Grafana, Loki).
- ❌ **DILARANG** menjalankan command destruktif OS (`rm -rf`, `mkfs`, `dd`, `shutdown`, `reboot`).
- ❌ **DILARANG** menjalankan perintah penghapusan cluster (`kubectl delete namespace ...`, `kubectl delete deployment ...`, `kubectl delete pods --all`).

## 2. Aturan Prioritas & Keamanan Operasional (Operational Safety)
- ✅ **SELALU utamakan `scale_out` sebelum melakukan `restart`** pada kasus memory leak / CPU overload agar availability dan SLA tetap terjaga.
- ✅ **SELALU lakukan restart secara bergulir (rolling restart)** menggunakan `kubectl rollout restart deployment/<nama> -n <namespace>`. Jangan pernah mematikan semua replika sekaligus.
- ✅ **SELALU verifikasi kesehatan pod** setelah perbaikan dilakukan (tunggu settling time 15-30 detik dan pastikan status Ready 1/1).

## 3. Batas Sumber Daya (Resource Bounds)
- Target namespace kerja yang diizinkan: HANYA `demo` dan `xfsci-system`.
- Batas replika deployment: Minimal 1 replika, Maksimal 10 replika.
- Batas delta penambahan replika: Maksimal +3 replika per satu aksi perbaikan.
- Cooldown period: Beri jeda minimal 180 detik per pod sebelum tindakan berikutnya.
