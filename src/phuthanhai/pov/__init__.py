"""AI Video POV - **hard-locked until Gate G5 passes**.

The specification allows POV work only after G0-G5 are proven. This module
therefore exposes a guard rather than a partial engine: importing it is harmless,
but any attempt to use it fails with a stable code instead of silently producing
a script from unvalidated data.

S06-S08 add: three script options (Qwen3-8B), Kokoro Vietnamese narration,
shot planning, editable timeline and FFmpeg render.
"""

from __future__ import annotations

SCHEDULED_FOR = "S06-S08 (after G5)"
LOCKED_CODE = "POV_LOCKED_UNTIL_G5"


class PovLockedError(RuntimeError):
    code = LOCKED_CODE


def require_unlocked(*, gates_passed: bool = False) -> None:
    """Raise unless G0-G5 have been proven. Never returns at S00."""

    if not gates_passed:
        raise PovLockedError(
            "AI Video POV is locked until G0-G5 pass; see artifacts/qa/ for gate evidence"
        )
