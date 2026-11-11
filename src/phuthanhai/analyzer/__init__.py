"""Media analysis: probing, frame extraction, isolated VLM worker.

Importing this package must not import MLX, torch or OpenCV. Those live only in
``vlm_worker``, which is always executed as a separate process.
"""

from __future__ import annotations
