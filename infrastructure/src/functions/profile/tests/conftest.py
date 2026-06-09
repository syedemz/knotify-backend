"""
pytest configuration for the knotify-profile function unit tests.

Adds the shared Lambda layer source directories to sys.path so that
`import knotify_db` and `import knotify_obs` resolve from the source tree
without requiring the layers to be packaged into zips first.
"""

import os
import sys

# Functions live at: infrastructure/src/functions/profile/tests/  (this file)
# Layers live at:    infrastructure/src/layers/{db,observability}/
# Path traversal: tests/ -> profile/ -> functions/ -> src/
_src_root = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

_layer_paths = [
    os.path.join(_src_root, "layers", "db"),
    os.path.join(_src_root, "layers", "observability"),
]

for _p in _layer_paths:
    if _p not in sys.path:
        sys.path.insert(0, _p)
