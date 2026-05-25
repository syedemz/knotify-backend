"""
Unit tests for build_package.py helper.

Story 3.5 AC: make package FUNC=<name> produces a Lambda deployment .zip
under build/ from src/functions/<name>/, excluding tests/ subdirectory and
any dependencies provided by the observability or db layer.
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from build_package import (
    _normalise,
    _load_manifest,
    _pkg_name_from_req_line,
    _filter_requirements,
    _should_exclude,
    build,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tree(files: dict[str, str]) -> Path:
    """Create a temp directory with the given {relative_path: content} files."""
    d = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return d


# ---------------------------------------------------------------------------
# _normalise
# ---------------------------------------------------------------------------

class TestNormalise(unittest.TestCase):
    def test_lowercase_unchanged(self):
        self.assertEqual(_normalise("requests"), "requests")

    def test_hyphens_become_underscores(self):
        self.assertEqual(_normalise("aws-lambda-powertools"), "aws_lambda_powertools")

    def test_mixed_case_lowercased(self):
        self.assertEqual(_normalise("PyJWT"), "pyjwt")

    def test_dots_become_underscores(self):
        self.assertEqual(_normalise("some.package"), "some_package")

    def test_psycopg2_binary(self):
        self.assertEqual(_normalise("psycopg2-binary"), "psycopg2_binary")


# ---------------------------------------------------------------------------
# _load_manifest
# ---------------------------------------------------------------------------

class TestLoadManifest(unittest.TestCase):
    def test_comments_and_blanks_ignored(self):
        root = _make_tree({
            "manifest.txt": "# comment\naws-lambda-powertools\njwt\n\n# more\ncryptography\n"
        })
        result = _load_manifest(root / "manifest.txt")
        self.assertEqual(result, {"aws_lambda_powertools", "jwt", "cryptography"})

    def test_missing_file_returns_empty_set(self):
        result = _load_manifest(Path("/nonexistent/manifest.txt"))
        self.assertEqual(result, set())


# ---------------------------------------------------------------------------
# _pkg_name_from_req_line
# ---------------------------------------------------------------------------

class TestPkgNameFromReqLine(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(_pkg_name_from_req_line("requests"), "requests")

    def test_pinned_version(self):
        self.assertEqual(_pkg_name_from_req_line("requests==2.32.3"), "requests")

    def test_extras(self):
        self.assertEqual(_pkg_name_from_req_line("PyJWT[crypto]==2.13.0"), "PyJWT")

    def test_comment_returns_none(self):
        self.assertIsNone(_pkg_name_from_req_line("# comment"))

    def test_blank_returns_none(self):
        self.assertIsNone(_pkg_name_from_req_line(""))

    def test_flag_returns_none(self):
        self.assertIsNone(_pkg_name_from_req_line("-r other.txt"))


# ---------------------------------------------------------------------------
# _filter_requirements
# ---------------------------------------------------------------------------

class TestFilterRequirements(unittest.TestCase):
    def _write_reqs(self, content: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "requirements.txt"
        p.write_text(content)
        return p

    def test_layer_package_excluded(self):
        reqs = self._write_reqs("boto3==1.34.0\nrequests==2.32.3\n")
        result = _filter_requirements(reqs, {"requests"})
        non_blank = [l for l in result if l.strip() and not l.strip().startswith("#")]
        self.assertIn("boto3==1.34.0", non_blank)
        self.assertNotIn("requests==2.32.3", non_blank)

    def test_non_layer_package_kept(self):
        reqs = self._write_reqs("boto3==1.34.0\n")
        result = _filter_requirements(reqs, {"requests"})
        non_blank = [l for l in result if l.strip() and not l.strip().startswith("#")]
        self.assertIn("boto3==1.34.0", non_blank)

    def test_normalisation_matches_pyjwt(self):
        # Manifest has "pyjwt" (normalised from "PyJWT"); reqs has "PyJWT[crypto]"
        reqs = self._write_reqs("PyJWT[crypto]==2.13.0\n")
        result = _filter_requirements(reqs, {"pyjwt"})
        non_blank = [l for l in result if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(non_blank, [])


# ---------------------------------------------------------------------------
# _should_exclude
# ---------------------------------------------------------------------------

class TestShouldExclude(unittest.TestCase):
    def test_py_file_not_excluded(self):
        self.assertFalse(_should_exclude(Path("handler.py")))

    def test_pyc_excluded(self):
        self.assertTrue(_should_exclude(Path("handler.cpython-312.pyc")))

    def test_pycache_nested_excluded(self):
        self.assertTrue(_should_exclude(Path("__pycache__/module.cpython-312.pyc")))

    def test_filtered_requirements_excluded(self):
        self.assertTrue(_should_exclude(Path("_filtered_requirements.txt")))


# ---------------------------------------------------------------------------
# build() — end-to-end
# ---------------------------------------------------------------------------

class TestBuild(unittest.TestCase):
    """
    Given a minimal src-root with a function directory,
    when build() is called,
    then a non-empty zip is produced containing the function's source files
    but not its tests/ directory.
    """

    def _make_src_root(self, func_files: dict[str, str]) -> tuple[Path, Path]:
        """
        Return (src_root, build_dir) with a minimal layout:
          src_root/
            functions/<func>/
              handler.py
              tests/
                test_handler.py
            layers/observability/layer_manifest.txt  (minimal)
            layers/db/layer_manifest.txt              (minimal)
        """
        root = Path(tempfile.mkdtemp())
        src_root = root / "src"
        build_dir = root / "build"
        build_dir.mkdir()

        # layer manifests
        obs_manifest = src_root / "layers" / "observability" / "layer_manifest.txt"
        obs_manifest.parent.mkdir(parents=True)
        obs_manifest.write_text("aws-lambda-powertools\nPyJWT\nrequests\n")

        db_manifest = src_root / "layers" / "db" / "layer_manifest.txt"
        db_manifest.parent.mkdir(parents=True)
        db_manifest.write_text("psycopg2-binary\npgvector\n")

        # function files
        for rel, content in func_files.items():
            target = src_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        return src_root, build_dir

    def test_given_smoke_function_when_build_then_zip_created(self):
        """
        Given a function with handler.py only (no requirements.txt),
        when build() is called,
        then a non-empty zip containing handler.py is produced.
        """
        src_root, build_dir = self._make_src_root({
            "functions/_smoke/handler.py": "def handler(e, c): return {}",
        })

        rc = build("_smoke", src_root, build_dir)

        self.assertEqual(rc, 0)
        zip_path = build_dir / "_smoke.zip"
        self.assertTrue(zip_path.exists(), "Zip file was not created")
        self.assertGreater(zip_path.stat().st_size, 0)
        with zipfile.ZipFile(zip_path) as zf:
            self.assertIn("handler.py", zf.namelist())

    def test_given_function_with_tests_when_build_then_tests_excluded_from_zip(self):
        """
        Given a function with handler.py and tests/test_handler.py,
        when build() is called,
        then the zip contains handler.py but NOT tests/test_handler.py.
        """
        src_root, build_dir = self._make_src_root({
            "functions/my_func/handler.py": "def handler(e, c): return {}",
            "functions/my_func/tests/test_handler.py": "def test_noop(): pass",
        })

        rc = build("my_func", src_root, build_dir)

        self.assertEqual(rc, 0)
        with zipfile.ZipFile(build_dir / "my_func.zip") as zf:
            names = zf.namelist()
        self.assertIn("handler.py", names)
        self.assertFalse(
            any("tests" in n for n in names),
            f"tests/ should be excluded from zip. Got: {names}",
        )

    def test_given_nonexistent_function_when_build_then_returns_nonzero(self):
        src_root, build_dir = self._make_src_root({})
        rc = build("nonexistent_func", src_root, build_dir)
        self.assertNotEqual(rc, 0)

    def test_given_function_with_layer_reqs_when_build_then_layer_pkgs_not_installed(self):
        """
        Given a function whose requirements.txt lists a layer-provided package
        (requests, already in the observability layer manifest),
        when build() is called,
        then the staging directory does NOT contain the requests package files.
        """
        src_root, build_dir = self._make_src_root({
            "functions/my_func/handler.py": "import requests",
            "functions/my_func/requirements.txt": "requests==2.32.3\n",
        })

        rc = build("my_func", src_root, build_dir)

        self.assertEqual(rc, 0)
        staging = build_dir / "my_func"
        # requests package should NOT be installed in the staging dir
        self.assertFalse(
            (staging / "requests").is_dir(),
            "requests package should be excluded (provided by observability layer)",
        )

    def test_given_function_with_no_reqs_when_build_then_still_succeeds(self):
        src_root, build_dir = self._make_src_root({
            "functions/my_func/handler.py": "def handler(e, c): pass",
        })
        rc = build("my_func", src_root, build_dir)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
