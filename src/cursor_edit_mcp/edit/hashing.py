"""Content hashing for the optimistic-concurrency guard.

Hashes are taken over the exact bytes on disk, so a change to a BOM, an
encoding, or the trailing newline is detected just like a change to code. That
is the property that makes the guard trustworthy: if the hash matches, the bytes
the agent reasoned about are still the bytes on disk.
"""

from __future__ import annotations

import hashlib

PREFIX = "sha256:"


def hash_bytes(data: bytes) -> str:
    """Return the canonical ``sha256:<hex>`` hash of ``data``."""
    return PREFIX + hashlib.sha256(data).hexdigest()


def normalize_hash(value: str) -> str:
    """Accept a hash with or without the ``sha256:`` prefix."""
    candidate = value.strip()
    if candidate.lower().startswith(PREFIX):
        return PREFIX + candidate[len(PREFIX) :].lower()
    return PREFIX + candidate.lower()


def short_hash(value: str) -> str:
    """Shorten a hash for logs and compact responses."""
    body = value.removeprefix(PREFIX)
    return f"{PREFIX}{body[:12]}"
