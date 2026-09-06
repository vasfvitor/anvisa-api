"""The HTTP client and the two API domains shipped in 0.1: `fila` and `udi`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Protocol, TypeVar

import httpx

from . import __version__, models
from .auth import Credentials, TokenAuth
from .errors import InvalidPageError, MissingFilterError, raise_for_response
from .throttle import Throttle

BASE_URL = "https://api-gateway.prd.apps.anvisa.gov.br/consultas-externas-api"
USER_AGENT = f"anvisa-python/{__version__} (+https://github.com/virtuaires/anvisa)"


class Client:
    """Sync client. Use as a context manager or call `close()`.

    >>> with Client.from_env() as anvisa:
    ...     anvisa.fila.areas()
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        user_agent: str = USER_AGENT,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        self.credentials = credentials or Credentials.from_env()
        self.throttle = throttle or Throttle()
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            auth=TokenAuth(self.credentials),
        )
        self.fila = Fila(self)
        self.udi = Udi(self)

    @classmethod
    def from_env(cls, **kwargs: Any) -> Client:
        return cls(Credentials.from_env(), **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, path: str) -> Any:
        return self._send(self._http.build_request("GET", path))

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._send(self._http.build_request("POST", path, json=body))

    def _send(self, request: httpx.Request) -> Any:
        self.throttle.before()
        response = self._http.send(request)
        self.throttle.after(response.headers)
        raise_for_response(response)
        return response.json()


def page_body(
    page: int, size: int, sort: dict[str, str] | None, filters: dict[str, Any]
) -> dict[str, Any]:
    """Build ANVISA's `PaginationBuilder` body. Pages are 1-based on the wire."""
    if page < 1:
        raise InvalidPageError(f"page must be >= 1 (got {page}); ANVISA pages are 1-based")
    return {"page": page, "size": size, "sorting": dict(sort or {}), "filter": dict(filters)}


T = TypeVar("T")


class PageLike(Protocol[T]):
    content: list[T] | None
    number: int | None
    last: bool | None


def iterate_pages(fetch: Callable[[int], PageLike[T]]) -> Iterator[T]:
    """Yield items across pages. `fetch` takes a 1-based page; responses report 0-based `number`."""
    page_number = 1
    while True:
        page = fetch(page_number)
        yield from page.content or []
        if page.last or not page.content:
            return
        page_number = (page.number or 0) + 2


class Fila:
    """Fila de análise: which processes are queued, in which order, per subqueue."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def areas(self) -> list[models.TipoProduto]:
        """Áreas de interesse (Medicamento=1, Dispositivos Médicos=8, ...)."""
        return [
            models.TipoProduto.model_validate(x) for x in self._client.get("/api/v1/fila/areafila")
        ]

    def grupos(self, area_id: int) -> list[models.ChaveValorLong]:
        """Grupos de fila of an area (Registros, Alterações, Revalidações, ...)."""
        data = self._client.get(f"/api/v1/fila/{area_id}/fila")
        return [models.ChaveValorLong.model_validate(x) for x in data]

    def subfilas(self, grupo_id: int) -> list[models.ChaveValorInteger]:
        """Subfilas of a grupo; their ids are what `consulta` takes."""
        data = self._client.get(f"/api/v1/fila/{grupo_id}/subfila")
        return [models.ChaveValorInteger.model_validate(x) for x in data]

    def consulta(self, subfila_id: int) -> list[models.FilaCalculadaDTO]:
        """The whole calculated queue of a subfila, in order. Not paginated by the API."""
        data = self._client.post("/api/v1/fila/consulta", {"filter": {"subfila": subfila_id}})
        return [models.FilaCalculadaDTO.model_validate(x) for x in data]


class Udi:
    """UDI (Unique Device Identification) of medical devices, plus GMDN terms."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def search(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageUdiDTO:
        """One page of devices. At least one filter is required (`nomeComercial` is verified;
        `udiDi`, `cnpjDetentora`, `codigoGmdn`, `nuRegistro`, ... come from ANVISA's example)."""
        if not filters:
            raise MissingFilterError(
                "udi.search needs at least one filter, e.g. nomeComercial='cateter'"
            )
        data = self._client.post("/api/v1/udi", page_body(page, size, sort, filters))
        return models.PageUdiDTO.model_validate(data)

    def iter_search(
        self, size: int = 100, sort: dict[str, str] | None = None, **filters: Any
    ) -> Iterator[models.UdiDTO]:
        """Every device matching the filters, across pages."""
        return iterate_pages(lambda page: self.search(page, size, sort, **filters))

    def get(self, id: int) -> models.DetalheDispositivoDTO:
        return models.DetalheDispositivoDTO.model_validate(self._client.get(f"/api/v1/udi/{id}"))

    def get_historico(self, id_dispositivo: int, id_historico: int) -> models.DetalheDispositivoDTO:
        data = self._client.get(f"/api/v1/udi/{id_dispositivo}/{id_historico}")
        return models.DetalheDispositivoDTO.model_validate(data)

    def historicos(self) -> list[models.HistoricoUdiDTO]:
        data = self._client.get("/api/v1/udi/historico")
        return [models.HistoricoUdiDTO.model_validate(x) for x in data]

    def termos_gmdn(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageTermoGMDNDTO:
        data = self._client.post("/api/v1/udi/termoGmdn", page_body(page, size, sort, filters))
        return models.PageTermoGMDNDTO.model_validate(data)

    def termo_gmdn(self, codigo: str) -> models.TermoGMDNDTO:
        return models.TermoGMDNDTO.model_validate(
            self._client.get(f"/api/v1/udi/termoGmdn/{codigo}")
        )
