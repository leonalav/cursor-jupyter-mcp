"""File-type profiles.

Each profile owns the addressing and structure rules that only make sense for
one language or format. The generic edit engine deliberately knows nothing
about them.
"""

from __future__ import annotations

__all__ = ["markdown_blocks", "python_symbols"]
