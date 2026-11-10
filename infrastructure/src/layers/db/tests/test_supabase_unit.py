"""
Unit tests for knotify_db._supabase — Supabase connection builder (story 14.8).

Tests that do NOT require a running database or live AWS account.  All calls
to psycopg2.connect and boto3.client are mocked at the module boundary.

Contracts verified:

Dict path:
  - All five connection fields (host, port, dbname, username, password) are
    forwarded to psycopg2.connect as keyword arguments.
  - sslmode="require" is always passed (Supabase enforces TLS at the pooler;
    plaintext connections fail).
  - port is coerced to int() (matches _db.py behaviour).
  - boto3 is NOT called on the dict path.

Secrets Manager path:
  - boto3.client("secretsmanager").get_secret_value(SecretId=...) is called
    with the input ARN/name.
  - The returned SecretString JSON is parsed; username + password are
    extracted and passed to psycopg2.connect.
  - SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DBNAME are read from os.environ
    and forwarded to psycopg2.connect.
  - A missing SUPABASE_HOST / SUPABASE_PORT / SUPABASE_DBNAME env var raises
    EnvironmentError with the missing var name in the message.
  - sslmode="require" is always passed.

SSoT identity proofs (sole-source-of-truth invariant):
  - _supabase.set_rls_context IS _rls.set_rls_context (same object).
  - _supabase.rls_context IS _rls.rls_context (same object).
  - _supabase._GUC_USER_ID IS _rls._GUC_USER_ID (same object).
  - _supabase._GUC_USER_SEX IS _rls._GUC_USER_SEX (same object).
"""

import unittest
from unittest.mock import MagicMock, patch


class TestGetConnectionDictPath(unittest.TestCase):
    """
    Given get_connection called with a dict containing all five fields,
    when invoked,
    then psycopg2.connect is called with those fields as kwargs, sslmode=require,
    and port coerced to int.  boto3 is never touched.
    """

    def _make_params(self, port=5432):
        return {
            "host": "db.supabase.co",
            "port": port,
            "dbname": "postgres",
            "username": "postgres.abc123",
            "password": "hunter2",
        }

    def test_dict_path_forwards_host_to_connect(self):
        from knotify_db._supabase import get_connection

        params = self._make_params()

        with patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(params)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["host"], "db.supabase.co")

    def test_dict_path_forwards_dbname_to_connect(self):
        from knotify_db._supabase import get_connection

        params = self._make_params()

        with patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(params)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["dbname"], "postgres")

    def test_dict_path_forwards_username_as_user_kwarg(self):
        """username dict key maps to user= kwarg in psycopg2.connect."""
        from knotify_db._supabase import get_connection

        params = self._make_params()

        with patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(params)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["user"], "postgres.abc123")

    def test_dict_path_forwards_password_to_connect(self):
        from knotify_db._supabase import get_connection

        params = self._make_params()

        with patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(params)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["password"], "hunter2")

    def test_dict_path_coerces_port_to_int(self):
        """port must be passed as int, not str, to psycopg2.connect."""
        from knotify_db._supabase import get_connection

        params = self._make_params(port="6543")  # string port from env

        with patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(params)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertIsInstance(call_kwargs["port"], int)
            self.assertEqual(call_kwargs["port"], 6543)

    def test_dict_path_passes_sslmode_require(self):
        """
        sslmode=require must be passed to psycopg2.connect on the dict path.
        Supabase enforces TLS at the pooler — plaintext connections are
        rejected by the pooler before reaching the database.
        """
        from knotify_db._supabase import get_connection

        params = self._make_params()

        with patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(params)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs.get("sslmode"), "require")

    def test_dict_path_does_not_call_boto3(self):
        """boto3 must NOT be imported or called on the dict path."""
        from knotify_db._supabase import get_connection

        params = self._make_params()

        with patch("knotify_db._supabase.psycopg2.connect"), \
             patch("boto3.client") as mock_boto:
            get_connection(params)
            mock_boto.assert_not_called()


