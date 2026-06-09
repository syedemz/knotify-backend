"""
chat_room_id helper for Knotify.

Derives a stable, symmetric room identifier from two user UUIDs.  The two
UUIDs are sorted lexicographically (string comparison) so the smaller value
always comes first, then the pair is concatenated as "<min>:<max>" and hashed
with SHA-256.  This guarantees the same room ID regardless of the call order
and is the canonical contract used by both the blocks Lambda (phase 6) and
the chat Lambda (phase 8).
"""

import hashlib


def chat_room_id(user_a: str, user_b: str) -> str:
    """
    Return the SHA-256 hex digest of the lexicographically ordered pair.

    Args:
        user_a: UUID string of the first user.
        user_b: UUID string of the second user.

    Returns:
        64-character lowercase hex string.  Symmetric: the result is identical
        when the two arguments are swapped.
    """
    lex_min = min(user_a, user_b)
    lex_max = max(user_a, user_b)
    return hashlib.sha256(f"{lex_min}:{lex_max}".encode("utf-8")).hexdigest()
