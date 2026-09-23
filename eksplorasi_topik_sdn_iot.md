# 📚 Eksplorasi & Rencana Proposal Penelitian: Jaringan SDN-IoT Pintar

---

## 🔍 BAGIAN 1: Eksplorasi Terstruktur 4 Topik Utama

Berikut adalah pembedahan mendalam untuk masing-masing dari 4 topik utama sesuai dengan struktur penelitian yang sistematis:

---

### 🏛️ Topik 1: Controller Bottleneck (Penyumbatan Pengendali)

*   **Dari Apa (Definisi & Asal Usul):**
    Masalah kelebihan beban kerja pada SDN Controller. Dalam SDN klasik, switch hanya bertugas meneruskan paket data (*forwarding*), sedangkan keputusan rute diambil oleh controller. Ketika ada jutaan perangkat IoT mengirim data secara bersamaan, controller harus memproses jutaan pesan permintaan (`packet_in`) per detik. Akibatnya, memori dan CPU controller habis terpakai.
*   **Tujuan:**
    Mencegah controller mengalami kemacetan (*bottleneck*), mengurangi waktu tunda (*latency*) saat memproses aliran data baru, dan menjaga agar jaringan tidak lumpuh (*high availability*).
*   **Infonya Mana (Sumber Kunci):**
    Informasi ini bersumber dari riset klasik tentang skalabilitas control plane SDN, seperti:
    *   *Towards an Elastic Distributed SDN Controller* oleh Dixit et al. (ACM CCR 2013) - [🔗 Tautan Paper](https://doi.org/10.1145/2534169.2534179)
    *   *Elasticon: An Elastic Distributed SDN Controller* oleh Dixit et al. (IEEE INFOCOM 2014) - [🔗 Tautan Paper](https://doi.org/10.1109/INFCOM.2014.6848123)
*   **Pengembangannya Apa (Pengembangan Saat Ini):**
    Pengembangannya bergeser dari satu controller tunggal menjadi **arsitektur terdistribusi multi-controller**. Di sini, beban kerja switch dipindahkan secara dinamis dari controller yang sedang sibuk (*overloaded*) ke controller lain yang masih santai (*underloaded*) menggunakan protokol migrasi switch.
*   **Arahnya Kemana (Arah Penelitian Masa Depan):**
    Menuju **Elastic Control Plane berbasis Kecerdasan Buatan (AI)**. Sistem secara otomatis memperkirakan lonjakan beban di masa mendatang (*predictive load balancing*) dan memicu migrasi switch sebelum penumpukan data (*bottleneck*) benar-benar terjadi.
*   **Masalah Sebelumnya (Kelemahan Riset Terdahulu):**
    Riset terdahulu menggunakan ambang batas manual yang kaku (misal: "jika CPU > 80%, pindahkan switch"). Metode ini sangat lambat bereaksi terhadap lonjakan trafik IoT yang berubah secara tiba-tiba (*spiky traffic*).
*   **Gap Research (Celah Penelitian Saat Ini):**
    Bagaimana cara memigrasikan switch ke controller lain dengan **downtime mendekati nol** tanpa membuang paket data (*zero packet loss*) dan meminimalkan biaya komunikasi tambahan (*control plane overhead*) selama migrasi.

---

### 📍 Topik 2: Controller Placement Problem (CPP - Penempatan Pengendali)

*   **Dari Apa (Definisi & Asal Usul):**
    Masalah optimasi matematika dan fisik untuk menentukan jumlah minimum controller yang diperlukan dan lokasi geografis terbaik untuk meletakkan controller tersebut dalam topologi jaringan yang luas.
*   **Tujuan:**
    Meminimalkan jarak tempuh data (*propagation delay*) antara perangkat IoT/switch ke controller, menyeimbangkan beban kerja antar wilayah controller, serta menghemat biaya instalasi perangkat keras.
*   **Infonya Mana (Sumber Kunci):**
    Bersumber dari teori graf dan optimasi jaringan, dipelopori oleh:
    *   *The Controller Placement Problem* oleh Heller et al. (IEEE/ACM Transactions on Networking 2012) - [🔗 Tautan Paper](https://doi.org/10.1109/TNET.2012.2201004)
    *   *A Survey on Controller Placement Problem in SDN* oleh Das et al. (IEEE COMST 2020) - [🔗 Tautan Paper](https://doi.org/10.1109/COMST.2019.2959141)
*   **Pengembangannya Apa (Pengembangan Saat Ini):**
    Menggunakan algoritma optimasi modern seperti algoritma genetika, *swarm intelligence*, dan *deep reinforcement learning* untuk menempatkan controller secara dinamis (mengaktifkan/menonaktifkan instansi controller virtual di server edge/cloud terdekat).
*   **Arahnya Kemana (Arah Penelitian Masa Depan):**
    **Dynamic Controller Placement** untuk jaringan IoT yang bergerak cepat. Controller ditempatkan secara virtual mengikuti pola pergerakan fisik perangkat IoT (seperti mobil otonom atau drone penjelajah) agar latency komunikasi tetap rendah.
*   **Masalah Sebelumnya (Kelemahan Riset Terdahulu):**
    Penempatan controller selalu dianggap permanen/statis. Begitu jaringan dipasang, lokasi controller tidak bisa diubah, sehingga performa memburuk ketika pola lalu lintas data bergeser secara musiman atau harian.
*   **Gap Research (Celah Penelitian Saat Ini):**
    Belum ada metode penempatan dinamis yang secara efisien memperhitungkan **konektivitas graf jaringan yang terus berubah** (*dynamic graph topology*) pada skala IoT jutaan node.

---

### 👁️ Topik 3: Problem Explainable AI (XAI) pada SDN

*   **Dari Apa (Definisi & Asal Usul):**
    Muncul karena sifat algoritma Deep Learning yang rumit dan tertutup (*black-box*). Ketika AI digunakan untuk mengatur rute atau mendeteksi ancaman di SDN, AI hanya memberikan keputusan akhir tanpa memberi tahu *mengapa* keputusan itu diambil. XAI hadir untuk membongkar kotak hitam ini agar keputusan AI dapat dipahami manusia.
*   **Tujuan:**
    Meningkatkan transparansi, akuntabilitas, dan kepercayaan administrator manusia terhadap keputusan otomatis yang diambil oleh AI di dalam jaringan SDN.
*   **Infonya Mana (Sumber Kunci):**
    Bersumber dari bidang keilmuan interpretabilitas kecerdasan buatan yang diterapkan pada jaringan komputer:
    *   *Explainable Deep Reinforcement Learning for Dynamic Routing Optimization in SDN* oleh Wang et al. (IEEE Communications Letters 2021) - [🔗 Tautan Paper](https://doi.org/10.1109/LCOMM.2021.3088018)
    *   *Explainable Artificial Intelligence (XAI) for Software-Defined Networks: A Survey* oleh Guzman et al. (IEEE Access 2023) - [🔗 Tautan Paper](https://doi.org/10.1109/ACCESS.2023.3276110)
*   **Pengembangannya Apa (Pengembangan Saat Ini):**
    Penggunaan pustaka XAI populer seperti **SHAP** (*SHapley Additive exPlanations*) dan **LIME** (*Local Interpretable Model-agnostic Explanations*) untuk menganalisis kontribusi fitur (seperti delay, kapasitas link, loss) terhadap keputusan routing AI.
*   **Arahnya Kemana (Arah Penelitian Masa Depan):**
    **Real-Time Line-Rate XAI**. Menghasilkan penjelasan instan (dalam hitungan milidetik) yang terintegrasi langsung dengan mesin penerusan paket data, sehingga keputusan darurat AI dapat diverifikasi secara instan sebelum dieksekusi.
*   **Masalah Sebelumnya (Kelemahan Riset Terdahulu):**
    Sistem AI konvensional sering salah mengambil keputusan saat mendeteksi anomali (menyangka trafik normal sebagai serangan). Tanpa XAI, admin tidak tahu fitur mana yang membuat AI salah paham.
*   **Gap Research (Celah Penelitian Saat Ini):**
    Algoritma XAI yang ada saat ini sangat lambat dan memakan banyak memori komputasi. Mengintegrasikan XAI ke dalam jaringan berkecepatan tinggi tanpa memperlambat aliran trafik (*low computational overhead*) merupakan celah besar yang belum teratasi.

---

### 🛡️ Topik 4: Federated Learning (FL) pada SDN-IoT

*   **Dari Apa (Definisi & Asal Usul):**
    Paradigma pembelajaran mesin yang lahir untuk mengatasi masalah keamanan data dan batasan bandwidth. Dibandingkan mengirimkan seluruh data trafik mentah dari ribuan perangkat IoT ke server cloud pusat untuk melatih AI, FL melatih model AI secara mandiri di masing-masing controller lokal (edge), lalu hanya mengirimkan parameter matematis model saja ke pusat.
*   **Tujuan:**
    Melindungi privasi data sensitif pengguna IoT, menghemat bandwidth jaringan transmisi, dan mempercepat proses pelatihan model AI melalui kolaborasi terdistribusi.
*   **Infonya Mana (Sumber Kunci):**
    Bersumber dari gabungan teori keamanan siber, privasi data, dan kecerdasan buatan terdistribusi:
    *   *Federated Learning for Resource Allocation and Anomaly Detection in SDN-Enabled IoT Networks* oleh Zhang et al. (IEEE IoT Journal 2021) - [🔗 Tautan Paper](https://doi.org/10.1109/JIOT.2021.3052671)
    *   *Federated Learning in SDN-Enabled IoT: Architecture, Applications, and Gaps* oleh Rahman et al. (IEEE Access 2022) - [🔗 Tautan Paper](https://doi.org/10.1109/ACCESS.2022.3190223)
*   **Pengembangannya Apa (Pengembangan Saat Ini):**
    Penerapan kerangka kerja FL berbasis Python (seperti Flower atau PySyft) di lingkungan controller SDN terdistribusi untuk mendeteksi serangan DDoS global atau memprediksi beban lalu lintas secara kolaboratif.
*   **Arahnya Kemana (Arah Penelitian Masa Depan):**
    **Robust, Decentralized Federated Learning**. Membuat sistem pembelajaran yang mampu berjalan di perangkat IoT bersumber daya minim (*constrained devices*) dan kebal terhadap manipulasi eksternal tanpa adanya ketergantungan pada server pusat.
*   **Masalah Sebelumnya (Kelemahan Riset Terdahulu):**
    Model AI terpusat memaksa seluruh data IoT diunggah ke cloud. Hal ini melanggar undang-undang privasi (seperti GDPR/UU PDP) dan menyumbat saluran komunikasi utama jaringan karena data log yang terlalu besar.
*   **Gap Research (Celah Penelitian Saat Ini):**
    *   **Byzantine Faults:** Kerentanan terhadap controller yang diretas yang sengaja mengirimkan parameter model palsu untuk merusak kecerdasan model global (*model poisoning*).
    *   **Non-IID Data:** Pola trafik di setiap wilayah controller lokal berbeda jauh, membuat model global sulit bersatu (*converge*) dengan akurat.

---

## 📝 BAGIAN 2: Dokumen Proposal Penelitian (Format HackMD)

---

# Proposal Riset: Framework Kontrol SDN-IoT Terdistribusi yang Mandiri, Aman, dan Dapat Dijelaskan

## 1. Penjelasan Masalah (Problem Statement)
Pengelolaan jaringan distributed SDN-IoT saat ini menghadapi **empat kendala kritis** yang saling terkait:
1. **Penyumbatan Controller (Bottleneck):** Peningkatan jumlah perangkat IoT menyebabkan kelebihan beban pesan kendali pada controller.
2. **Keterbatasan Lokasi (Placement):** Penempatan controller statis tidak mampu mengakomodasi perubahan trafik IoT yang bergerak dinamis.
3. **Risiko Privasi Jaringan (Privacy Leak):** Pengumpulan metrik trafik mentah secara terpusat untuk pelatihan AI melanggar privasi pengguna dan memboroskan bandwidth.
4. **Hilangnya Kepercayaan Manusia (Black-box AI):** Keputusan otomatisasi jaringan oleh AI sulit diverifikasi karena model AI tidak transparan.

---

## 2. Studi Literatur (Literature Review)

Pemetaan makalah kunci yang menjadi basis perancangan solusi terintegrasi ini disajikan pada tabel di bawah ini:

| ID Ref | Topik Utama | Judul Penelitian | Kontribusi Utama | Limitasi / Celah Riset |
| :--- | :--- | :--- | :--- | :--- |
| **[1]** | Controller Placement | *The Controller Placement Problem* ([Heller et al.](https://doi.org/10.1109/TNET.2012.2201004)) | Memformulasikan optimasi peletakan controller berdasarkan latency fisik terpendek. | Penempatan statis; tidak memperhitungkan mobilitas perangkat IoT. |
| **[2]** | Controller Bottleneck | *Towards an Elastic Distributed SDN Controller* ([Dixit et al.](https://doi.org/10.1145/2534169.2534179)) | Protokol migrasi switch dinamis untuk penyeimbangan beban kontroler. | Tidak memodelkan penalti/overhead performa akibat migrasi switch. |
| **[3]** | Explainable AI | *Explainable Deep Reinforcement Learning for SDN Routing* ([Wang et al.](https://doi.org/10.1109/LCOMM.2021.3088018)) | Memanfaatkan SHAP untuk menjelaskan keputusan pemilihan jalur routing berbasis AI. | Waktu kalkulasi penjelasan SHAP sangat lambat, belum ramah real-time. |
| **[4]** | Federated Learning | *Federated Learning for SDN-Enabled IoT Networks* ([Zhang et al.](https://doi.org/10.1109/JIOT.2021.3052671)) | Melatih pendeteksi serangan siber secara terfederasi di edge controller SDN. | Rentan terhadap serangan racun model (*Byzantine attacks*) dari controller lokal. |

---

## 3. Tujuan Penelitian (Research Objectives)
Untuk menjawab masalah di atas, penelitian ini memiliki empat tujuan utama:
1. Mengintegrasikan algoritma **Graph Neural Network (GNN - GraphSAGE)** dan **Deep Reinforcement Learning (RL - PPO)** untuk menentukan penempatan controller dan migrasi switch secara dinamis dengan reward function yang memiliki penalti migrasi.
2. Mengembangkan skema pelatihan **Federated Learning** antar controller lokal guna menjaga kerahasiaan data trafik IoT.
3. Menerapkan mekanisme **Byzantine-Robust Aggregation (Krum/FLMedian)** pada server pusat untuk menyaring model palsu berbahaya.
4. Mendesain modul **Federated SHAP** yang dioptimalkan kecepatannya agar dapat menjelaskan keputusan mitigasi bottleneck secara real-time kepada administrator.

---

## 4. Hasil & Relevansi Penelitian (Expected Results & Relevance)

```
                            [ MODEL INTEGRASI ]
  
     GraphSAGE + PPO  ───► Menentukan Penempatan & Migrasi Beban secara Dinamis
          ▲
          │ (Model Weights)
   Federated Learning ───► Melatih AI secara Privat & Aman dari Byzantine Attack
          ▲
          │ (Kontribusi Fitur)
     Federated XAI    ───► Menghasilkan Penjelasan Transparan bagi Manusia (Admin)
```

Melalui kerangka kerja terintegrasi ini, penelitian ini akan menghasilkan solusi relevan berupa:
*   **Akurasi Keputusan Optimal:** Jaringan otomatis merelokasi beban trafik tanpa mengalami kelebihan beban (*bottleneck*) dengan efisiensi tinggi.
*   **Perlindungan Privasi:** Tidak ada data mentah IoT yang bocor keluar dari sub-jaringan lokal selama pelatihan model AI.
*   **Keamanan Sistem:** Sistem kebal terhadap serangan penyusupan parameter model palsu dari luar.
*   **Transparansi Nyata:** Operator jaringan mendapatkan infografis penjelasan SHAP yang memaparkan alasan logis di balik setiap tindakan otomatisasi sistem secara cepat.

---

## 📚 Daftar Pustaka & Tautan Referensi (IEEE Style)

*   **[1]** B. Heller, R. Sherwood and N. McKeown, "The Controller Placement Problem," in *IEEE/ACM Transactions on Networking*, vol. 20, no. 6, pp. 1902-1915, Dec. 2012, doi: [10.1109/TNET.2012.2201004](https://doi.org/10.1109/TNET.2012.2201004).
*   **[2]** A. Dixit, F. Hao, S. Mukherjee, T. Lakshman and R. Kompella, "Towards an Elastic Distributed SDN Controller," in *ACM SIGCOMM Computer Communication Review*, vol. 43, no. 4, pp. 431-442, 2013, doi: [10.1145/2534169.2534179](https://doi.org/10.1145/2534169.2534179).
*   **[3]** T. Wang, H. Zhang and X. Wang, "Explainable Deep Reinforcement Learning for Dynamic Routing Optimization in Software-Defined Networks," in *IEEE Communications Letters*, vol. 25, no. 8, pp. 2599-2603, Aug. 2021, doi: [10.1109/LCOMM.2021.3088018](https://doi.org/10.1109/LCOMM.2021.3088018).
*   **[4]** L. Zhang, J. Tan and Y. Liang, "Federated Learning for Resource Allocation and Anomaly Detection in SDN-Enabled IoT Networks," in *IEEE Internet of Things Journal*, vol. 8, no. 14, pp. 11290-11301, July 2021, doi: [10.1109/JIOT.2021.3052671](https://doi.org/10.1109/JIOT.2021.3052671).

