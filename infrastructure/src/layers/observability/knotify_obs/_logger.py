"""Logger initialisation helper."""

from aws_lambda_powertools import Logger


def init_logger(service: str) -> Logger:
    """Return a Powertools Logger bound to the given service name.

    The POWERTOOLS_SERVICE_NAME and LOG_LEVEL environment variables are
    injected by the Lambda module (story 3.1 AC4), so in production the
    service name is already set.  This helper lets consumers create a logger
    explicitly bound to a specific service string regardless of the env var
    value — useful in unit tests and when a single deployment package handles
    multiple logical services.
    """
    return Logger(service=service)
