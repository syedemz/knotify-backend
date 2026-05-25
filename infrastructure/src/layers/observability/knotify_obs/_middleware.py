"""Correlation ID middleware for Lambda handlers."""

from aws_lambda_powertools import Logger
from aws_lambda_powertools.logging import correlation_paths

# A module-level logger used by the middleware to stamp the correlation ID.
# The service name falls back to the POWERTOOLS_SERVICE_NAME env var which
# the Lambda module (story 3.1) always sets.
_logger = Logger()


def correlation_id_middleware(handler):
    """Decorator that injects the HTTP API request ID as a correlation ID.

    Usage:
        @correlation_id_middleware
        def lambda_handler(event, context):
            ...

    The correlation ID is sourced from the API Gateway HTTP request context
    (event.requestContext.requestId).  When no request ID is present (e.g. in
    unit tests or direct invocations) the middleware is a no-op pass-through.
    """
    import functools

    @functools.wraps(handler)
    def wrapper(event, context):
        # inject_lambda_context with the API Gateway HTTP correlation path
        # stamps every subsequent log record with requestId.
        decorated = _logger.inject_lambda_context(
            handler,
            correlation_id_path=correlation_paths.API_GATEWAY_HTTP,
        )
        return decorated(event, context)

    return wrapper
