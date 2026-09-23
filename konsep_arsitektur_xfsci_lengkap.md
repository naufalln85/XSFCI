# 📘 DOKUMEN SPESIFIKASI KONSEP & ARSITEKTUR LENGKAP XFSCI
**Explainable Federated Self-Healing Cloud Infrastructure**  
*Arsitektur AI Agent Otonom Hybrid Berpola Reinforcement Learning (RL) pada Kubernetes Multi-Node*

---

## 📌 DAFTAR ISI
1. [Ringkasan Eksekutif & Problem Statement](#1-ringkasan-eksekutif--problem-statement)
2. [Topologi Infrastruktur Fisik & Virtual (1 Server · 4 VM · 1 K8s Cluster)](#2-topologi-infrastruktur-fisik--virtual)
3. [Arsitektur Sistem End-to-End (8 Layer)](#3-arsitektur-sistem-end-to-end-8-layer)
4. [Bedah Mendalam: AI Agent Berpola Reinforcement Learning](#4-bedah-mendalam-ai-agent-berpola-reinforcement-learning)
5. [Peran Krusial Federated Learning & Byzantine Defense](#5-peran-krusial-federated-learning--byzantine-defense)
6. [Studi Kasus Insiden Nyata: Flash Sale + Memory Leak](#6-studi-kasus-insiden-nyata)
7. [Matriks Komparasi: Pure RL (PPO/DQN) vs AI Agent XFSCI](#7-matriks-komparasi-pure-rl-vs-ai-agent-xfsci)
8. [Panduan File Codebase & Diagram Draw.io](#8-panduan-file-codebase--diagram-drawio)

---

## 1. Ringkasan Eksekutif & Problem Statement

### 1.1 Latar Belakang Masalah
Pada infrastruktur *cloud computing* skala besar (khususnya arsitektur *microservices* di Kubernetes), insiden sistemik seperti *resource saturation*, *cascading failure*, *memory leak*, dan *traffic spike* dapat menyebabkan *downtime* fatal. 
Pendekatan tradisional memiliki kelemahan mendasar:
1. **Rule-Based Script (`if-else` / HPA sederhana):** Sangat kaku, tidak mampu menangani korelasi insiden multivariat, dan rapuh terhadap anomali baru.
2. **Pure Reinforcement Learning (PPO / DQN / Q-Learning):** Membutuhkan jutaan kali *trial-and-error*. Di server produksi nyata, proses eksplorasi acak RL **sangat berbahaya** (misal: RL mencoba mematikan pod database atau salah menghitung alokasi).

### 1.2 Solusi XFSCI
**XFSCI** memadukan keunggulan adaptif **Reinforcement Learning** ke dalam arsitektur **AI Agent Modern** yang deterministik, aman, dan berlandaskan SOP industri:
* **Persepsi & Deteksi Dini:** Menggunakan **GNN** (topologi & *blast radius*) dan **LSTM** (prediksi kegagalan 15 menit ke depan).
* **Kalkulasi State Deterministik:** **Pandas** menghitung fakta metrik 100% presisi matematis (menghilangkan halusinasi angka LLM).
* **Knowledge Prior:** **RAG (ChromaDB)** menyuplai 6 SOP SRE standar industri.
* **Otak Pengambil Keputusan (Policy):** **Google Gemini 2.0 Flash** (`temp=0.1`) menyusun strategi pemulihan bertahap (*multi-step plan*).
* **Keamanan Eksekusi:** Ruang aksi dikunci hanya pada **7 Pydantic Enum** dengan **5 Lapis Guardrails** dan **Dry-Run Sandbox**.
* **Pembelajaran Terdistribusi:** **Federated Learning (Flower Framework)** dengan agregasi **Krum/FedMedian** untuk transfer pengetahuan antar-node tanpa memindahkan data mentah (hemat bandwidth & aman dari *data poisoning*).

---

## 2. Topologi Infrastruktur Fisik & Virtual

Sistem dijalankan pada **1 Server Fisik** (misal: Proxmox VE 8.x) yang dialokasikan menjadi **4 Virtual Machine (VM)** membentuk **1 Klaster Kubernetes Multi-Node**:

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│  PHYSICAL SERVER (Proxmox VE / KVM) — Total: 16 vCPU · 16 GB RAM · 160 GB Disk           │
│                                                                                          │
│  ┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐  ┌────────────────┐│
│  │   VM 1 (Master)   │  │ VM 2 (Worker 1)   │  │ VM 3 (Worker 2)   │  │ VM 4 (Worker 3)││
│  │   Control-Plane   │  │ Region: Jakarta   │  │ Region: Bandung   │  │ Region: Sby    ││
│  │                   │  │ Beban Kerja:      │  │ Beban Kerja:      │  │ Beban Kerja:   ││
│  │ • K8s API, etcd   │  │ • Normal Load     │  │ • Traffic Spike   │  │ • Chaos Target ││
│  │ • AI Agent Core   │  │ • Steady State    │  │ • Latency & Leak  │  │ • Byzantine FL ││
│  │ • Monitoring Pods │  │                   │  │                   │  │                ││
│  │ 4c · 4GB · 40GB   │  │ 4c · 4GB · 40GB   │  │ 4c · 4GB · 40GB   │  │ 4c · 4GB · 40GB││
│  └───────────────────┘  └───────────────────┘  └───────────────────┘  └────────────────┘│
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Konfigurasi Labeling Node (Kunci Kompatibilitas Sistem)
Setiap worker diberi label khusus yang menjadi target *nodeSelector* manifest dan agen pemantau:
* **VM 1 (Master):** `xfsci-role=control-plane`
* **VM 2 (Worker Jakarta):** `xfsci-role=worker`, `xfsci-site=jakarta`, `topology.kubernetes.io/zone=id-jakarta`
* **VM 3 (Worker Bandung):** `xfsci-role=worker`, `xfsci-site=bandung`, `topology.kubernetes.io/zone=id-bandung`
* **VM 4 (Worker Surabaya):** `xfsci-role=worker`, `xfsci-site=surabaya`, `topology.kubernetes.io/zone=id-surabaya`

### 2.2 Namespace Wajib
* `monitoring`: Prometheus, Grafana, Loki, Promtail.
* `demo`: 11 Microservices *Google Online Boutique* (Frontend, Cart, Payment, Order, Currency, dll).
* `xfsci-system`: Orchestrator Agent, FastAPI REST API, Dashboard Next.js, Flower FL Server.
* `chaos-testing`: Pod injeksi gangguan (`cpu-stress`, `memory-leak`, `network-latency`, `pod-crash`).

### 2.3 Port Mapping Terstandarisasi
* **Prometheus UI:** Port `9090` (NodePort `30090`)
* **Grafana Dashboard:** Port `3000` (NodePort `30030`, user: `admin`, pass: `xfsci-admin-2026`)
* **Loki Log API:** Port `3100` (NodePort `30100`)
* **XFSCI Backend API:** Port `8000` (NodePort `30800`)
* **XFSCI Next.js Dashboard:** Port `3001` (NodePort `30801`)
* **Flower FL Server:** Port `8080` (NodePort `30880`)

---

## 3. Arsitektur Sistem End-to-End (8 Layer)

Sistem beroperasi dalam siklus tertutup (*Closed-Loop Control*):

```
[Cluster Workload] ──(Metrik 5s)──> ① Monitoring (Prometheus/Loki)
                                            │
                                            ▼ (Timeseries & Graphs)
                                    ② GNN & ③ LSTM/Transformer
                                            │
                                            ▼ (Root Cause & Risk Prediction)
                                    ④ AI AGENT (Gemini+Pandas+RAG)
                                            │
                                            ▼ (Plan JSON)
                                    🛡️ 5-Layer Guardrails & Sandbox
                                            │
                                            ▼ (Approved Execution)
                                    ⑤ Self-Healing Actuator (Kubectl)
                                            │
                                            ▼
[Cluster Pulih 💚] <────────────────────────┘
        │
        ├──> ⑥ Explainable AI (SHAP & NL Reasoning)
        ├──> ⑦ Federated Learning (FL Flower + Krum Defense)
        └──> ⑧ Dashboard & Human-in-the-Loop (Next.js)
```

| Layer | Nama Layer | Teknologi | Fungsi Utama |
| :---: | :--- | :--- | :--- |
| **1** | **Monitoring Layer** | Prometheus, Loki, OpenTelemetry | Mengambil telemetri tiap 5 detik (CPU, RAM, RPS, P95/P99 Latency, 5xx Error, Restarts). |
| **2** | **Cloud Intelligence** | PyTorch Geometric (GNN / GAT) | Memetakan graf dependensi microservices, menghitung *blast radius*, dan mengisolasi *root cause*. |
| **3** | **Failure Prediction** | LSTM + Time-Series Transformer | Meramalkan kegagalan infrastruktur (misal OOMKilled) hingga 15 menit sebelum terjadi. |
| **4** | **Cognitive AI Agent** | Gemini 2.0 Flash + Pandas + RAG | Mensintesis fakta, membaca SOP, menimbang *trade-off*, dan merumuskan rencana aksi pemulihan. |
| **5** | **Self-Healing Engine** | K8s API + 5 Guardrails + Sandbox | Memvalidasi batas keamanan, mengeksekusi multi-step rollback watchdog, dan mengubah status klaster. |
| **6** | **Explainability (XAI)**| SHAP + LIME + Natural Language CoT | Menerjemahkan alasan keputusan AI ke Bahasa Indonesia & menghitung bobot fitur metrik penentu. |
| **7** | **Federated Learning** | Flower Framework (`flwr`) | Melatih model prediksi lokal di tiap worker tanpa memindahkan data mentah ke master. |
| **8** | **Security / Byzantine**| Krum / FedMedian Aggregation | Mendeteksi dan membuang bobot model yang teracuni akibat serangan anomali dari Worker Surabaya. |

---

## 4. Bedah Mendalam: AI Agent Berpola Reinforcement Learning

Inovasi utama XFSCI adalah mengadopsi struktur matematis RL ke dalam arsitektur AI modern:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               AI AGENT INTERNAL ANATOMY                                │
│                                                                                        │
│  [Prometheus] ──> [ 🐼 PANDAS ] ──(Fakta Angka Eksak)──> [ 🤖 GEMINI FLASH ]           │
│                        │                                      ▲     ▲     ▲            │
│                        ▼                                      │     │     │            │
│                 [ 📐 SCORING ] ──(Skor Urgensi 0-100)─────────┘     │     │            │
│                                                                     │     │            │
│                 [ 📚 RAG SOP ] ──(Pedoman Prosedural)───────────────┘     │            │
│                                                                           │            │
│                 [ 🧪 EXP MEMORY ] ──(Pengalaman Sukses Masa Lalu)─────────┘            │
│                        ▲                                                               │
│                        │                                                               ▼
│                        │ (Simpan Pengalaman)                               [ 🔒 7 AKSI PYDANTIC ]
│                        │                                                               │
│                        │                                                               ▼
│                 [ 📐 POST-SCORE ] <── [ ⚡ KUBECTL ] <── [ 🛡️ GUARDRAILS & SANDBOX ]    │
│                   (RL REWARD)           (ACTUATOR)                                     │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Modul-Modul Internal Agen:
1. **🐼 Pandas State Engine (`RL: State`):**
   * **Masalah:** LLM sangat buruk dalam berhitung dan rentan halusinasi angka.
   * **Solusi:** Pandas mengekstrak kueri PromQL dan menghitung laju pertumbuhan RAM ($\Delta\text{RAM} = +12.4\text{ MB/min}$), persentil P95/P99, dan error delta secara deterministik murni.
2. **📐 Deterministic Urgency Scorer (`RL: Value/Reward Predictor`):**
   * Menghitung skor keparahan masalah ($0-100$) secara matematis terbuka tanpa *black-box*:
     $$\text{Score} = w_1 \cdot \text{RAM}_{\text{usage}} + w_2 \cdot \Delta\text{Latency} + w_3 \cdot \text{ErrorRate}$$
3. **📚 RAG SOP Knowledge Base (`RL: Policy Prior`):**
   * Menyimpan 6 SOP industri di **ChromaDB**:
     * *SOP-01:* Pod CrashLoopBackOff Remediation
     * *SOP-02:* Traffic Spike Rate Limiting & HPA
     * *SOP-03:* Concurrency Spike + Memory Degradation Mitigation
     * *SOP-04:* Worker Node Partition & Pod Eviction
     * *SOP-05:* Database Connection Pool Exhaustion
     * *SOP-06:* Canary Deployment Instant Rollback
4. **🤖 Google Gemini 2.0 Flash (`RL: Policy Network`):**
   * Parameter: `temperature = 0.1` (sangat deterministik).
   * Menerima gabungan data: (Angka Pandas + Urgensi Scoring + Pedoman SOP + Pengalaman Sukses Lama).
   * Mengeluarkan rencana aksi bertahap berformat JSON terstruktur (*Chain-of-Thought*).
5. **🔒 7 Aksi Terkunci Pydantic Enum (`RL: Action Space`):**
   * AI dibatasi ketat hanya boleh memilih: `no_op`, `restart_pod`, `scale_out`, `scale_in`, `rate_limit`, `migrate`, `escalate`.
   * **Anti-Halusinasi:** AI secara sintaksis mustahil mengeksekusi perintah di luar daftar ini.
6. **🛡️ 5-Layer Safety Guardrails:**
   * *Layer 1 (Cooldown):* Mencegah modifikasi pod yang sama $<10$ menit (anti-*flapping*).
   * *Layer 2 (Confidence Gate):* Wajib memiliki keyakinan $\ge 0.80$ (jika $<0.80$, eskalasi ke manusia).
   * *Layer 3 (Blacklist):* Dilarang menyentuh pod sistem vital (`etcd`, `kube-system`, CoreDNS).
   * *Layer 4 (Circuit Breaker):* Maksimal 3 kali remedi berturut-turut dlm 15 menit.
   * *Layer 5 (Blast Radius Limiter):* Maksimal penambahan 10 replika per deployment.
7. **🎯 ML Precision Optimizer (XGBoost):**
   * Memecahkan kelemahan aksi diskrit: Menghitung secara eksak jumlah replika yang dibutuhkan berdasarkan kurva beban ($f(\text{RPS}, \text{Latency Target})$), misal: tepat **5 replika**, bukan tebakan 3 atau 8.
8. **🧪 Isolated Dry-Run Sandbox:**
   * Lingkungan virtual non-destruktif untuk menguji skenario *zero-day* yang belum ada di SOP.
9. **⚡ Multi-Step Actuator & Watchdog Rollback:**
   * Membuat *checkpoint* snapshot, mengeksekusi aksi sekuensial via `kubectl`, dan memantau status pod. Jika kondisi memburuk, otomatis *rollback*.
10. **📐 Post-Action Evaluator (`RL: Reward Function`):**
    * Mengukur perubahan metrik 25 detik pasca-aksi. Menghitung reward score ($0-100$).
11. **🧪 Experience Memory (`RL: Replay Buffer`):**
    * Menyimpan tuple transisi $\langle \text{State}, \text{Action}, \text{Reward}, \text{Next State} \rangle$ ke **ChromaDB**.
    * Pengalaman tersimpan **permanen** (tidak hilang saat komputer mati) dan dapat dicari kembali menggunakan *cosine similarity*.

---

## 5. Peran Krusial Federated Learning & Byzantine Defense

Pertanyaan yang sering muncul: *"Jika pengambil keputusan adalah Gemini Flash, lalu apa fungsi Federated Learning (FL)?"*

### 5.1 Apa yang Dilatih oleh FL?
FL **TIDAK melatih Gemini**, melainkan melatih **Model Sensorik / Prediksi Lokal (LSTM Anomaly Predictor & XGBoost Regressor)** di masing-masing worker node:
* Worker 1 (Jakarta) melatih pola trafik normal.
* Worker 2 (Bandung) melatih pola lonjakan trafik (*spike*) dan degradasi memori.
* Worker 3 (Surabaya) melatih pola kegagalan acak (*chaos*).

### 5.2 Mengapa Menggunakan FL?
1. **Efisiensi Bandwidth & Privasi:**
   Metrik dan log mentah berukuran puluhan gigabyte tetap berada di worker lokal. Hanya **bobot model kecil (~beberapa KB)** yang dikirim ke master.
2. **Cross-Region Knowledge Sharing:**
   Worker Jakarta yang hanya mengalami beban normal bisa **mewarisi kepintaran prediksi spike** dari Worker Bandung tanpa harus mengalami server jebol terlebih dahulu.
3. **Pertahanan Byzantine (Krum / FedMedian):**
   Jika Worker 3 (Surabaya) mengalami *chaos* ekstrem atau diserang data palsu (*data poisoning*), algoritma **Krum** di master akan mengukur jarak vektor bobot model, mendeteksinya sebagai pencilan (*outlier*), dan **mengeliminasinya**, sehingga model global di master tetap bersih dan akurat.

---

## 6. Studi Kasus Insiden Nyata: Flash Sale + Memory Leak

* **Target:** Worker 2 (Bandung)
* **Kondisi:** RPS melonjak 400% (50 $\rightarrow$ 285 req/s), dibarengi kebocoran memori pada `payment-service` (RAM 94%, mendekati OOM 1GB).

```
T+00:00 [SENSING]
  Prometheus mendeteksi: RAM 94%, RPS 285, P95 Latency 850ms, Error 18.2%.

T+00:05 [DIAGNOSIS ML]
  LSTM (FL model) meramal: "payment-service akan OOMKilled dalam 7 menit."
  GNN menghitung: Root cause = payment-service, blast radius = order-service.

T+00:08 [STATE & KNOWLEDGE]
  Pandas menghitung delta RAM = +12.4 MB/min (Eksak).
  Scoring Engine mengeluarkan nilai urgensi = 88/100 (CRITICAL).
  RAG menarik SOP-03: "Spike + Memory Leak: Scale-out dulu, baru restart pod lama."

T+00:10 [COGNITIVE REASONING - GEMINI FLASH]
  Gemini bernalar: "Restart langsung saat traffic tinggi akan mematikan transaksi.
  Maka strategi: Step 1 scale 2->5 replika. Step 2 rolling restart pod lama."
  Output: JSON terstruktur (Confidence 0.94).

T+00:12 [GUARDRAILS SAFETY CHECK]
  Cooldown: OK (terakhir diubah 12 menit lalu).
  Confidence: 0.94 > 0.80 (Lolos).
  Action Whitelist: [scale_out, restart_pod] (Lolos).

T+00:15 [ACTUATION DI K8S]
  Kubectl scale payment-service --replicas=5.
  Setelah pod baru Ready, kubectl rollout restart payment-service (rolling).

T+00:35 [REWARD & LEARNING]
  Evaluasi metrik pasca-aksi: Latensi 850ms -> 65ms, Error 18.2% -> 0.05%.
  Reward Score dihitung = 92 / 100 (Sukses).
  Tuple <S, A, R=92, S'> disimpan ke ChromaDB Experience Memory.
  XAI mengirim penjelasan ke Dashboard SRE.
```

---

## 7. Matriks Komparasi: Pure RL vs AI Agent XFSCI

| Parameter | Pure RL Tradisional (PPO / DQN) | AI Agent XFSCI (Implementasi Kami) | Alasan XFSCI Lebih Unggul di Industri |
| :--- | :--- | :--- | :--- |
| **State ($S$)** | Vektor angka mentah rentan noise | **Pandas DataFrame** dari PromQL | **100% Presisi Matematis:** Dihitung fungsi eksak Python, bebas aproksimasi float. |
| **Policy ($\pi$)** | Black-box Neural Net, butuh jutaan *trial-error* destruktif | **Gemini 2.0 Flash + RAG SOP** | **Zero Cold-Start:** Langsung cerdas sejak hari pertama, bertindak patuh pada SOP SRE. |
| **Action ($\mathcal{A}$)** | Aksi kontinu/acak rawan merusak server saat eksplorasi | **7 Aksi Terkunci (Pydantic Enum)** + Guardrails | **Zero Risk:** AI mustahil mengarang perintah destruktif di luar izin whitelist. |
| **Reward ($R$)** | Reward function sintetis rawan *reward-hacking* | **Post-Action Score Deterministik** (Fakta Riil) | **Auditable:** Dihitung dari penurunan nyata error rate, latensi, dan kestabilan RAM. |
| **Memory** | Replay buffer di RAM volatil (hilang saat restart) | **ChromaDB Vector Store** (Persisten) | **Permanent Knowledge:** Pengalaman tersimpan permanen dan dapat digenerasi jadi SOP baru. |

---

## 8. Panduan File Codebase & Diagram Draw.io

### 8.1 File Diagram Arsitektur (.drawio) di Root Folder
Untuk keperluan presentasi atau review visual, buka file `.drawio` berikut menggunakan VS Code Draw.io extension atau [app.diagrams.net](https://app.diagrams.net):
1. **`xfsci_alur_kerja_sederhana.drawio`**: Diagram siklus tertutup (*Closed-Loop*) yang sangat bersih dan mudah dipresentasikan dalam 1 menit.
2. **`xfsci_ai_agent_simple.drawio`**: Diagram AI Agent yang menjelaskan setiap kotak: **UNTUK APA** dan **KEMANA** alirannya.
3. **`xfsci_ai_agent_architecture_full.drawio`**: Diagram anatomi mendalam 5 Zona AI Agent lengkap dengan matriks perbandingan RL.
4. **`xfsci_arsitektur_presentasi_dosen.drawio`**: Diagram arsitektur lengkap 4 VM, alur langkah ① s/d ⑦, dan tabel komparasi.
5. **`xfsci_architecture_detailed.drawio`**: Diagram teknis terlengkap (210 elemen) yang memetakan seluruh port, layer, dan node.

### 8.2 Peta Modul Codebase Python (`xfsci/`)
* **`infrastructure/kind-config.yaml`**: Konfigurasi klaster 4 node (1 Master + 3 Worker) beserta port mappings.
* **`configs/config.yaml`**: Konfigurasi global terpusat seluruh sistem XFSCI.
* **`agent/orchestrator.py`**: Pipeline orkestrator utama yang menyatukan Pandas, Scoring, RAG, Gemini, dan Guardrails.
* **`agent/pandas_processor.py`**: Modul kalkulator metrik eksak (State RL).
* **`agent/scoring_engine.py`**: Modul penghitung skor urgensi dan reward pasca-aksi.
* **`agent/decision_agent.py`**: Integrasi Google Gemini 2.0 Flash dengan skema Pydantic terstruktur.
* **`agent/action_schema.py`**: Definisi 7 Pydantic Enum aksi terkunci dan struktur data situasi.
* **`agent/experience_memory.py`**: ChromaDB Vector Store untuk replay buffer episosik.
* **`agent/sandbox.py`**: Modul dry-run sandbox untuk pengujian terisolasi.
* **`agent/precision_optimizer.py`**: XGBoost regressor untuk fine-tuning kapasitas replika.
* **`scripts/setup_cluster.sh`**: Script otomatisasi pembuatan klaster.
* **`scripts/deploy_monitoring.sh`**: Script otomatisasi instalasi Prometheus, Loki, Grafana, dan Online Boutique.

---
*Dokumen ini disusun sebagai acuan formal desain sistem XFSCI. Tidak ada dependensi kode atau konfigurasi program yang diubah.*
