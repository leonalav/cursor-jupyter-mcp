"""Tool wrappers for the configuration service.

Two tools, one module each:

* :func:`config_detect` is a thin wrapper over :mod:`.detect`. The
  heuristic logic lives in ``detect`` so the same answers are reusable
  from the validation pipeline.
* :func:`config_doctor` runs a fixed set of conflict / misconfiguration
  checks and returns findings with evidence and a unified-diff
  ``suggested_patch``. The doctor never writes; that is the job of
  :func:`config_apply_fix`.
* :func:`config_apply_fix` applies one doctor's patch with an explicit
  confirmation token. It is the only mutating tool in the configuration
  service.

The split is deliberate: the doctor is the *diagnosis*, the apply tool
is the *prescription*, and they share a confirmation token model so a
token from a stale diagnosis cannot be replayed.
"""

from __future__ import annotations

from typing import Any

from ..config.detect import DETECT_DESCRIPTION
from ..config.detect import detect_file as _detect_file
from ..config.detect import detect_workspace as _detect_workspace
from ..config.doctor import (
    APPLY_DESCRIPTION,
    DOCTOR_DESCRIPTION,
    apply_fix as _apply_fix,
    diagnose as _diagnose,
)
from ..logging_setup import get_logger
from ..settings import Settings, get_settings

_LOGGER = get_logger(__name__)


def config_detect(
    path: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Tool entry: report the effective tooling for a path or workspace.

    With ``path``: per-file report, identical shape to the ``tooling``
    field on ``validate_file``.

    Without ``path``: workspace summary listing which tools are
    configured and which config files are present.
    """
    settings = settings or get_settings()
    if path is None:
        return _detect_workspace(settings)
    return _detect_file(path, settings)


def config_doctor(
    path: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Tool entry: run the doctor on a file or workspace.

    Returns a list of findings. Each finding has a ``severity`` (one of
    ``"error"``, ``"warning"``, ``"info"``), an ``evidence`` block with
    the file and line that triggered the check, an ``explanation`` the
    agent can paraphrase, and a ``suggested_patch`` as a unified diff.

    The doctor is read-only. Apply a patch with ``config_apply_fix``
    using the ``confirmation_token`` returned alongside the findings.
    """
    settings = settings or get_settings()
    return _diagnose(path, settings)


def config_apply_fix(
    finding_id: str,
    confirmation_token: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Tool entry: apply one doctor patch with explicit confirmation.

    ``finding_id`` is the stable id of the finding (the doctor's
    response includes a token table that maps ids to tokens). The
    caller is expected to have read the suggested patch and decided to
    apply it.
    """
    settings = settings or get_settings()
    return _apply_fix(finding_id, confirmation_token, settings)


__all__ = [
    "DETECT_DESCRIPTION",
    "DOCTOR_DESCRIPTION",
    "APPLY_DESCRIPTION",
    "config_detect",
    "config_doctor",
    "config_apply_fix",
]
