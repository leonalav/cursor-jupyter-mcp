"""Edit primitives: document model, hashing, batching, atomic writes."""

from .atomic import atomic_write
from .batch import Edit, apply_batch, validate_batch
from .document import TextDocument, detect_eol, read_document
from .hashing import hash_bytes, normalize_hash

__all__ = [
    "Edit",
    "TextDocument",
    "apply_batch",
    "atomic_write",
    "detect_eol",
    "hash_bytes",
    "normalize_hash",
    "read_document",
    "validate_batch",
]
