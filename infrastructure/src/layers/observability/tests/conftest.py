"""
pytest configuration for the observability layer tests.

Adds the layer's python/ directory to sys.path when running tests directly
(without `make package` having been run), so that `import knotify_obs` works
from the source tree itself.
"""

import sys
import os

# During development the knotify_obs package lives at:
#   infrastructure/src/layers/observability/knotify_obs/
# which is one level above this conftest.py.  Pytest normally handles
# this via rootdir inference, but we add it explicitly so `python -m pytest`
# from any working directory also resolves the import.
_layer_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _layer_root not in sys.path:
    sys.path.insert(0, _layer_root)
