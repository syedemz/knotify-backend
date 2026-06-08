"""
Unit tests for require_edge_secret and with_edge_secret.

Story 5.6 AC: knotify_obs exposes EdgeSecretRequired exception,
require_edge_secret(event) function, and with_edge_secret decorator.
"""

import json
import os
import unittest


class TestRequireEdgeSecretMissingHeader(unittest.TestCase):
    """
    Given an event with no headers at all,
    when require_edge_secret is called,
    then EdgeSecretRequired is raised.
    """

    def test_given_no_headers_when_require_edge_secret_then_raises(self):
        from knotify_obs import EdgeSecretRequired, require_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            with self.assertRaises(EdgeSecretRequired):
                require_edge_secret({})
        finally:
            del os.environ["EDGE_SECRET"]

    def test_given_headers_without_edge_secret_key_when_require_edge_secret_then_raises(self):
        from knotify_obs import EdgeSecretRequired, require_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            event = {"headers": {"content-type": "application/json"}}
            with self.assertRaises(EdgeSecretRequired):
                require_edge_secret(event)
        finally:
            del os.environ["EDGE_SECRET"]


class TestRequireEdgeSecretWrongValue(unittest.TestCase):
    """
    Given an event with x-knotify-edge-secret set to a wrong value,
    when require_edge_secret is called,
    then EdgeSecretRequired is raised.
    """

    def test_given_wrong_secret_when_require_edge_secret_then_raises(self):
        from knotify_obs import EdgeSecretRequired, require_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            event = {"headers": {"x-knotify-edge-secret": "wrong-secret"}}
            with self.assertRaises(EdgeSecretRequired):
                require_edge_secret(event)
        finally:
            del os.environ["EDGE_SECRET"]


class TestRequireEdgeSecretCorrectValue(unittest.TestCase):
    """
    Given an event with the correct x-knotify-edge-secret header (any casing),
    when require_edge_secret is called,
    then None is returned (no exception).
    """

    def test_given_correct_lowercase_header_when_require_edge_secret_then_returns_none(self):
        from knotify_obs import require_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            event = {"headers": {"x-knotify-edge-secret": "correct-secret"}}
            result = require_edge_secret(event)
            self.assertIsNone(result)
        finally:
            del os.environ["EDGE_SECRET"]

    def test_given_correct_uppercase_header_key_when_require_edge_secret_then_returns_none(self):
        from knotify_obs import require_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            # HTTP API Gateway can deliver headers in any casing
            event = {"headers": {"X-Knotify-Edge-Secret": "correct-secret"}}
            result = require_edge_secret(event)
            self.assertIsNone(result)
        finally:
            del os.environ["EDGE_SECRET"]

    def test_given_correct_mixed_case_header_key_when_require_edge_secret_then_returns_none(self):
        from knotify_obs import require_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            event = {"headers": {"X-KNOTIFY-EDGE-SECRET": "correct-secret"}}
            result = require_edge_secret(event)
            self.assertIsNone(result)
        finally:
            del os.environ["EDGE_SECRET"]


class TestRequireEdgeSecretEnvVarUnset(unittest.TestCase):
    """
    Given the EDGE_SECRET environment variable is not set,
    when require_edge_secret is called,
    then EdgeSecretRequired is raised (treated as a Lambda config error).
    """

    def test_given_edge_secret_env_unset_when_require_edge_secret_then_raises(self):
        from knotify_obs import EdgeSecretRequired, require_edge_secret

        # Ensure EDGE_SECRET is definitely not set
        os.environ.pop("EDGE_SECRET", None)
        event = {"headers": {"x-knotify-edge-secret": "any-value"}}
        with self.assertRaises(EdgeSecretRequired):
            require_edge_secret(event)

    def test_given_edge_secret_env_empty_string_when_require_edge_secret_then_raises(self):
        from knotify_obs import EdgeSecretRequired, require_edge_secret

        os.environ["EDGE_SECRET"] = ""
        try:
            event = {"headers": {"x-knotify-edge-secret": ""}}
            with self.assertRaises(EdgeSecretRequired):
                require_edge_secret(event)
        finally:
            del os.environ["EDGE_SECRET"]


