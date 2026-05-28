#!/usr/bin/env python3
"""
smoke_test_package.py — verify the package-test smoke zip is valid.

Usage:
    python smoke_test_package.py <build_dir>

Checks that build_dir/_smoke.zip exists, is non-empty, and contains
handler.py.  Cleans up after verification.

Exit codes:
  0 — PASS
  1 — FAIL
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <build_dir>", file=sys.stderr)
        return 1

    build_dir = Path(argv[1])
    zip_path = build_dir / "_smoke.zip"

    if not zip_path.exists() or zip_path.stat().st_size == 0:
        print(
            f"ERROR: smoke zip is missing or empty: {zip_path}",
            file=sys.stderr,
        )
        return 1

    print(f"[package-test] Smoke zip OK: {zip_path} ({zip_path.stat().st_size} bytes)")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        print(f"[package-test] Contents: {names}")
        if "handler.py" not in names:
            print("ERROR: handler.py missing from smoke zip", file=sys.stderr)
            return 1

    # Clean up
    import shutil
    shutil.rmtree(build_dir / "_smoke", ignore_errors=True)
    zip_path.unlink()
    print("[package-test] Cleanup done.")
    print("[package-test] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
