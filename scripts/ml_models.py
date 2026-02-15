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
        """Simple edge-aware message-passing regressor."""

        def __init__(
            self,
            node_dim: int,
            edge_dim: int,
            hidden_dim: int = 128,
            message_steps: int = 3,
            dropout: float = 0.1,
        ) -> None:
            require_torch()
            super().__init__()
            self.hidden_dim = hidden_dim
            self.message_steps = message_steps

            self.node_encoder = nn.Sequential(
                nn.Linear(node_dim, hidden_dim),
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
            self.upd_mlp = nn.Sequential(
                nn.Linear(hidden_dim * 2, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
            )
            self.dropout = nn.Dropout(dropout)
            self.out_head = nn.Sequential(
                nn.Linear(hidden_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, 1),
            )

        def forward(self, x, edge_index, edge_attr):
            h = self.node_encoder(x)
            e = self.edge_encoder(edge_attr)
            src = edge_index[0]
            dst = edge_index[1]

            for _ in range(self.message_steps):
                msg_in = torch.cat([h[src], e], dim=1)
                msg = self.msg_mlp(msg_in)

                agg = torch.zeros_like(h)
                agg.index_add_(0, dst, msg)

                deg = torch.zeros(h.size(0), dtype=h.dtype, device=h.device)
                deg.index_add_(0, dst, torch.ones(dst.size(0), dtype=h.dtype, device=h.device))
                deg = deg.clamp(min=1.0).unsqueeze(1)
                agg = agg / deg

                upd_in = torch.cat([h, agg], dim=1)
                delta = self.upd_mlp(upd_in)
                h = h + self.dropout(torch.relu(delta))

            return self.out_head(h).squeeze(1)

else:

    class TimingMPNN:  # pragma: no cover - torch missing path
        def __init__(self, *args, **kwargs) -> None:
            require_torch()
