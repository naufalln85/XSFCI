"""
============================================================
XFSCI GNN Trainer - Layer 2: Cloud Intelligence
============================================================
Script pelatihan model DualHeadGATv2:
  - Multi-task Loss: Node CrossEntropy + Graph BCE
  - Class-weighted loss untuk menangani imbalance level-node
  - Early stopping berdasarkan Macro-F1 validasi
  - Evaluasi komprehensif pada Test set (Akurasi, F1, Confusion Matrix)
  - Penyimpanan model checkpoint (gnn_best.pt) + metadata pelatihan

Cara pakai:
  python models/gnn/train_gnn.py
  python models/gnn/train_gnn.py --epochs 100 --batch-size 32 --lr 0.001
============================================================
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support, confusion_matrix
from loguru import logger

# Pastikan root proyek ada di sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from models.gnn.graph_dataset import (
    build_graph_dataloaders,
    LABEL_MAP,
    IDX_TO_LABEL,
    NUM_SERVICES,
)
from models.gnn.gnn_model import DualHeadGATv2

WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"
WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)


class GNNTrainer:
    """Trainer pipeline untuk DualHeadGATv2."""

    def __init__(self,
                 model: nn.Module,
                 train_loader,
                 val_loader,
                 test_loader,
                 device: torch.device,
                 lr: float = 0.001,
                 weight_decay: float = 1e-4,
                 graph_loss_weight: float = 0.5):
        self.model = model.to(device)
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.test_loader = test_loader
        self.device = device
        self.graph_loss_weight = graph_loss_weight

        # Hitung class weights untuk node-level loss (mengatasi dominasi NORMAL pada graf)
        self.class_weights = self._compute_class_weights().to(device)
        self.node_criterion = nn.CrossEntropyLoss(weight=self.class_weights)
        self.graph_criterion = nn.BCELoss()

        self.optimizer = AdamW(self.model.parameters(), lr=lr, weight_decay=weight_decay)
        self.scheduler = CosineAnnealingLR(self.optimizer, T_max=50, eta_min=1e-5)

        self.history = {
            "train_loss": [], "val_loss": [],
            "val_acc": [], "val_f1_macro": [],
            "train_node_loss": [], "train_graph_loss": []
        }

    def _compute_class_weights(self) -> torch.Tensor:
        """Menghitung inverse class frequency pada dataset train."""
        counts = np.zeros(len(LABEL_MAP), dtype=np.float32)
        for batch in self.train_loader:
            labels = batch.y.cpu().numpy()
            for l in labels:
                counts[l] += 1
        
        total = counts.sum()
        # Inverse frequency dengan smoothing
        weights = total / (len(counts) * np.maximum(counts, 1.0))
        # Normalisasi
        weights = weights / weights.sum() * len(counts)
        logger.info(f"Class weights node: { {IDX_TO_LABEL[i]: round(float(w), 3) for i, w in enumerate(weights)} }")
        return torch.tensor(weights, dtype=torch.float32)

    def train_epoch(self) -> Tuple[float, float, float]:
        self.model.train()
        total_loss = 0.0
        total_node_loss = 0.0
        total_graph_loss = 0.0
        num_batches = 0

        for batch in self.train_loader:
            batch = batch.to(self.device)
            self.optimizer.zero_grad()

            batch_idx = getattr(batch, "batch", None)
            node_logits, graph_urgency, _ = self.model(batch.x, batch.edge_index, batch_idx)

            loss_node = self.node_criterion(node_logits, batch.y)
            loss_graph = self.graph_criterion(graph_urgency.squeeze(-1), batch.y_graph.squeeze(-1))

            loss = loss_node + (self.graph_loss_weight * loss_graph)
            loss.backward()

            # Gradient clipping untuk mencegah ledakan gradien
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=2.0)
            self.optimizer.step()

            total_loss += loss.item()
            total_node_loss += loss_node.item()
            total_graph_loss += loss_graph.item()
            num_batches += 1

        self.scheduler.step()
        avg_loss = total_loss / max(num_batches, 1)
        avg_node = total_node_loss / max(num_batches, 1)
        avg_graph = total_graph_loss / max(num_batches, 1)
        return avg_loss, avg_node, avg_graph

    def evaluate(self, loader) -> Tuple[float, float, float, np.ndarray, np.ndarray]:
        self.model.eval()
        total_loss = 0.0
        all_preds = []
        all_targets = []
        num_batches = 0

        with torch.no_grad():
            for batch in loader:
                batch = batch.to(self.device)
                batch_idx = getattr(batch, "batch", None)
                node_logits, graph_urgency, _ = self.model(batch.x, batch.edge_index, batch_idx)

                loss_node = self.node_criterion(node_logits, batch.y)
                loss_graph = self.graph_criterion(graph_urgency.squeeze(-1), batch.y_graph.squeeze(-1))
                loss = loss_node + (self.graph_loss_weight * loss_graph)

                total_loss += loss.item()
                preds = torch.argmax(node_logits, dim=-1).cpu().numpy()
                targets = batch.y.cpu().numpy()

                all_preds.extend(preds)
                all_targets.extend(targets)
                num_batches += 1

        all_preds = np.array(all_preds)
        all_targets = np.array(all_targets)

        acc = accuracy_score(all_targets, all_preds)
        f1_macro = f1_score(all_targets, all_preds, average="macro", zero_division=0)
        avg_loss = total_loss / max(num_batches, 1)

        return avg_loss, acc, f1_macro, all_targets, all_preds

    def run_training(self, epochs: int = 80, patience: int = 15) -> Dict[str, Any]:
        logger.info("=" * 60)
        logger.info(f"Mulai pelatihan GNN: {epochs} epochs | Device: {self.device} | Early Stopping: {patience}")
        logger.info("=" * 60)

        best_val_f1 = -1.0
        best_epoch = 0
        patience_counter = 0
        best_state = None

        start_time = time.time()

        for epoch in range(1, epochs + 1):
            train_loss, train_node, train_graph = self.train_epoch()
            val_loss, val_acc, val_f1, _, _ = self.evaluate(self.val_loader)

            self.history["train_loss"].append(train_loss)
            self.history["val_loss"].append(val_loss)
            self.history["val_acc"].append(val_acc)
            self.history["val_f1_macro"].append(val_f1)
            self.history["train_node_loss"].append(train_node)
            self.history["train_graph_loss"].append(train_graph)

            lr_curr = self.optimizer.param_groups[0]["lr"]

            if epoch % 5 == 0 or epoch == 1 or val_f1 > best_val_f1:
                logger.info(f"Epoch {epoch:>3}/{epochs} | Train Loss: {train_loss:.4f} (Node: {train_node:.3f}, Graph: {train_graph:.3f}) | "
                            f"Val Loss: {val_loss:.4f} | Val Acc: {val_acc*100:5.2f}% | Val F1: {val_f1*100:5.2f}% | LR: {lr_curr:.6f}")

            # Checkpoint terbaik berdasarkan F1-Macro
            if val_f1 > best_val_f1:
                best_val_f1 = val_f1
                best_epoch = epoch
                best_state = {k: v.cpu() for k, v in self.model.state_dict().items()}
                patience_counter = 0
            else:
                patience_counter += 1
                if patience_counter >= patience:
                    logger.warning(f"Early stopping aktif pada epoch {epoch} (Best epoch: {best_epoch} dengan Val F1: {best_val_f1*100:.2f}%)")
                    break

        training_time = round(time.time() - start_time, 2)
        logger.success(f"Pelatihan selesai dalam {training_time}s | Best Epoch: {best_epoch} | Best Val F1: {best_val_f1*100:.2f}%")

        # Muat bobot terbaik
        if best_state is not None:
            self.model.load_state_dict({k: v.to(self.device) for k, v in best_state.items()})

        # Evaluasi Akhir pada Test Set
        logger.info("\n" + "=" * 60)
        logger.info("🧪 EVALUASI AKHIR PADA TEST SET (HELD-OUT KRONOLOGIS)")
        logger.info("=" * 60)

        test_loss, test_acc, test_f1, y_true, y_pred = self.evaluate(self.test_loader)
        logger.success(f"TEST ACCURACY : {test_acc*100:6.2f}%")
        logger.success(f"TEST MACRO-F1 : {test_f1*100:6.2f}%")

        # Per-class metrics
        precision, recall, f1, support = precision_recall_fscore_support(y_true, y_pred, zero_division=0)
        logger.info("\nLaporan Per-Kelas:")
        for idx, name in IDX_TO_LABEL.items():
            if idx < len(precision):
                p, r, f, s = precision[idx], recall[idx], f1[idx], support[idx]
                logger.info(f"  {name:<25} : P={p*100:5.1f}% | R={r*100:5.1f}% | F1={f*100:5.1f}% | Samples={s:>4}")

        # Confusion matrix
        cm = confusion_matrix(y_true, y_pred)
        logger.info("\nConfusion Matrix:")
        logger.info(f"{'Pred ->':<10}" + "".join([f"{k[:4]:>7}" for k in LABEL_MAP.keys()]))
        for i, row in enumerate(cm):
            row_str = "".join([f"{val:>7}" for val in row])
            lbl_name = IDX_TO_LABEL.get(i, str(i))[:9]
            logger.info(f"{lbl_name:<10}{row_str}")

        # Simpan Model Checkpoint
        model_path = WEIGHTS_DIR / "gnn_best.pt"
        metrics_path = WEIGHTS_DIR / "training_metrics.json"

        checkpoint = {
            "model_state_dict": self.model.state_dict(),
            "model_config": {
                "in_channels": self.model.in_channels,
                "hidden_dim": self.model.hidden_dim,
                "num_heads": self.model.num_heads,
                "num_classes": self.model.num_classes,
                "dropout": self.model.dropout_rate,
            },
            "class_weights": self.class_weights.cpu().tolist(),
            "training_metrics": {
                "best_epoch": best_epoch,
                "best_val_f1": float(best_val_f1),
                "test_accuracy": float(test_acc),
                "test_f1_macro": float(test_f1),
                "training_time_seconds": training_time,
                "timestamp": datetime.utcnow().isoformat(),
            }
        }

        torch.save(checkpoint, model_path)
        logger.success(f"Model tersimpan di: {model_path} ({model_path.stat().st_size / 1024:.1f} KB)")

        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(checkpoint["training_metrics"], f, indent=2)
        logger.success(f"Metrik tersimpan di: {metrics_path.name}")

        return checkpoint["training_metrics"]


def main():
    parser = argparse.ArgumentParser(description="XFSCI GNN Trainer")
    parser.add_argument("--epochs", type=int, default=80, help="Jumlah epoch (default: 80)")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size (default: 32)")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate (default: 0.001)")
    parser.add_argument("--patience", type=int, default=15, help="Early stopping patience (default: 15)")
    parser.add_argument("--device", type=str, default="auto", help="Device: cpu | cuda | auto")
    args = parser.parse_args()

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)

    logger.info(f"Target Device: {device}")

    # Build dataloaders
    train_loader, val_loader, test_loader, info = build_graph_dataloaders(batch_size=args.batch_size)

    # Inisialisasi Model
    model = DualHeadGATv2(
        in_channels=info["num_features"],
        hidden_dim=32,
        num_heads=4,
        num_classes=info["num_classes"],
        dropout=0.2
    )

    trainer = GNNTrainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        test_loader=test_loader,
        device=device,
        lr=args.lr,
        graph_loss_weight=0.5
    )

    metrics = trainer.run_training(epochs=args.epochs, patience=args.patience)
    logger.success(f"GNN Training Pipeline Selesai! Final Test Acc: {metrics['test_accuracy']*100:.2f}% | F1: {metrics['test_f1_macro']*100:.2f}%")


if __name__ == "__main__":
    main()
