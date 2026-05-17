"""Shared internal utilities."""
from __future__ import annotations

import os


def require_env(name: str) -> str:
    """Read an env var or raise with a uniform message."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"{name} missing. Set it in .env")
    return val
