#!/usr/bin/env python3
"""
build_package.py — build a Lambda deployment zip for a single function.

Usage:
    python build_package.py \\
        --func <function_name> \\
        --src-root <path/to/infrastructure/src> \\
        --build-dir <path/to/build> \\
        [--include-dir SRC:DEST[:REGEX] ...]

What it does:
  1. Validates that src-root/functions/<func>/ exists.
  2. Reads the union of layer manifests from
       src-root/layers/observability/layer_manifest.txt
       src-root/layers/db/layer_manifest.txt
     to determine which distribution names are already present at Lambda
     runtime via the attached layers.
  3. If src-root/functions/<func>/requirements.txt exists, strips any
     layer-provided package from it and pip-installs the remainder into
     a temporary staging directory (build-dir/<func>/).
  4. Copies all *.py source files from src-root/functions/<func>/ into
     the staging directory, excluding the tests/ subdirectory and
     __pycache__ directories.
  5. For each --include-dir SRC:DEST[:REGEX] argument, copies matching
     files from SRC into build-dir/<func>/DEST/.  REGEX defaults to
     match all files when omitted.  Used by db_migrator to bundle
     infrastructure/db/migrations/*.sql into the zip.
  6. Creates build-dir/<func>.zip from the staging directory, excluding
     __pycache__/, *.pyc files, and the internal temp requirements file.

Exit codes:
  0 — success (zip created at build-dir/<func>.zip)
  1 — error (message printed to stderr)

This script is called by `make package FUNC=<name>` in the repo root
Makefile.  It is pure Python and requires no system `zip` binary.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# Layer manifest handling
# ---------------------------------------------------------------------------

def _normalise(name: str) -> str:
    """Normalise a distribution name: lowercase, hyphens/dots → underscores."""
    return re.sub(r"[-_.]+", "_", name).lower()


def _load_manifest(path: Path) -> set[str]:
    """Return the set of normalised distribution names listed in *path*."""
    if not path.exists():
        return set()
    names: set[str] = set()
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(_normalise(line))
    return names


def _pkg_name_from_req_line(line: str) -> str | None:
    """
    Extract the distribution name from a requirements.txt line.
    Returns None for comments, blank lines, or pip flag lines (e.g. -r, -i).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    match = re.match(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)", stripped)
    if not match:
        return None
    return match.group(1)


def _filter_requirements(
    reqs_path: Path,
    layer_pkgs: set[str],
) -> list[str]:
    """Return requirements lines with layer-provided packages removed."""
    filtered: list[str] = []
    for line in reqs_path.read_text().splitlines():
        pkg_name = _pkg_name_from_req_line(line)
        if pkg_name is None:
            filtered.append(line)
            continue
        if _normalise(pkg_name) in layer_pkgs:
            print(f"[build_package]   Excluding layer-provided: {pkg_name}")
        else:
            filtered.append(line)
    return filtered


# ---------------------------------------------------------------------------
# Include-dir copying (M-new-1)
# ---------------------------------------------------------------------------

def _copy_include_dir(
    src_dir: Path,
    staging: Path,
    dest_subdir: str,
    file_regex: str = r".*",
) -> None:
    """
    Copy files from *src_dir* whose names match *file_regex* into
    *staging*/*dest_subdir*/.

    Args:
        src_dir:     Absolute path to the source directory containing the
                     files to copy (e.g. infrastructure/db/migrations/).
        staging:     Staging directory for this function's build.
        dest_subdir: Relative path inside *staging* where files land.
                     Created if it does not exist.  May be nested
                     (e.g. "deep/nested/migrations").
        file_regex:  Python regex that must fully match the *filename*
                     (not the full path) for it to be included.
                     Default matches every file.
    """
    dest = staging / dest_subdir
    dest.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(file_regex)
    for entry in sorted(src_dir.iterdir()):
        if not entry.is_file():
            continue
        if pattern.match(entry.name):
            shutil.copy2(entry, dest / entry.name)
            print(f"[build_package]     include-dir: {entry.name} → {dest_subdir}/")


# ---------------------------------------------------------------------------
# Zip creation
# ---------------------------------------------------------------------------

_EXCLUDE_SUFFIXES = {".pyc"}
_EXCLUDE_PARTS = {"__pycache__"}
_EXCLUDE_NAMES = {"_filtered_requirements.txt"}


def _should_exclude(relative: Path) -> bool:
    if relative.suffix in _EXCLUDE_SUFFIXES:
        return True
    if relative.name in _EXCLUDE_NAMES:
        return True
    if _EXCLUDE_PARTS & set(relative.parts):
        return True
    return False


