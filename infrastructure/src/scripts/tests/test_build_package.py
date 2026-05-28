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
    _copy_include_dir,
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


# ---------------------------------------------------------------------------
# _copy_include_dir  (M-new-1)
# ---------------------------------------------------------------------------

class TestCopyIncludeDir(unittest.TestCase):
    """
    Given the _copy_include_dir helper,
    when called with src_dir, dest_dir inside staging, and a file regex,
    then matching files are copied under dest_dir inside staging and
    non-matching files are excluded.
    """

    def _make_src(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        for name, content in files.items():
            (d / name).write_text(content)
        return d

    def test_matching_sql_files_are_copied(self):
        src = self._make_src({
            "0001_init.sql": "SELECT 1;",
            "0002_users.sql": "SELECT 2;",
        })
        staging = Path(tempfile.mkdtemp())
        dest_subdir = "migrations"

        _copy_include_dir(src, staging, dest_subdir, r"^\d{4}_.+\.sql$")

        dest = staging / dest_subdir
        self.assertTrue(dest.is_dir())
        self.assertIn("0001_init.sql", [f.name for f in dest.iterdir()])
        self.assertIn("0002_users.sql", [f.name for f in dest.iterdir()])

    def test_rollback_sql_files_are_copied(self):
        src = self._make_src({
            "0001_init.rollback.sql": "DROP TABLE IF EXISTS t;",
        })
        staging = Path(tempfile.mkdtemp())

        _copy_include_dir(src, staging, "migrations", r"^\d{4}_.+\.(rollback\.)?sql$")

        dest = staging / "migrations"
        self.assertIn("0001_init.rollback.sql", [f.name for f in dest.iterdir()])

    def test_non_matching_files_are_excluded(self):
        src = self._make_src({
            "local_init.sql": "ALTER ROLE app_user WITH PASSWORD 'x';",
            "0001_init.sql": "SELECT 1;",
        })
        staging = Path(tempfile.mkdtemp())

        _copy_include_dir(src, staging, "migrations", r"^\d{4}_.+\.sql$")

        dest = staging / "migrations"
        names = [f.name for f in dest.iterdir()]
        self.assertIn("0001_init.sql", names)
        self.assertNotIn("local_init.sql", names)

    def test_dest_subdir_created_if_missing(self):
        src = self._make_src({"0001_init.sql": "SELECT 1;"})
        staging = Path(tempfile.mkdtemp())

        _copy_include_dir(src, staging, "deep/nested/migrations", r"^\d{4}_.+\.sql$")

        self.assertTrue((staging / "deep" / "nested" / "migrations").is_dir())


class TestBuildWithIncludeDir(unittest.TestCase):
    """
    Given the build() function with --include-dir arguments,
    when called for the db_migrator function with a migrations src directory,
    then the zip contains every migration .sql file and does NOT contain
    local_init.sql (which lives outside the migrations/ folder).
    """

    def _make_src_root_with_migrations(
        self, migration_files: dict[str, str], extra_files: dict[str, str] | None = None
    ) -> tuple[Path, Path, Path]:
        """
        Returns (src_root, build_dir, migrations_dir) with:
          src_root/functions/db_migrator/handler.py
          migrations_dir/<migration_files>
        """
        root = Path(tempfile.mkdtemp())
        src_root = root / "src"
        build_dir = root / "build"
        build_dir.mkdir()
        migrations_dir = root / "migrations"
        migrations_dir.mkdir()

        # layer manifests
        obs_manifest = src_root / "layers" / "observability" / "layer_manifest.txt"
        obs_manifest.parent.mkdir(parents=True)
        obs_manifest.write_text("aws-lambda-powertools\nPyJWT\n")

        db_manifest = src_root / "layers" / "db" / "layer_manifest.txt"
        db_manifest.parent.mkdir(parents=True)
        db_manifest.write_text("psycopg2-binary\npgvector\nyoyo-migrations\n")

        # function
        func_dir = src_root / "functions" / "db_migrator"
        func_dir.mkdir(parents=True)
        (func_dir / "handler.py").write_text("def handler(e, c): pass")

        # migration files
        for name, content in migration_files.items():
            (migrations_dir / name).write_text(content)

        if extra_files:
            for name, content in extra_files.items():
                (migrations_dir / name).write_text(content)

        return src_root, build_dir, migrations_dir

    def test_given_migrations_when_build_with_include_dir_then_sql_files_in_zip(self):
        """
        Given migration .sql files in a separate directory,
        when build() is called with include_dirs pointing at that directory,
        then the zip contains those files under the 'migrations' sub-path.
        """
        src_root, build_dir, migrations_dir = self._make_src_root_with_migrations(
            {
                "0001_init.sql": "SELECT 1;",
                "0002_users.sql": "SELECT 2;",
                "0001_init.rollback.sql": "DROP TABLE t;",
            }
        )

        rc = build(
            "db_migrator",
            src_root,
            build_dir,
            include_dirs=[(migrations_dir, "migrations", r"^\d{4}_.+\.(rollback\.)?sql$")],
        )

        self.assertEqual(rc, 0)
        with zipfile.ZipFile(build_dir / "db_migrator.zip") as zf:
            names = zf.namelist()
        self.assertIn("migrations/0001_init.sql", names)
        self.assertIn("migrations/0002_users.sql", names)
        self.assertIn("migrations/0001_init.rollback.sql", names)

    def test_given_local_init_sql_outside_migrations_when_build_then_not_in_zip(self):
        """
        local_init.sql must never appear in the zip — it is a local-dev-only
        file that lives outside the migrations/ directory.
        """
        src_root, build_dir, migrations_dir = self._make_src_root_with_migrations(
            {"0001_init.sql": "SELECT 1;"},
            # local_init.sql in the same migrations_dir (edge case — in reality
            # it lives outside migrations/ and would not be in the src_dir at all)
            extra_files={"local_init.sql": "ALTER ROLE app_user WITH PASSWORD 'x';"},
        )

        rc = build(
            "db_migrator",
            src_root,
            build_dir,
            include_dirs=[(migrations_dir, "migrations", r"^\d{4}_.+\.(rollback\.)?sql$")],
        )

        self.assertEqual(rc, 0)
        with zipfile.ZipFile(build_dir / "db_migrator.zip") as zf:
            names = zf.namelist()
        self.assertNotIn("migrations/local_init.sql", names)
        self.assertNotIn("local_init.sql", names)

    def test_given_no_include_dirs_when_build_then_still_succeeds(self):
        """
        Functions with no include_dirs (all current functions except db_migrator)
        must still build successfully when no include_dirs are passed.
        """
        src_root, build_dir, _ = self._make_src_root_with_migrations({})
        rc = build("db_migrator", src_root, build_dir)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
