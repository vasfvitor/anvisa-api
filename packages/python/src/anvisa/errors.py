"""Exceptions, and the mapping from ANVISA's HTTP 500 error envelope to typed errors.

ANVISA returns request-validation failures (missing filter, page < 1, malformed body)
as HTTP 500 with the same envelope as internal errors, so the only way to tell them
apart is the message text. The patterns below were recorded live (fixtures/err_*.json).
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx


class AnvisaError(Exception):
    """Base class for everything this library raises."""


class CredentialsError(AnvisaError):
    """No client id/secret could be found."""


class AuthError(AnvisaError):
    """Token request failed or the API answered 401."""


class BlockedError(AnvisaError):
    """Cloudflare rejected the request (HTTP 403 with an HTML body)."""


class RateLimitError(AnvisaError):
    """HTTP 429: the per-client token bucket is empty."""


class NotFoundError(AnvisaError):
    """HTTP 404."""


class ApiError(AnvisaError):
    """The API answered with its error envelope (or a non-JSON error body)."""

    def __init__(
        self,
        mensagem: str,
        *,
        status_code: int | None = None,
        mensagem_detalhada: str = "",
        data_hora: datetime | None = None,
    ) -> None:
        self.mensagem = mensagem
        self.status_code = status_code
        self.mensagem_detalhada = mensagem_detalhada
        self.data_hora = data_hora
        text = f"HTTP {status_code}: {mensagem}" if status_code else mensagem
        if mensagem_detalhada:
            text += f" — {mensagem_detalhada}"
        super().__init__(text)


class RequestRejectedError(ApiError):
    """A validation failure that ANVISA reports as HTTP 500."""


class MissingFilterError(RequestRejectedError):
    """A required `filter` key was not sent (`filters` names them when known)."""

    def __init__(self, mensagem: str, *, filters: list[str] | None = None, **kwargs) -> None:
        super().__init__(mensagem, **kwargs)
        self.filters = filters or []


class InvalidPageError(RequestRejectedError):
    """`page` must be >= 1 (requests are 1-based)."""


class MalformedRequestError(RequestRejectedError):
    """The server could not deserialize the body (e.g. `sorting` sent as an array)."""


class NotAcceptableError(RequestRejectedError):
    """A download endpoint was asked for a representation it cannot produce.

    Every download but `GET /udi/{id}/download` refuses `Accept: application/json`. The
    client sends `Accept: */*` on downloads, so this only reaches callers who build their
    own request."""


class EmptyExportError(RequestRejectedError):
    """There was nothing to export (an empty subfila or sublista).

    The JSON siblings answer an empty-bodied 404 for the same id; the download endpoints
    raise instead."""


class NoResultError(RequestRejectedError):
    """The id in the request matches no row (`javax.persistence.NoResultException`).

    Observed for an unknown formulário id on `POST /assunto/downloadAssuntoFormulario`.
    A plain HTTP 404 stays `NotFoundError`; this is ANVISA's 500-shaped version."""


_MISSING_FILTER = re.compile(r"Filtro '(\w+)' não informado")


def parse_data_hora(parts: object) -> datetime | None:
    """`[year, month, day, hour, minute, second, nanosecond]` -> naive server-local datetime."""
    if not isinstance(parts, list) or len(parts) < 6:
        return None
    y, mo, d, h, mi, s = parts[:6]
    ns = parts[6] if len(parts) > 6 else 0
    try:
        return datetime(y, mo, d, h, mi, s, ns // 1000)
    except (TypeError, ValueError):
        return None


def raise_for_response(response: httpx.Response) -> None:
    """Raise the typed exception for a non-2xx response; return otherwise."""
    if response.is_success:
        return
    code = response.status_code
    content_type = response.headers.get("content-type", "")
    if code == 401:
        raise AuthError("HTTP 401: token rejected")
    if code == 403 and "text/html" in content_type:
        raise BlockedError(
            "HTTP 403 from Cloudflare: the request was blocked before reaching ANVISA. "
            "Send a descriptive User-Agent header."
        )
    if code == 404:
        raise NotFoundError(f"HTTP 404: {response.request.url.path}")
    if code == 429:
        raise RateLimitError("HTTP 429: rate limit exceeded (25 burst, 1 request/s)")

    try:
        body = response.json()
    except ValueError:
        raise ApiError(response.text[:200].strip() or "empty body", status_code=code) from None
    if not isinstance(body, dict):
        raise ApiError(str(body)[:200], status_code=code)

    mensagem = str(body.get("mensagem") or "")
    detail = str(body.get("mensagem_detalhada") or "")
    common = {
        "status_code": code,
        "mensagem_detalhada": detail,
        "data_hora": parse_data_hora(body.get("data_hora")),
    }

    if mensagem == "mensagens.MSG-062":
        raise MissingFilterError(
            "at least one filter key is required (mensagens.MSG-062)", **common
        )
    if match := _MISSING_FILTER.search(detail):
        raise MissingFilterError(mensagem, filters=[match.group(1)], **common)
    if detail == "Page index must not be less than zero!":
        raise InvalidPageError(mensagem, **common)
    if detail.startswith("com.fasterxml.jackson"):
        raise MalformedRequestError(mensagem, **common)
    if detail == "Could not find acceptable representation":
        raise NotAcceptableError(
            "this endpoint does not produce JSON; send Accept: */* "
            "(the client already does for every download)",
            **common,
        )
    if detail == "Nenhum resultado encontrado para exportação.":
        raise EmptyExportError("nothing to export for these filters", **common)
    if detail.startswith("javax.persistence.NoResultException"):
        raise NoResultError("no row matches that id", **common)
    raise ApiError(mensagem or "unknown error", **common)
