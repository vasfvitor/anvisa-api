"""Client for ANVISA's official Consultas Externas API."""

__version__ = "0.1.0"

from .auth import Credentials  # noqa: E402  (client.py reads __version__ at import time)
from .client import Client  # noqa: E402
from .errors import (  # noqa: E402
    AnvisaError,
    ApiError,
    AuthError,
    BlockedError,
    CredentialsError,
    InvalidPageError,
    MalformedRequestError,
    MissingFilterError,
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
    "InvalidPageError",
    "MalformedRequestError",
    "MissingFilterError",
    "NotFoundError",
    "RateLimitError",
    "RequestRejectedError",
    "Throttle",
    "__version__",
]
