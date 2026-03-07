from __future__ import annotations

import os
from pathlib import Path


def get_project_root() -> Path:
    root = os.environ.get("ECONSIGNALS_ROOT")
    if root:
        return Path(root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]