class TestGetConnectionSecretsManagerPath(unittest.TestCase):
    """
    Given get_connection called with a string (Secrets Manager ARN or name),
    when invoked with SUPABASE_HOST / PORT / DBNAME set in the environment,
    then boto3 is called, the secret is parsed, env vars are read, and
    psycopg2.connect receives all kwargs including sslmode=require.
    """

    _SECRET_ARN = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:knotify/dev/supabase_credential-AbCdEf"
    _SECRET_PAYLOAD = '{"username": "postgres.proj123", "password": "s3cr3t"}'
    _ENV = {
        "SUPABASE_HOST": "aws-0-eu-central-1.pooler.supabase.com",
        "SUPABASE_PORT": "6543",
        "SUPABASE_DBNAME": "postgres",
    }

    def _make_boto_client(self):
        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretString": self._SECRET_PAYLOAD}
        return fake_client

    def test_secrets_path_calls_boto3_secretsmanager(self):
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client) as mock_boto, \
             patch("knotify_db._supabase.psycopg2.connect"):
            get_connection(self._SECRET_ARN)
            mock_boto.assert_called_once_with("secretsmanager")

    def test_secrets_path_calls_get_secret_value_with_secret_id(self):
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect"):
            get_connection(self._SECRET_ARN)
            fake_client.get_secret_value.assert_called_once_with(
                SecretId=self._SECRET_ARN
            )

    def test_secrets_path_passes_username_from_secret(self):
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(self._SECRET_ARN)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["user"], "postgres.proj123")

    def test_secrets_path_passes_password_from_secret(self):
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(self._SECRET_ARN)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["password"], "s3cr3t")

    def test_secrets_path_passes_supabase_host_from_env(self):
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(self._SECRET_ARN)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["host"], "aws-0-eu-central-1.pooler.supabase.com")

    def test_secrets_path_passes_supabase_port_from_env_as_int(self):
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(self._SECRET_ARN)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertIsInstance(call_kwargs["port"], int)
            self.assertEqual(call_kwargs["port"], 6543)

    def test_secrets_path_passes_supabase_dbname_from_env(self):
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(self._SECRET_ARN)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs["dbname"], "postgres")

    def test_secrets_path_passes_sslmode_require(self):
        """
        sslmode=require on the Secrets Manager path too.
        """
        from knotify_db._supabase import get_connection

        fake_client = self._make_boto_client()

        with patch.dict("os.environ", self._ENV, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect") as mock_connect:
            get_connection(self._SECRET_ARN)
            call_kwargs = mock_connect.call_args.kwargs
            self.assertEqual(call_kwargs.get("sslmode"), "require")

    def test_secrets_path_raises_environment_error_when_supabase_host_missing(self):
        """
        Missing SUPABASE_HOST raises EnvironmentError with 'SUPABASE_HOST' in
        the message so misconfigured Lambdas fail loudly at startup.
        """
        from knotify_db._supabase import get_connection

        env_missing_host = {
            "SUPABASE_PORT": "6543",
            "SUPABASE_DBNAME": "postgres",
        }
        fake_client = self._make_boto_client()

        with patch.dict("os.environ", env_missing_host, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect"):
            with self.assertRaises(EnvironmentError) as ctx:
                get_connection(self._SECRET_ARN)
            self.assertIn("SUPABASE_HOST", str(ctx.exception))

    def test_secrets_path_raises_environment_error_when_supabase_port_missing(self):
        """Missing SUPABASE_PORT raises EnvironmentError with 'SUPABASE_PORT' in message."""
        from knotify_db._supabase import get_connection

        env_missing_port = {
            "SUPABASE_HOST": "aws-0-eu-central-1.pooler.supabase.com",
            "SUPABASE_DBNAME": "postgres",
        }
        fake_client = self._make_boto_client()

        with patch.dict("os.environ", env_missing_port, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect"):
            with self.assertRaises(EnvironmentError) as ctx:
                get_connection(self._SECRET_ARN)
            self.assertIn("SUPABASE_PORT", str(ctx.exception))

    def test_secrets_path_raises_environment_error_when_supabase_dbname_missing(self):
        """Missing SUPABASE_DBNAME raises EnvironmentError with 'SUPABASE_DBNAME' in message."""
        from knotify_db._supabase import get_connection

        env_missing_dbname = {
            "SUPABASE_HOST": "aws-0-eu-central-1.pooler.supabase.com",
            "SUPABASE_PORT": "6543",
        }
        fake_client = self._make_boto_client()

        with patch.dict("os.environ", env_missing_dbname, clear=True), \
             patch("boto3.client", return_value=fake_client), \
             patch("knotify_db._supabase.psycopg2.connect"):
            with self.assertRaises(EnvironmentError) as ctx:
                get_connection(self._SECRET_ARN)
            self.assertIn("SUPABASE_DBNAME", str(ctx.exception))


class TestRlsIdentityReExport(unittest.TestCase):
    """
    SSoT proof: RLS symbols in _supabase are the SAME objects as in _rls.

    _supabase.py imports (not redefines) the four RLS symbols from _rls.
    Object identity assertions prove there is exactly one implementation
    of each RLS symbol in the codebase.  A one-character deviation in a
    GUC constant silently fail-closes the RLS policy to zero rows.
    """

    def test_supabase_set_rls_context_is_rls_set_rls_context(self):
        """
        _supabase.set_rls_context must be the SAME object as
        _rls.set_rls_context — imported, not redefined.
        """
        import knotify_db._supabase as supabase
        import knotify_db._rls as rls

        self.assertIs(
            supabase.set_rls_context,
            rls.set_rls_context,
            "knotify_db._supabase.set_rls_context must be the SAME object as "
            "knotify_db._rls.set_rls_context (identity import, not re-implementation)",
        )

    def test_supabase_rls_context_is_rls_rls_context(self):
        """
        _supabase.rls_context must be the SAME object as _rls.rls_context.
        """
        import knotify_db._supabase as supabase
        import knotify_db._rls as rls

        self.assertIs(
            supabase.rls_context,
            rls.rls_context,
            "knotify_db._supabase.rls_context must be the SAME object as "
            "knotify_db._rls.rls_context (identity import, not re-implementation)",
        )

    def test_supabase_guc_user_id_is_rls_guc_user_id(self):
        """
        _supabase._GUC_USER_ID must be the SAME object as _rls._GUC_USER_ID.
        Strings interned by Python at module load — the `is` check is valid here.
        """
        import knotify_db._supabase as supabase
        import knotify_db._rls as rls

        self.assertIs(
            supabase._GUC_USER_ID,
            rls._GUC_USER_ID,
            "knotify_db._supabase._GUC_USER_ID must be the SAME object as "
            "knotify_db._rls._GUC_USER_ID (identity import, not re-implementation)",
        )

    def test_supabase_guc_user_sex_is_rls_guc_user_sex(self):
        """
        _supabase._GUC_USER_SEX must be the SAME object as _rls._GUC_USER_SEX.
        """
        import knotify_db._supabase as supabase
        import knotify_db._rls as rls

        self.assertIs(
            supabase._GUC_USER_SEX,
            rls._GUC_USER_SEX,
            "knotify_db._supabase._GUC_USER_SEX must be the SAME object as "
            "knotify_db._rls._GUC_USER_SEX (identity import, not re-implementation)",
        )


if __name__ == "__main__":
    unittest.main()
