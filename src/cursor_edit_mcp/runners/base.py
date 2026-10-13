"""Subprocess runners for external validation tools.

Validation tools are discovered on the user's machine, not vendored: Ruff,
markdownlint-cli2, and Prettier are the user's tools, run with the user's
config, in the user's environment. Vendoring them would silently disagree with
whatever the project already uses.

Every runner here follows the same shape:

- content goes in on **stdin**, so nothing is written to the workspace to be
  validated, and the tool resolves its own config from the file path we pass;
- the result is parsed into `Diagnostic` objects, never raw text;
- a missing tool is an expected outcome, not an exception. Callers get a
  ``skipped`` result with a reason they can show the agent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ToolStatus:
    """Whether a tool exists, and how we found it."""

    available: bool
    executable: str | None = None
    version: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "version": self.version,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RunOutcome:
    """The result of one external invocation.

    ``ran`` distinguishes "the tool ran and found nothing" from "the tool was
    not available", which is the difference between a clean file and an
    unvalidated one.
    """

    ran: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    reason: str | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        """True when the tool produced output we can trust."""
        return self.ran and not self.timed_out and self.error is None


@dataclass(frozen=True)
class CallSpec:
    """One external invocation."""

    argv: list[str]
    cwd: Path | None = None
    stdin: str | None = None
    timeout: float = 10.0
    env: dict[str, str] | None = None


def run(spec: CallSpec) -> RunOutcome:
    """Run a subprocess with a hard timeout.

    Never raises for an expected failure: a missing binary, a non-zero exit, and
    a timeout all come back as an outcome the caller can report.
    """
    env = dict(os.environ)
    if spec.env:
        env.update(spec.env)

    try:
        completed = subprocess.run(  # noqa: S603 - argv is built from fixed literals
            spec.argv,
            cwd=str(spec.cwd) if spec.cwd else None,
            input=spec.stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return RunOutcome(ran=False, reason=f"{spec.argv[0]} is not installed")
    except subprocess.TimeoutExpired:
        return RunOutcome(
            ran=True, timed_out=True, reason=f"timed out after {spec.timeout:g}s"
        )
    except OSError as exc:
        return RunOutcome(ran=False, error=f"could not start {spec.argv[0]}: {exc}")

    return RunOutcome(
        ran=True,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=completed.returncode,
    )


def which_variants(name: str) -> list[str]:
    """Candidate executable names for a cross-platform install."""
    if os.name == "nt":
        return [f"{name}.exe", f"{name}.cmd", f"{name}.bat", name]
    return [name]


@dataclass
class Resolver:
    """Finds a tool in the workspace venv first, then on ``PATH``.

    The workspace venv comes first because that is the environment the project
    pins its tools into; a globally installed Ruff can be a different version
    with different rules.
    """

    workspace_root: Path
    _cache: dict[str, ToolStatus] = field(default_factory=dict)

    def locate(self, name: str) -> str | None:
        for candidate in self._venv_candidates(name):
            if candidate.is_file():
                return str(candidate)
        for variant in which_variants(name):
            found = shutil.which(variant)
            if found:
                return found
        return None

    def _venv_candidates(self, name: str) -> list[Path]:
        root = self.workspace_root
        if os.name == "nt":
            return [
                root / ".venv" / "Scripts" / f"{name}.exe",
                root / ".venv" / "Scripts" / f"{name}.cmd",
                root / "venv" / "Scripts" / f"{name}.exe",
            ]
        return [
            root / ".venv" / "bin" / name,
            root / "venv" / "bin" / name,
        ]

    def status(self, name: str, argv_prefix: list[str] | None = None) -> ToolStatus:
        """Locate a tool and read its version, caching the result."""
        key = f"{name}:{'|'.join(argv_prefix or [])}"
        if key in self._cache:
            return self._cache[key]

        executable = self.locate(name)
        if executable is None:
            status = ToolStatus(
                available=False,
                reason=f"{name} is not installed or not on PATH",
            )
            self._cache[key] = status
            return status

        outcome = run(
            CallSpec(
                argv=[executable, *(argv_prefix or ["--version"])],
                timeout=5.0,
            )
        )
        if not outcome.ran:
            status = ToolStatus(
                available=False, executable=executable, reason=outcome.reason
            )
        elif outcome.timed_out:
            status = ToolStatus(
                available=True,
                executable=executable,
                version=None,
                reason="version check timed out",
            )
        else:
            first = (outcome.stdout or outcome.stderr).strip().splitlines()
            status = ToolStatus(
                available=True,
                executable=executable,
                version=first[0].strip() if first else None,
            )
        self._cache[key] = status
        return status


def node_script(root: Path, name: str) -> str | None:
    """Locate a locally installed npm package binary, if there is one."""
    for base in (root / "node_modules" / ".bin", root / "node_modules"):
        for variant in which_variants(name):
            candidate = base / variant
            if candidate.is_file():
                return str(candidate)
    return None


def python_executable() -> str:
    """The interpreter running this server."""
    return sys.executable