def _create_zip(staging: Path, output: Path) -> None:
    """Zip *staging* into *output*, honouring the exclusion rules above."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(staging.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(staging)
            if _should_exclude(relative):
                continue
            zf.write(file_path, relative)
    print(f"[build_package] Created {output} ({output.stat().st_size} bytes)")


# ---------------------------------------------------------------------------
# Main build logic
# ---------------------------------------------------------------------------

def build(
    func: str,
    src_root: Path,
    build_dir: Path,
    include_dirs: Sequence[tuple[Path, str, str]] | None = None,
) -> int:
    """
    Build a Lambda deployment zip for *func*.

    Args:
        func:         Function name (must match src-root/functions/<func>/).
        src_root:     Absolute path to infrastructure/src/.
        build_dir:    Directory where the zip and staging tree are written.
        include_dirs: Optional list of (src_dir, dest_subdir, file_regex)
                      tuples.  For each entry, files in *src_dir* whose names
                      match *file_regex* are copied into *dest_subdir/* inside
                      the staging tree.  Used by db_migrator to bundle
                      infrastructure/db/migrations/*.sql.
    """
    func_dir = src_root / "functions" / func
    if not func_dir.is_dir():
        print(
            f"ERROR: function directory does not exist: {func_dir}",
            file=sys.stderr,
        )
        return 1

    print(f"[build_package] Building {func} ...")

    staging = build_dir / func
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # ------------------------------------------------------------------
    # Collect layer-provided package names
    # ------------------------------------------------------------------
    layer_pkgs: set[str] = set()
    for manifest_path in [
        src_root / "layers" / "observability" / "layer_manifest.txt",
        src_root / "layers" / "db" / "layer_manifest.txt",
    ]:
        loaded = _load_manifest(manifest_path)
        if loaded:
            print(
                f"[build_package]   Loaded manifest {manifest_path.name}: "
                f"{sorted(loaded)}"
            )
        layer_pkgs |= loaded

    # ------------------------------------------------------------------
    # Install function-specific requirements (minus layer-provided ones)
    # ------------------------------------------------------------------
    reqs_path = func_dir / "requirements.txt"
    if reqs_path.exists():
        filtered = _filter_requirements(reqs_path, layer_pkgs)
        # Drop blank / comment-only lines for the install check
        install_lines = [
            l for l in filtered
            if l.strip() and not l.strip().startswith("#")
        ]
        if install_lines:
            filtered_reqs = staging / "_filtered_requirements.txt"
            filtered_reqs.write_text("\n".join(filtered) + "\n")
            print(
                f"[build_package]   Installing {len(install_lines)} "
                f"function-specific requirement(s) ..."
            )
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "--quiet", "--target", str(staging),
                    "-r", str(filtered_reqs),
                ],
                check=True,
            )
        else:
            print(
                "[build_package]   No function-specific requirements "
                "after layer exclusion."
            )
    else:
        print("[build_package]   No requirements.txt — skipping pip install.")

    # ------------------------------------------------------------------
    # Copy Python source files (excluding tests/ and __pycache__)
    # ------------------------------------------------------------------
    print("[build_package]   Copying source files (excluding tests/) ...")
    for src_file in sorted(func_dir.rglob("*.py")):
        relative = src_file.relative_to(func_dir)
        parts = relative.parts
        # Exclude tests/ and __pycache__
        if "tests" in parts or "__pycache__" in parts:
            continue
        dest = staging / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
        print(f"[build_package]     {relative}")

    # ------------------------------------------------------------------
    # Copy extra files from include_dirs (M-new-1)
    # ------------------------------------------------------------------
    if include_dirs:
        print("[build_package]   Copying include-dir assets ...")
        for src_dir, dest_subdir, file_regex in include_dirs:
            print(
                f"[build_package]     {src_dir} → {dest_subdir}/ "
                f"(regex: {file_regex})"
            )
            _copy_include_dir(src_dir, staging, dest_subdir, file_regex)

    # ------------------------------------------------------------------
    # Create the deployment zip
    # ------------------------------------------------------------------
    zip_path = build_dir / f"{func}.zip"
    if zip_path.exists():
        zip_path.unlink()
    _create_zip(staging, zip_path)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build a Lambda deployment zip for a single function."
    )
    parser.add_argument("--func", required=True, help="Function name")
    parser.add_argument(
        "--src-root", required=True, type=Path,
        help="Absolute path to infrastructure/src/",
    )
    parser.add_argument(
        "--build-dir", required=True, type=Path,
        help="Absolute path to the build output directory",
    )
    parser.add_argument(
        "--include-dir",
        action="append",
        dest="include_dirs",
        metavar="SRC:DEST[:REGEX]",
        default=[],
        help=(
            "Copy files from SRC into DEST/ inside the package staging dir. "
            "REGEX (optional) is a Python regex matched against file names; "
            "default is '.*' (all files).  Repeatable.  "
            "Example: infrastructure/db/migrations:migrations:^\\d{4}_.+\\.sql$"
        ),
    )
    args = parser.parse_args(argv[1:])

    # Parse --include-dir SRC:DEST[:REGEX] entries
    include_dirs: list[tuple[Path, str, str]] = []
    for raw in args.include_dirs:
        parts = raw.split(":", 2)
        if len(parts) < 2:
            print(
                f"ERROR: --include-dir must be SRC:DEST[:REGEX], got: {raw!r}",
                file=sys.stderr,
            )
            return 1
        src_dir = Path(parts[0])
        dest_subdir = parts[1]
        file_regex = parts[2] if len(parts) == 3 else r".*"
        if not src_dir.is_dir():
            print(
                f"ERROR: --include-dir source directory does not exist: {src_dir}",
                file=sys.stderr,
            )
            return 1
        include_dirs.append((src_dir, dest_subdir, file_regex))

    return build(
        args.func,
        args.src_root,
        args.build_dir,
        include_dirs=include_dirs or None,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
