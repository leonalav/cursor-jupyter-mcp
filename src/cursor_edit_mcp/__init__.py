"""Precise, guarded editing tools for Cursor's agent over MCP.

The public API is resolved lazily so that ``python -m cursor_edit_mcp.server``
does not import the server module twice (once via this package, once as
``__main__``), which would emit a RuntimeWarning on stderr at startup.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .server import VERSION, build_server, main

__version__ = "0.1.0"
__all__ = ["VERSION", "__version__", "build_server", "main"]


def __getattr__(name: str):
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