class TestWithEdgeSecretDecoratorPassthrough(unittest.TestCase):
    """
    Given a handler decorated with @with_edge_secret and an event containing
    the correct secret,
    when the handler is invoked,
    then the handler runs normally and returns its own response.
    """

    def test_given_correct_secret_when_handler_invoked_then_returns_handler_response(self):
        from knotify_obs import with_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            @with_edge_secret
            def handler(event, context):
                return {"statusCode": 200, "body": json.dumps({"ok": True})}

            event = {"headers": {"x-knotify-edge-secret": "correct-secret"}}
            result = handler(event, None)
            self.assertEqual(result["statusCode"], 200)
            self.assertEqual(json.loads(result["body"]), {"ok": True})
        finally:
            del os.environ["EDGE_SECRET"]


class TestWithEdgeSecretDecoratorForbidden(unittest.TestCase):
    """
    Given a handler decorated with @with_edge_secret and an event with a wrong
    or missing secret,
    when the handler is invoked,
    then the handler body is NOT executed and a 403 response dict is returned.
    """

    def test_given_wrong_secret_when_handler_invoked_then_returns_403(self):
        from knotify_obs import with_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            handler_executed = []

            @with_edge_secret
            def handler(event, context):
                handler_executed.append(True)
                return {"statusCode": 200, "body": json.dumps({"ok": True})}

            event = {"headers": {"x-knotify-edge-secret": "wrong-secret"}}
            result = handler(event, None)

            self.assertEqual(result["statusCode"], 403)
            self.assertEqual(json.loads(result["body"]), {"error": "forbidden"})
            self.assertEqual(result["headers"]["Content-Type"], "application/json")
            self.assertEqual(handler_executed, [], "handler body must not be executed on 403")
        finally:
            del os.environ["EDGE_SECRET"]

    def test_given_missing_secret_header_when_handler_invoked_then_returns_403(self):
        from knotify_obs import with_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            @with_edge_secret
            def handler(event, context):
                return {"statusCode": 200}

            result = handler({}, None)

            self.assertEqual(result["statusCode"], 403)
            self.assertEqual(json.loads(result["body"]), {"error": "forbidden"})
            self.assertEqual(result["headers"]["Content-Type"], "application/json")
        finally:
            del os.environ["EDGE_SECRET"]


class TestWithEdgeSecretDecoratorOtherExceptionsPropagated(unittest.TestCase):
    """
    Given a handler decorated with @with_edge_secret and an event with the
    correct secret, but the handler raises a non-EdgeSecretRequired exception,
    when the handler is invoked,
    then the exception propagates — the decorator does NOT swallow it.
    """

    def test_given_correct_secret_but_handler_raises_when_invoked_then_exception_propagates(self):
        from knotify_obs import with_edge_secret

        os.environ["EDGE_SECRET"] = "correct-secret"
        try:
            @with_edge_secret
            def handler(event, context):
                raise ValueError("something went wrong inside the handler")

            event = {"headers": {"x-knotify-edge-secret": "correct-secret"}}
            with self.assertRaises(ValueError) as ctx:
                handler(event, None)
            self.assertIn("something went wrong inside the handler", str(ctx.exception))
        finally:
            del os.environ["EDGE_SECRET"]


class TestModuleExports(unittest.TestCase):
    """
    Given the knotify_obs module,
    when imported,
    then all three new symbols are importable.
    """

    def test_edge_secret_symbols_are_importable(self):
        from knotify_obs import EdgeSecretRequired, require_edge_secret, with_edge_secret

        self.assertIsNotNone(EdgeSecretRequired)
        self.assertIsNotNone(require_edge_secret)
        self.assertIsNotNone(with_edge_secret)
        self.assertTrue(callable(require_edge_secret))
        self.assertTrue(callable(with_edge_secret))
        self.assertTrue(issubclass(EdgeSecretRequired, Exception))


if __name__ == "__main__":
    unittest.main()
