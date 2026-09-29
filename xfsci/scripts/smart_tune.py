#!/usr/bin/env python3
"""
============================================================
XFSCI Smart Tuner — Bayesian Optimization (Optuna TPE)
============================================================
Script pelatihan cerdas yang BELAJAR dari setiap percobaan
untuk mengarahkan pencarian ke kombinasi hyperparameter
terbaik secara MATEMATIS.

Perbedaan fundamental vs Grid Search (auto_train.sh):
  Grid Search  : Coba resep A, B, C, D, E yang sudah ditulis.
                  Tidak belajar apa-apa dari hasil sebelumnya.
  Bayesian Opt : Setelah coba resep A → hasilnya jelek.
                  Algoritma TPE menganalisis: "Ah, LR tinggi +
                  batch kecil = jelek. Coba sebaliknya."
                  Setiap trial berikutnya SEMAKIN PINTAR.

Cara kerja Optuna TPE (Tree-structured Parzen Estimator):
  1. Kumpulkan semua percobaan sebelumnya (parameter → skor)
  2. Bagi jadi 2 distribusi: l(x) = percobaan bagus, g(x) = jelek
  3. Pilih parameter baru yang memaksimalkan l(x)/g(x)
  4. Semakin banyak trial → semakin akurat prediksinya

Target:
  - Cluster Anomaly Accuracy > 99%
  - Top-3 RCA (A@3)         > 98%
  - Fault-F1 (Macro)        > 80%

Cara pakai:
  # Install Optuna dulu (1x):
  pip install optuna

  # Jalankan:
  python scripts/smart_tune.py --n-trials 30

  # Background (recommended):
  nohup python scripts/smart_tune.py --n-trials 50 > smart_tune.log 2>&1 &
============================================================
"""

import sys
import os
import json
import time
import shutil
import argparse
from pathlib import Path
from datetime import datetime

# Pastikan root proyek ada di sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

import numpy as np
import torch
from loguru import logger

# Import Optuna
try:
    import optuna
    from optuna.samplers import TPESampler
    from optuna.pruners import MedianPruner
except ImportError:
    logger.error("Optuna belum terinstall! Jalankan: pip install optuna")
    sys.exit(1)

# Import pipeline XFSCI
from models.gnn.graph_dataset import build_graph_dataloaders
from models.gnn.gnn_model import DualHeadGATv2
from models.gnn.train_gnn import GNNTrainer

WEIGHTS_DIR = PROJECT_DIR / "models" / "gnn" / "weights"
WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# TARGET METRIK
# ============================================================
TARGET_CLUSTER_ACC = 0.99
TARGET_RCA_A3 = 0.98
TARGET_FAULT_F1 = 0.80

# ============================================================
# GLOBAL BEST TRACKER
# ============================================================
GLOBAL_BEST = {
    "score": -1.0,
    "trial": -1,
    "metrics": {},
    "params": {},
}


