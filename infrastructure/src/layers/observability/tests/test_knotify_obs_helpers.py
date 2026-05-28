"""
Unit tests for init_logger and correlation_id_middleware helpers.

Story 3.2 AC: knotify_obs exposes helpers init_logger(service),
correlation_id_middleware, verify_cognito_jwt(token, user_pool_id, region).
"""

import unittest


class TestInitLogger(unittest.TestCase):
    """
    Given a service name string,
    when init_logger is called,
    then it returns a Logger instance bound to that service name.
    """

    def test_given_service_name_when_init_logger_then_returns_bound_logger(self):
        from knotify_obs import init_logger

        logger = init_logger("knotify-test-service")

        # aws_lambda_powertools Logger exposes a service attribute
        self.assertEqual(logger.service, "knotify-test-service")

    def test_given_different_service_names_when_init_logger_then_each_has_own_service(self):
        from knotify_obs import init_logger

        logger_a = init_logger("service-a")
        logger_b = init_logger("service-b")

        self.assertEqual(logger_a.service, "service-a")
        self.assertEqual(logger_b.service, "service-b")


class TestCorrelationIdMiddleware(unittest.TestCase):
    """
    Given the correlation_id_middleware export,
    when imported from knotify_obs,
    then it is the Powertools correlation_id_logger_handler (callable or
    the Powertools middleware object — either way it must be importable and
    not None so consumers can apply it as a decorator).
    """

    def test_correlation_id_middleware_is_importable_and_not_none(self):
        from knotify_obs import correlation_id_middleware

        self.assertIsNotNone(correlation_id_middleware)

    def test_correlation_id_middleware_is_callable(self):
        from knotify_obs import correlation_id_middleware

        self.assertTrue(callable(correlation_id_middleware))


class TestModulePublicApi(unittest.TestCase):
    """
    Given the knotify_obs module,
    when imported,
    then it exposes exactly the three helpers named in the AC.
    """

    def test_all_three_helpers_are_importable(self):
        from knotify_obs import init_logger, correlation_id_middleware, verify_cognito_jwt

        self.assertIsNotNone(init_logger)
        self.assertIsNotNone(correlation_id_middleware)
        self.assertIsNotNone(verify_cognito_jwt)


if __name__ == "__main__":
    unittest.main()
