"""
pytest configuration for the knotify-deletion-initiator function unit tests.

Adds the shared Lambda layer source directories to sys.path so that
`import knotify_obs` resolves from the source tree without requiring the
layers to be packaged into zips first.

The deletion_initiator Lambda runs OUTSIDE the VPC and has no Aurora access,
so only the observability layer (knotify_obs) is needed — not knotify_db.
"""

import os
import sys

# Functions live at: infrastructure/src/functions/deletion_initiator/tests/  (this file)
# Layers live at:    infrastructure/src/layers/observability/
# Path traversal: tests/ -> deletion_initiator/ -> functions/ -> src/
_src_root = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

_layer_paths = [
    os.path.join(_src_root, "layers", "observability"),
]

for _p in _layer_paths:
    if _p not in sys.path:
        sys.path.insert(0, _p)
