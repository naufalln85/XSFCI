"""
============================================================
XFSCI GNN Architecture - Layer 2: Cloud Intelligence
============================================================
Arsitektur Dual-Head GATv2 (Graph Attention Network v2)
untuk deteksi anomali dan lokalisasi akar masalah microservice:

1. GATv2 Backbone:
   - Dynamic edge attention weights (α_ij) antar-microservices
   - Multi-head attention untuk menangkap pola multi-dimensi (trafik, memory, cpu)
   - Residual / skip connection + LayerNorm untuk stabilitas gradien

2. Dual-Head Output:
   - Head 1 (Node Classifier):
       Memprediksi status anomali per-pod (5 kelas: NORMAL, CPU, MEMORY, CRASH, NET)
       Digunakan untuk Root-Cause Localization.
   - Head 2 (Graph-Level Urgency):
       Memprediksi skor risiko anomali kluster secara global (0.0 s/d 1.0)
       Digunakan langsung oleh Scoring Engine & Orchestrator.

3. Explainability (XAI):
   - Mampu mengekstrak bobot atensi (attention weights) untuk menunjukkan
     relasi pemanggilan atau dependensi mana yang paling terdampak.
============================================================
"""

from typing import Tuple, Optional, Dict, Any

import torch
import torch.nn as nn
import torch.nn.functional as F
from loguru import logger

try:
    from torch_geometric.nn import GATv2Conv, global_mean_pool, global_max_pool
    PYG_AVAILABLE = True
except ImportError:
    PYG_AVAILABLE = False
    logger.warning("torch_geometric.nn tidak ditemukan, fallback ke manual GAT implementation.")


# ============================================================
# FALLBACK MANUAL GAT LAYER (JIKA PYG BELUM TERINSTALL)
# ============================================================

class FallbackGATLayer(nn.Module):
    """Implementasi fallback murni PyTorch untuk GAT jika PyG tidak ada."""
    def __init__(self, in_features: int, out_features: int, heads: int = 1, dropout: float = 0.2):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.heads = heads
        self.dropout = nn.Dropout(dropout)
        self.W = nn.Linear(in_features, heads * out_features, bias=False)
        self.a_src = nn.Parameter(torch.zeros(size=(1, heads, out_features)))
        self.a_dst = nn.Parameter(torch.zeros(size=(1, heads, out_features)))
        nn.init.xavier_uniform_(self.W.weight, gain=1.414)
        nn.init.xavier_uniform_(self.a_src, gain=1.414)
        nn.init.xavier_uniform_(self.a_dst, gain=1.414)
        self.leaky_relu = nn.LeakyReLU(0.2)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, return_attention_weights: bool = False):
        N = x.size(0)
        h = self.W(x).view(N, self.heads, self.out_features)
        src, dst = edge_index[0], edge_index[1]
        
        attn_src = (h[src] * self.a_src).sum(dim=-1)
        attn_dst = (h[dst] * self.a_dst).sum(dim=-1)
        attn = self.leaky_relu(attn_src + attn_dst)
        
        # Segmented Softmax over edges per destination node (dst)
        # Menjamin total atensi masuk ke setiap node = 1.0
        exp_attn = torch.exp(attn)
        sum_exp = torch.zeros(N, self.heads, device=x.device)
        sum_exp.index_add_(0, dst, exp_attn)
        alpha = exp_attn / (sum_exp[dst] + 1e-12)
        alpha = self.dropout(alpha)
        
        # Message passing: kumpulkan pesan dari source ke destination
        out = torch.zeros(N, self.heads, self.out_features, device=x.device)
        msg = h[src] * alpha.unsqueeze(-1)
        out.index_add_(0, dst, msg)
        
        out = out.view(N, self.heads * self.out_features)
        if return_attention_weights:
            return out, (edge_index, alpha)
        return out


# ============================================================
# DUAL-HEAD GATv2 MODEL
# ============================================================

