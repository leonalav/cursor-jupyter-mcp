"""Configuration resolution."""

from __future__ import annotations

from . import resolution
from .detect import (
    DETECT_DESCRIPTION,
    WorkspaceConfigReport,
    detect_file,
    detect_workspace,
)

__all__ = [
    "resolution",
    "detect",
    "DETECT_DESCRIPTION",
    "WorkspaceConfigReport",
    "detect_file",
    "detect_workspace",
]
