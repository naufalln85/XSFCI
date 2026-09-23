# SOP: General Escalation to Human Operator
## Runbook ID: ESC-001
## Severity: Varies
## Last Updated: 2026-09-19

---

## Kapan AI Agent Harus Eskalasi ke Manusia

AI Agent WAJIB memilih aksi `escalate` dalam kondisi berikut:

### 1. Confidence Level Rendah
- AI Agent confidence < 85%
- RAG tidak menemukan runbook yang cocok (similarity < 70%)
- Anomaly type = "unknown"

### 2. Kegagalan Berulang
- Aksi yang sama sudah dilakukan 2x pada pod yang sama dalam 30 menit terakhir tapi masalah tidak membaik
- Circuit breaker aktif (3+ kegagalan berturut-turut)
- Post-action score < 30 (aksi gagal memperbaiki keadaan)

### 3. Masalah di Luar Kemampuan AI
- Bug pada kode aplikasi (crash loop karena bad image)
- Masalah infrastruktur level (node hardware failure)
- Security breach atau serangan DDoS
- Database corruption
- Certificate expiration
- Masalah DNS cluster-wide

### 4. Dampak Luas (Blast Radius Tinggi)
- Lebih dari 3 deployment terdampak secara bersamaan
- Masalah terjadi di lebih dari 1 node
- Error rate > 50% secara cluster-wide

## Format Eskalasi
Ketika melakukan eskalasi, AI Agent harus menyertakan:
1. **Ringkasan situasi** — Apa yang terjadi
2. **Data metrik** — Angka-angka kunci dari Pandas
3. **Prediksi ML** — Risk score dan anomaly type
4. **Aksi yang sudah dicoba** — Apa yang AI sudah lakukan (jika ada)
5. **Saran aksi** — Rekomendasi jika AI punya insight

## Channel Eskalasi
- Kirim alert ke Slack/Discord/Email
- Log ke database untuk audit trail
- Tampilkan di dashboard XFSCI
- Jika severity CRITICAL: Kirim notifikasi ke semua admin

## Peringatan
- Eskalasi BUKAN kegagalan — ini adalah fitur keamanan
- Lebih baik eskalasi 10x terlalu banyak daripada 1x salah eksekusi
- Setiap eskalasi yang berhasil diselesaikan manusia → Masuk ke Experience Memory → Jadi runbook baru untuk masa depan