def run_data_pipeline(samples: int, noise: float):
    """Jalankan synthetic generation + feature engineering."""
    import subprocess

    logger.info(f"  [Data Pipeline] Generating synthetic data: {samples}/class, noise={noise:.2f}")
    result = subprocess.run(
        [sys.executable, "data/preprocessors/synthetic_generator.py",
         "--target-per-class", str(samples),
         "--noise", str(noise)],
        cwd=str(PROJECT_DIR),
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        logger.error(f"  Synthetic generation gagal:\n{result.stderr[-500:]}")
        raise RuntimeError("Synthetic generation failed")

    logger.info("  [Data Pipeline] Running feature engineering...")
    result = subprocess.run(
        [sys.executable, "data/preprocessors/feature_engineer.py"],
        cwd=str(PROJECT_DIR),
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        logger.error(f"  Feature engineering gagal:\n{result.stderr[-500:]}")
        raise RuntimeError("Feature engineering failed")

    logger.info("  [Data Pipeline] ✅ Data pipeline selesai")


def objective(trial: optuna.Trial) -> float:
    """
    Fungsi objektif Optuna.
    
    Optuna memanggil fungsi ini untuk setiap trial.
    TPE sampler menggunakan RIWAYAT trial sebelumnya
    untuk memilih parameter yang lebih menjanjikan.
    """
    global GLOBAL_BEST

    trial_start = time.time()

    # ============================================================
    # HYPERPARAMETER SEARCH SPACE
    # ============================================================
    # Optuna TPE akan memilih nilai dari ruang ini secara CERDAS,
    # bukan acak. Setiap kali trial selesai, distribusi di-update.

    # --- Data Generation ---
    samples = trial.suggest_int("samples_per_class", 4000, 12000, step=1000)
    noise = trial.suggest_float("noise_factor", 0.03, 0.20)

    # --- Training Hyperparameters ---
    epochs = trial.suggest_int("epochs", 60, 200, step=10)
    lr = trial.suggest_float("lr", 5e-4, 5e-3, log=True)
    batch_size = trial.suggest_categorical("batch_size", [16, 24, 32, 48, 64])

    # --- Loss Function Tuning (KUNCI untuk Fault-F1) ---
    # fault_weight_cap: batas atas bobot kelas fault dalam loss function
    # Semakin tinggi → model semakin "berani" memprediksi fault
    # Terlalu tinggi → false alarm meledak
    fault_weight_cap = trial.suggest_float("fault_weight_cap", 2.0, 10.0)
    label_smoothing = trial.suggest_float("label_smoothing", 0.01, 0.12)
    graph_loss_weight = trial.suggest_float("graph_loss_weight", 0.1, 0.6)

    # --- Model Architecture ---
    hidden_dim = trial.suggest_categorical("hidden_dim", [32, 48, 64])
    dropout = trial.suggest_float("dropout", 0.1, 0.4)

    logger.info(f"\n{'='*65}")
    logger.info(f"  TRIAL {trial.number + 1} | Optuna TPE Bayesian Optimization")
    logger.info(f"{'='*65}")
    logger.info(f"  samples={samples} | noise={noise:.3f}")
    logger.info(f"  epochs={epochs} | lr={lr:.5f} | batch={batch_size}")
    logger.info(f"  fault_weight_cap={fault_weight_cap:.1f} | smoothing={label_smoothing:.3f}")
    logger.info(f"  graph_weight={graph_loss_weight:.2f} | hidden={hidden_dim} | dropout={dropout:.2f}")
    logger.info(f"{'='*65}")

    # ── Step 1: Data Pipeline ──
    try:
        run_data_pipeline(samples, noise)
    except Exception as e:
        logger.error(f"  Data pipeline error: {e}")
        return 0.0  # Trial gagal, skor 0

    # ── Step 2: Build DataLoaders ──
    try:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        train_loader, val_loader, test_loader, info = build_graph_dataloaders(
            batch_size=batch_size
        )
    except Exception as e:
        logger.error(f"  DataLoader error: {e}")
        return 0.0

    # ── Step 3: Build & Train Model ──
    try:
        model = DualHeadGATv2(
            in_channels=info["num_features"],
            hidden_dim=hidden_dim,
            num_heads=4,
            num_classes=info["num_classes"],
            dropout=dropout
        )

        trainer = GNNTrainer(
            model=model,
            train_loader=train_loader,
            val_loader=val_loader,
            test_loader=test_loader,
            device=device,
            lr=lr,
            label_smoothing=label_smoothing,
            graph_loss_weight=graph_loss_weight,
            fault_weight_cap=fault_weight_cap,
            epochs=epochs
        )

        metrics = trainer.run_training(epochs=epochs, patience=0)
    except Exception as e:
        logger.error(f"  Training error: {e}")
        import traceback
        traceback.print_exc()
        return 0.0

    # ── Step 4: Evaluate ──
    cluster_acc = metrics.get("cluster_anomaly_accuracy", 0)
    rca_a3 = metrics.get("top3_rca_accuracy", 0)
    fault_f1 = metrics.get("fault_macro_f1", 0)
    node_acc = metrics.get("node_accuracy", 0)

    # Skor komposit (Fault-F1 dominan karena itu yang perlu dinaikkan)
    composite = (cluster_acc * 0.20) + (rca_a3 * 0.30) + (fault_f1 * 0.50)

    trial_time = time.time() - trial_start

    logger.info(f"\n  📊 HASIL TRIAL {trial.number + 1}:")
    logger.info(f"     Cluster Acc : {cluster_acc*100:.2f}%")
    logger.info(f"     RCA A@3     : {rca_a3*100:.2f}%")
    logger.info(f"     Fault-F1    : {fault_f1*100:.2f}%")
    logger.info(f"     Node Acc    : {node_acc*100:.2f}%")
    logger.info(f"     Composite   : {composite*100:.2f}%")
    logger.info(f"     Waktu       : {trial_time:.0f}s")

    # ── Track Global Best ──
    if composite > GLOBAL_BEST["score"]:
        GLOBAL_BEST["score"] = composite
        GLOBAL_BEST["trial"] = trial.number + 1
        GLOBAL_BEST["metrics"] = {
            "cluster_anomaly_accuracy": cluster_acc,
            "top3_rca_accuracy": rca_a3,
            "fault_macro_f1": fault_f1,
            "node_accuracy": node_acc,
            "composite_score": composite,
        }
        GLOBAL_BEST["params"] = trial.params

        # Backup model terbaik
        src = WEIGHTS_DIR / "gnn_best.pt"
        dst = WEIGHTS_DIR / "gnn_optuna_best.pt"
        if src.exists():
            shutil.copy2(src, dst)
            logger.success(f"  ⭐ NEW GLOBAL BEST! Score: {composite*100:.2f}% → Saved to {dst.name}")

        # Simpan metrik terbaik
        best_metrics_path = WEIGHTS_DIR / "optuna_best_metrics.json"
        with open(best_metrics_path, "w") as f:
            json.dump({
                "metrics": GLOBAL_BEST["metrics"],
                "params": GLOBAL_BEST["params"],
                "trial": GLOBAL_BEST["trial"],
                "timestamp": datetime.utcnow().isoformat(),
            }, f, indent=2)

    # ── Cek apakah SEMUA target tercapai ──
    all_targets_met = (
        cluster_acc >= TARGET_CLUSTER_ACC and
        rca_a3 >= TARGET_RCA_A3 and
        fault_f1 >= TARGET_FAULT_F1
    )

    if all_targets_met:
        logger.success(f"\n  🎉 SEMUA TARGET TERCAPAI pada Trial {trial.number + 1}!")
        logger.success(f"     Cluster Acc : {cluster_acc*100:.2f}% (target >99%)")
        logger.success(f"     RCA A@3     : {rca_a3*100:.2f}% (target >98%)")
        logger.success(f"     Fault-F1    : {fault_f1*100:.2f}% (target >80%)")
        # Optuna akan berhenti otomatis jika callback mendeteksi ini

    return composite


class TargetReachedCallback:
    """
    Callback yang menghentikan optimasi ketika semua target tercapai.
    Ini menghemat waktu — tidak perlu lanjut trial kalau sudah sukses.
    """
    def __call__(self, study: optuna.Study, trial: optuna.trial.FrozenTrial):
        if trial.value is None:
            return

        # Cek apakah metrik best trial sudah memenuhi target
        if GLOBAL_BEST["metrics"]:
            m = GLOBAL_BEST["metrics"]
            if (m.get("cluster_anomaly_accuracy", 0) >= TARGET_CLUSTER_ACC and
                m.get("top3_rca_accuracy", 0) >= TARGET_RCA_A3 and
                m.get("fault_macro_f1", 0) >= TARGET_FAULT_F1):
                logger.success("🏁 Callback: SEMUA TARGET TERCAPAI! Menghentikan optimasi.")
                study.stop()


def print_study_summary(study: optuna.Study):
    """Cetak ringkasan hasil optimasi."""
    logger.info("\n" + "=" * 65)
    logger.info("  📊 RINGKASAN OPTIMASI BAYESIAN (OPTUNA TPE)")
    logger.info("=" * 65)

    logger.info(f"\n  Total trials       : {len(study.trials)}")
    logger.info(f"  Best trial         : #{study.best_trial.number + 1}")
    logger.info(f"  Best composite     : {study.best_value * 100:.2f}%")

    if GLOBAL_BEST["metrics"]:
        m = GLOBAL_BEST["metrics"]
        logger.info(f"\n  📈 Metrik Terbaik:")
        logger.info(f"     Cluster Acc  : {m['cluster_anomaly_accuracy']*100:.2f}%")
        logger.info(f"     RCA A@3      : {m['top3_rca_accuracy']*100:.2f}%")
        logger.info(f"     Fault-F1     : {m['fault_macro_f1']*100:.2f}%")
        logger.info(f"     Node Acc     : {m['node_accuracy']*100:.2f}%")

    logger.info(f"\n  🔧 Hyperparameters Optimal:")
    for k, v in study.best_params.items():
        logger.info(f"     {k:25s}: {v}")

    logger.info(f"\n  📁 File Output:")
    logger.info(f"     Model    : models/gnn/weights/gnn_optuna_best.pt")
    logger.info(f"     Metrik   : models/gnn/weights/optuna_best_metrics.json")
    logger.info(f"     Study DB : models/gnn/weights/optuna_study.db")
    logger.info("=" * 65)

    # Tampilkan evolusi skor per trial
    logger.info("\n  📉 Evolusi Skor per Trial:")
    best_so_far = -1.0
    for t in study.trials:
        if t.value is not None:
            if t.value > best_so_far:
                best_so_far = t.value
                marker = " ⭐"
            else:
                marker = ""
            logger.info(f"     Trial {t.number+1:3d} : {t.value*100:6.2f}%{marker}")
        else:
            logger.info(f"     Trial {t.number+1:3d} : FAILED")


def main():
    parser = argparse.ArgumentParser(
        description="XFSCI Smart Tuner — Bayesian Optimization (Optuna TPE)"
    )
    parser.add_argument(
        "--n-trials", type=int, default=30,
        help="Jumlah trial optimasi (default: 30, lebih banyak = lebih akurat)"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Lanjutkan optimasi dari study sebelumnya (jika ada)"
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed untuk reproducibility"
    )
    args = parser.parse_args()

    # ============================================================
    # SETUP OPTUNA STUDY
    # ============================================================
    # Storage: SQLite lokal agar study bisa di-resume kapan saja
    db_path = WEIGHTS_DIR / "optuna_study.db"
    storage_url = f"sqlite:///{db_path}"

    study_name = "xfsci_gnn_fault_f1_optimization"

    logger.info("")
    logger.info("╔══════════════════════════════════════════════════════════════╗")
    logger.info("║     XFSCI Smart Tuner — Bayesian Optimization (Optuna)      ║")
    logger.info("║                                                              ║")
    logger.info("║  Algoritma: TPE (Tree-structured Parzen Estimator)           ║")
    logger.info("║  Setiap trial BELAJAR dari trial sebelumnya                  ║")
    logger.info("║  → Semakin banyak trial, semakin PINTAR pemilihan paramater  ║")
    logger.info("╚══════════════════════════════════════════════════════════════╝")
    logger.info("")
    logger.info(f"  Jumlah trial : {args.n_trials}")
    logger.info(f"  Resume       : {'Ya' if args.resume else 'Tidak'}")
    logger.info(f"  Seed         : {args.seed}")
    logger.info(f"  Study DB     : {db_path}")
    logger.info(f"  Target       : Cluster>99% | RCA>98% | Fault-F1>80%")
    logger.info("")

    # TPE Sampler: Algoritma Bayesian utama
    # n_startup_trials=5: 5 trial pertama random (explorasi),
    # setelah itu mulai belajar dari riwayat (eksploitasi).
    sampler = TPESampler(
        seed=args.seed,
        n_startup_trials=5,
        multivariate=True,  # Pertimbangkan korelasi antar parameter
    )

    # Buat atau muat study
    study = optuna.create_study(
        study_name=study_name,
        storage=storage_url,
        sampler=sampler,
        direction="maximize",  # Maksimalkan skor komposit
        load_if_exists=args.resume,
    )

    logger.info(f"  Study dimuat: {len(study.trials)} trial sebelumnya")
    logger.info("")

    # ============================================================
    # JALANKAN OPTIMASI
    # ============================================================
    start_time = time.time()

    study.optimize(
        objective,
        n_trials=args.n_trials,
        callbacks=[TargetReachedCallback()],
        show_progress_bar=False,
    )

    total_time = time.time() - start_time
    total_minutes = total_time / 60

    # ============================================================
    # SUMMARY
    # ============================================================
    print_study_summary(study)

    logger.info(f"\n  ⏱️  Total waktu optimasi: {total_minutes:.1f} menit")

    # Final check
    if GLOBAL_BEST["metrics"]:
        m = GLOBAL_BEST["metrics"]
        all_met = (
            m.get("cluster_anomaly_accuracy", 0) >= TARGET_CLUSTER_ACC and
            m.get("top3_rca_accuracy", 0) >= TARGET_RCA_A3 and
            m.get("fault_macro_f1", 0) >= TARGET_FAULT_F1
        )
        if all_met:
            logger.success("\n  🎉🎉🎉 SEMUA TARGET TERCAPAI! Model siap deploy! 🎉🎉🎉")
        else:
            logger.warning(
                f"\n  ⚠️ Target belum sepenuhnya tercapai."
                f"\n  Saran: Jalankan lagi dengan --resume --n-trials {args.n_trials}"
                f"\n  Optuna akan melanjutkan dari posisi terakhir (semakin pintar)."
            )


if __name__ == "__main__":
    main()
