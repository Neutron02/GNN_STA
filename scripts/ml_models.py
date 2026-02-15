#!/usr/bin/env python3
"""Neural network models for timing prediction."""

from __future__ import annotations

try:
    import torch
    import torch.nn as nn
except Exception:  # pragma: no cover - runtime dependency guard
    torch = None
    nn = None


def require_torch() -> None:
    if torch is None or nn is None:
        raise RuntimeError("PyTorch is required for model code. Install torch and rerun.")


if nn is not None:

    class TimingMPNN(nn.Module):
        """Edge-aware message-passing regressor with optional categorical embeddings."""

        def __init__(
            self,
            node_dim: int,
            edge_dim: int,
            hidden_dim: int = 128,
            message_steps: int = 3,
            dropout: float = 0.1,
            cell_type_vocab_size: int = 0,
            cell_emb_dim: int = 0,
            use_message_gate: bool = True,
            use_layer_norm: bool = True,
            out_dim: int = 1,
        ) -> None:
            require_torch()
            super().__init__()
            self.hidden_dim = hidden_dim
            self.message_steps = message_steps
            self.cell_emb_dim = max(0, int(cell_emb_dim))
            self.cell_type_vocab_size = max(0, int(cell_type_vocab_size))
            self.out_dim = max(1, int(out_dim))

            if self.cell_emb_dim > 0 and self.cell_type_vocab_size > 0:
                self.cell_embedding = nn.Embedding(self.cell_type_vocab_size, self.cell_emb_dim)
            else:
                self.cell_embedding = None

            node_in_dim = node_dim + (self.cell_emb_dim if self.cell_embedding is not None else 0)

            self.node_encoder = nn.Sequential(
                nn.Linear(node_in_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.ReLU(),
            )
            self.edge_encoder = nn.Sequential(
                nn.Linear(edge_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.ReLU(),
            )
            self.msg_mlp = nn.Sequential(
                nn.Linear(hidden_dim * 2, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
            )
            self.msg_gate = None
            if use_message_gate:
                self.msg_gate = nn.Sequential(
                    nn.Linear(hidden_dim * 2, hidden_dim),
                    nn.ReLU(),
                    nn.Linear(hidden_dim, hidden_dim),
                    nn.Sigmoid(),
                )
            self.upd_mlp = nn.Sequential(
                nn.Linear(hidden_dim * 2, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
            )
            self.dropout = nn.Dropout(dropout)
            self.update_norm = nn.LayerNorm(hidden_dim) if use_layer_norm else None
            self.out_head = nn.Sequential(
                nn.Linear(hidden_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, self.out_dim),
            )

        def forward(self, x, edge_index, edge_attr, cell_type_idx=None):
            if self.cell_embedding is not None:
                if cell_type_idx is None:
                    raise RuntimeError("cell_type_idx is required when cell embeddings are enabled")
                x = torch.cat([x, self.cell_embedding(cell_type_idx)], dim=1)

            h = self.node_encoder(x)
            e = self.edge_encoder(edge_attr)
            src = edge_index[0]
            dst = edge_index[1]

            for _ in range(self.message_steps):
                msg_in = torch.cat([h[src], e], dim=1)
                msg = self.msg_mlp(msg_in)
                if self.msg_gate is not None:
                    msg = msg * self.msg_gate(msg_in)

                agg = torch.zeros_like(h)
                agg.index_add_(0, dst, msg)

                deg = torch.zeros(h.size(0), dtype=h.dtype, device=h.device)
                deg.index_add_(0, dst, torch.ones(dst.size(0), dtype=h.dtype, device=h.device))
                deg = deg.clamp(min=1.0).unsqueeze(1)
                agg = agg / deg

                upd_in = torch.cat([h, agg], dim=1)
                delta = self.upd_mlp(upd_in)
                h = h + self.dropout(torch.relu(delta))
                if self.update_norm is not None:
                    h = self.update_norm(h)

            out = self.out_head(h)
            if self.out_dim == 1:
                return out.squeeze(1)
            return out

else:

    class TimingMPNN:  # pragma: no cover - torch missing path
        def __init__(self, *args, **kwargs) -> None:
            require_torch()
