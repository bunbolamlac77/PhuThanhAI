"""PhuThanhAI 3.0.0 - local-first, offline video library and analyzer.

This package is the single backend implementation shared by the CLI and the
FastAPI server. It must never import MLX/torch in-process: model inference is
performed by an isolated subprocess (see ``phuthanhai.analyzer.vlm_worker``).
"""

from __future__ import annotations

__all__ = ["PRODUCT_VERSION", "CONTRACT_VERSION"]

PRODUCT_VERSION = "3.0.0"
CONTRACT_VERSION = "1.0.0"
