"""Client for ANVISA's official Consultas Externas API."""

__version__ = "0.4.0"

from .auth import Credentials  # noqa: E402  (client.py reads __version__ at import time)
from .client import Client  # noqa: E402
from .download import Download  # noqa: E402
from .errors import (  # noqa: E402
    AnvisaError,
    ApiError,
    AuthError,
    BlockedError,
    CredentialsError,
    EmptyExportError,
    InvalidPageError,
    MalformedRequestError,
    MissingFilterError,
    NoResultError,
    NotAcceptableError,
    NotFoundError,
    RateLimitError,
    RequestRejectedError,
)
from .throttle import Throttle  # noqa: E402

__all__ = [
    "AnvisaError",
    "ApiError",
    "AuthError",
    "BlockedError",
    "Client",
    "Credentials",
    "CredentialsError",
    "Download",
    "EmptyExportError",
    "InvalidPageError",
    "MalformedRequestError",
    "MissingFilterError",
    "NoResultError",
    "NotAcceptableError",
    "NotFoundError",
    "RateLimitError",
    "RequestRejectedError",
    "Throttle",
    "__version__",
]