class DualHeadGATv2(nn.Module):
    """
    Arsitektur GATv2 Dual-Head untuk XFSCI Layer 2.
    
    Parameters:
      in_channels : Jumlah fitur input per node (default: 18)
      hidden_dim  : Dimensi representasi hidden (default: 32)
      num_heads   : Jumlah attention heads di layer 1 (default: 4)
      num_classes : Jumlah kelas anomali node (default: 5)
      dropout     : Rasio dropout (default: 0.2)
    """

    def __init__(self,
                 in_channels: int = 18,
                 hidden_dim: int = 32,
                 num_heads: int = 4,
                 num_classes: int = 5,
                 dropout: float = 0.2):
        super().__init__()
        self.in_channels = in_channels
        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.num_classes = num_classes
        self.dropout_rate = dropout

        # --- Layer 1: GATv2 Multi-Head ---
        l1_out_dim = hidden_dim * num_heads  # 32 * 4 = 128
        if PYG_AVAILABLE:
            self.gat1 = GATv2Conv(
                in_channels=in_channels,
                out_channels=hidden_dim,
                heads=num_heads,
                concat=True,
                dropout=dropout
            )
        else:
            self.gat1 = FallbackGATLayer(in_channels, hidden_dim, heads=num_heads, dropout=dropout)

        self.norm1 = nn.LayerNorm(l1_out_dim)
        self.res_proj1 = nn.Linear(in_channels, l1_out_dim)  # Skip connection

        # --- Layer 2: GATv2 Aggregation ---
        l2_out_dim = hidden_dim  # 32
        if PYG_AVAILABLE:
            self.gat2 = GATv2Conv(
                in_channels=l1_out_dim,
                out_channels=hidden_dim,
                heads=2,
                concat=False,  # Average heads
                dropout=dropout
            )
        else:
            self.gat2 = FallbackGATLayer(l1_out_dim, hidden_dim, heads=1, dropout=dropout)

        self.norm2 = nn.LayerNorm(l2_out_dim)
        self.res_proj2 = nn.Linear(l1_out_dim, l2_out_dim)

        self.dropout = nn.Dropout(dropout)
        self.activation = nn.ELU()

        # --- Head 1: Node-Level Anomaly Classifier ---
        # Menghasilkan logits [N, num_classes] untuk tiap pod
        # Menggabungkan representasi graf (h2) dan fitur lokal mentah pod (x)
        # via Residual Feature Bypass untuk membedakan node sehat dari tetangga bermasalah.
        self.node_classifier = nn.Sequential(
            nn.Linear(l2_out_dim + in_channels, 64),
            nn.ReLU(),
            nn.Dropout(dropout / 2),
            nn.Linear(64, num_classes),
        )

        # --- Head 2: Graph-Level Cluster Urgency Score ---
        # Menghasilkan skor skalar [batch_size, 1] antara 0.0 s/d 1.0
        self.graph_urgency_head = nn.Sequential(
            nn.Linear(l2_out_dim * 2, 32),  # Gabungan mean + max pooling
            nn.ReLU(),
            nn.Linear(32, 1),
            nn.Sigmoid(),
        )

        self._init_weights()
        logger.info(f"DualHeadGATv2 initialized | In: {in_channels} | Hidden: {hidden_dim}x{num_heads} | "
                    f"Classes: {num_classes} | Params: {self.count_parameters():,}")

    def _init_weights(self):
        """Xavier initialization untuk linear layers."""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0.0)

    def count_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

    def forward(self,
                x: torch.Tensor,
                edge_index: torch.Tensor,
                batch: Optional[torch.Tensor] = None,
                return_attention: bool = False) -> Tuple[torch.Tensor, torch.Tensor, Optional[Any]]:
        """
        Forward pass graf microservices.
        
        Args:
          x               : [Total_Nodes, in_channels]
          edge_index      : [2, E]
          batch           : [Total_Nodes] mapping node ke graph index (opsional)
          return_attention: Kembalikan bobot atensi jika True
          
        Returns:
          node_logits : [Total_Nodes, num_classes]
          graph_urgency: [Batch_Size, 1]
          attn_weights: Tuple (edge_index, alpha) jika return_attention=True
        """
        attn_weights = None

        # --- Block 1 ---
        res1 = self.res_proj1(x)
        if PYG_AVAILABLE and return_attention:
            h1, (edge_index_attn, alpha) = self.gat1(x, edge_index, return_attention_weights=True)
            attn_weights = (edge_index_attn, alpha)
        else:
            h1 = self.gat1(x, edge_index)
        
        h1 = self.norm1(h1 + res1)
        h1 = self.activation(h1)
        h1 = self.dropout(h1)

        # --- Block 2 ---
        res2 = self.res_proj2(h1)
        h2 = self.gat2(h1, edge_index)
        h2 = self.norm2(h2 + res2)
        h2 = self.activation(h2)
        h2 = self.dropout(h2)

        # --- Head 1: Node Classification with Residual Local Feature Bypass ---
        node_rep = torch.cat([h2, x], dim=-1)
        node_logits = self.node_classifier(node_rep)

        # --- Head 2: Graph-Level Global Risk Pooling ---
        if batch is None:
            # Single graph mode
            g_mean = h2.mean(dim=0, keepdim=True)
            g_max = h2.max(dim=0, keepdim=True)[0]
        else:
            # Batch mode
            if PYG_AVAILABLE:
                g_mean = global_mean_pool(h2, batch)
                g_max = global_max_pool(h2, batch)
            else:
                num_graphs = int(batch.max().item()) + 1
                g_mean_list = []
                g_max_list = []
                for b_idx in range(num_graphs):
                    mask = (batch == b_idx)
                    sub = h2[mask]
                    g_mean_list.append(sub.mean(dim=0, keepdim=True))
                    g_max_list.append(sub.max(dim=0, keepdim=True)[0])
                g_mean = torch.cat(g_mean_list, dim=0)
                g_max = torch.cat(g_max_list, dim=0)

        g_pooled = torch.cat([g_mean, g_max], dim=-1)  # [Batch_size, hidden_dim * 2]
        graph_urgency = self.graph_urgency_head(g_pooled)

        return node_logits, graph_urgency, attn_weights

    def predict_anomaly(self, x: torch.Tensor, edge_index: torch.Tensor) -> Dict[str, Any]:
        """
        Helper method untuk inferensi cepat (single-graph mode).
        
        Returns:
          dict berisi probabilitas, prediksi kelas per node, dan skor urgensi kluster.
        """
        self.eval()
        with torch.no_grad():
            node_logits, urgency, attn = self.forward(x, edge_index, batch=None, return_attention=True)
            node_probs = F.softmax(node_logits, dim=-1)
            pred_classes = torch.argmax(node_probs, dim=-1)
            
            return {
                "node_logits": node_logits.cpu(),
                "node_probs": node_probs.cpu(),
                "pred_classes": pred_classes.cpu(),
                "cluster_urgency": float(urgency.squeeze().item()),
                "attention_weights": attn,
            }


if __name__ == "__main__":
    logger.info("Testing DualHeadGATv2 model...")
    model = DualHeadGATv2(in_channels=18, hidden_dim=32, num_heads=4, num_classes=5)
    
    # Dummy single graph test (11 nodes, 18 features)
    dummy_x = torch.randn(11, 18)
    dummy_edge_index = torch.tensor([[0, 1, 2, 3], [1, 2, 3, 0]], dtype=torch.long)
    
    node_out, graph_out, _ = model(dummy_x, dummy_edge_index)
    print(f"Node logits shape   : {node_out.shape} (Expected: [11, 5])")
    print(f"Graph urgency shape : {graph_out.shape} (Expected: [1, 1])")
    print(f"Graph urgency value : {graph_out.item():.4f}")
    assert node_out.shape == (11, 5)
    assert graph_out.shape == (1, 1)
    logger.success("Model forward pass test passed!")
