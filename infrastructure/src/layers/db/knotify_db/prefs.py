"""
knotify_db.prefs — preference vector encoder.

Public surface (re-exported by knotify_db/__init__.py):
  PREFERENCE_KEYS : list[str]
      The ordered list of 20 boolean preference keys from §5.5 of
      architecture.md. This is the single source of truth for the 20-D
      preference vector encoding. Do NOT redefine this list elsewhere.

  encode_prefs(prefs: dict) -> list[float]
      Map a preferences JSONB dict to a 20-dimensional float vector.
      Each position corresponds to PREFERENCE_KEYS[i]: 1.0 if prefs.get(key)
      is truthy, 0.0 otherwise. Unknown keys in prefs are ignored.

Usage in profile PATCH handler:
    from knotify_db import encode_prefs
    vec = encode_prefs(patch_data["preferences"])  # list of 20 floats
    # bind with explicit SQL cast: "preference_vector = %s::vector"
    # psycopg2's default list adapter + the ::vector cast handles the
    # write without pgvector's register_vector() adapter.
"""

from __future__ import annotations

# §5.5 of architecture.md — verbatim key ordering.
# Must not be modified without updating the pgvector column definition and any
# existing preference_vector data in Aurora.
PREFERENCE_KEYS: list[str] = [
    "highlyeducated", "moderateeducated", "basiceducated",
    "familyoriented", "homeoriented", "workoriented", "religionoriented",
    "talkative", "reserved", "cheerful", "serious", "listener",
    "intelligent", "welldressed", "athletic",
    "travel", "cooking", "reading", "movies", "nature",
]


def encode_prefs(prefs: dict) -> list[float]:
    """
    Encode a preferences dict as a 20-dimensional float vector.

    Args:
        prefs: JSONB preferences dict (may be empty, may contain unknown keys).

    Returns:
        A list of 20 floats. Position i is 1.0 if prefs.get(PREFERENCE_KEYS[i])
        is truthy, else 0.0. Unknown keys in prefs are ignored. The result
        length is always exactly len(PREFERENCE_KEYS) == 20.
    """
    return [1.0 if prefs.get(k, False) else 0.0 for k in PREFERENCE_KEYS]
