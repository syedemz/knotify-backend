"""
_smoke — minimal Lambda used only by `make package-test` to verify the
make package target produces a well-formed zip before any real function
(3.6, 3.7) depends on it.

This function has no layer dependencies and no function-specific pip
requirements, so its deployment zip contains only handler.py itself.
It is never deployed to AWS.
"""


def handler(event: dict, context: object) -> dict:
    """Return a fixed health response — proves the zip is valid Python."""
    return {"statusCode": 200, "body": "smoke ok"}
