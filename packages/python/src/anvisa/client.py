"""The HTTP client and the API domains: `fila`, `lista`, `udi`, `nome_tecnico` and `assunto`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Protocol, TypeVar

import httpx

from . import __version__, models
from .auth import Credentials, TokenAuth
from .errors import InvalidPageError, MissingFilterError, raise_for_response
from .throttle import Throttle

BASE_URL = "https://api-gateway.prd.apps.anvisa.gov.br/consultas-externas-api"
USER_AGENT = f"anvisa-python/{__version__} (+https://github.com/vasfvitor/anvisa-api)"


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
        self.lista = Lista(self)
        self.udi = Udi(self)
        self.nome_tecnico = NomeTecnico(self)
        self.assunto = Assunto(self)

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


class Lista:
    """Listas calculadas: the same structure as `fila` (área → grupo → sublista → rows) for
    the "lista" reports. Rows share `FilaCalculadaDTO` with the queue."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def areas(self) -> list[models.TipoProduto]:
        """Áreas with lists (Empresas=7, Insumo Farmacêutico=15, Medicamento=1, Toxicologia=9)."""
        data = self._client.get("/api/v1/lista/arealista")
        return [models.TipoProduto.model_validate(x) for x in data]

    def grupos(self, area_id: int) -> list[models.ChaveValorLong]:
        """Grupos de lista of an area."""
        data = self._client.get(f"/api/v1/lista/{area_id}/lista")
        return [models.ChaveValorLong.model_validate(x) for x in data]

    def sublistas(self, grupo_id: int) -> list[models.ChaveValorInteger]:
        """Sublistas of a grupo; their ids are what `consulta` takes."""
        data = self._client.get(f"/api/v1/lista/{grupo_id}/sublista")
        return [models.ChaveValorInteger.model_validate(x) for x in data]

    def consulta(self, sublista_id: int) -> list[models.FilaCalculadaDTO]:
        """The whole calculated list of a sublista. Not paginated by the API.

        The filter key is `subfila` even here (the API answers "Filtro 'subfila' não
        informado." to anything else)."""
        data = self._client.post("/api/v1/lista/consulta", {"filter": {"subfila": sublista_id}})
        return [models.FilaCalculadaDTO.model_validate(x) for x in data]


class NomeTecnico:
    """Nomes técnicos de produtos para saúde, with risk class and category."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def search(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageNomeTecnicoDTO:
        """One page of technical names. Unlike `udi`, no filter is required (~2,700 rows).
        Verified filters: `nomeTecnico` (substring match) and `categoriaProduto` (id from
        `categorias`); `codigo` comes from ANVISA's example and is untested."""
        data = self._client.post("/api/v1/nomeTecnico", page_body(page, size, sort, filters))
        return models.PageNomeTecnicoDTO.model_validate(data)

    def iter_search(
        self, size: int = 100, sort: dict[str, str] | None = None, **filters: Any
    ) -> Iterator[models.NomeTecnicoDTO]:
        """Every technical name matching the filters, across pages."""
        return iterate_pages(lambda page: self.search(page, size, sort, **filters))

    def categorias(self) -> list[models.TipoProduto]:
        """Categories (Equipamento ou Material=8, Diagnóstico in vitro=12)."""
        data = self._client.get("/api/v1/nomeTecnico/categorias")
        return [models.TipoProduto.model_validate(x) for x in data]


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
        """One page of devices. At least one filter is required. Verified filters:
        `nomeComercial` (substring), `udiDi` (exact), `cnpjDetentora`, `codigoGmdn`,
        `nuRegistro`; the rest of ANVISA's example keys are untested."""
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
        """A device as recorded in one snapshot from `historicos`. Raises `NotFoundError`
        (empty 404) when the device was not part of that snapshot."""
        data = self._client.get(f"/api/v1/udi/{id_dispositivo}/{id_historico}")
        return models.DetalheDispositivoDTO.model_validate(data)

    def historicos(self) -> list[models.HistoricoUdiDTO]:
        """The daily UDI snapshots (`tipoHistorico` DIARIO), newest first, with how many
        records each one holds. 171 entries on 2026-09-06."""
        data = self._client.get("/api/v1/udi/historico")
        return [models.HistoricoUdiDTO.model_validate(x) for x in data]

    def termos_gmdn(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageTermoGMDNDTO:
        """GMDN term search. Verified filter: `conteudo` (text, matches the Portuguese name and
        definition); `codigo` comes from ANVISA's example. No filter is required."""
        data = self._client.post("/api/v1/udi/termoGmdn", page_body(page, size, sort, filters))
        return models.PageTermoGMDNDTO.model_validate(data)

    def termo_gmdn(self, codigo: str) -> models.TermoGMDNDTO:
        return models.TermoGMDNDTO.model_validate(
            self._client.get(f"/api/v1/udi/termoGmdn/{codigo}")
        )


class Assunto:
    """Assuntos de peticionamento: subject codes, and per code the required documents,
    forms, legal basis and fees. `lista`, `detalhe` and the four catalogs are verified live;
    `busca` is not usable as of 2026-09-06 (see its docstring)."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def lista(self) -> list[models.AssuntoDTO]:
        """Every subject code with its description (~2,600 entries, one request)."""
        data = self._client.get("/api/v1/assunto/assuntos")
        return [models.AssuntoDTO.model_validate(x) for x in data]

    def detalhe(self, codigo: int | str) -> models.DetalheAssunto:
        """Full detail of one subject: system, services, forms, checklist, fees by company size."""
        return models.DetalheAssunto.model_validate(self._client.get(f"/api/v1/assunto/{codigo}"))

    def tipos_solicitacao(self) -> list[models.TipoSolicitacaoDTO]:
        data = self._client.get("/api/v1/assunto/tiposSolicitacao")
        return [models.TipoSolicitacaoDTO.model_validate(x) for x in data]

    def tipos_produto(self) -> list[models.TipoProdutoDTO]:
        data = self._client.get("/api/v1/assunto/tiposProduto")
        return [models.TipoProdutoDTO.model_validate(x) for x in data]

    def sistemas(self) -> list[models.SistemaDTO]:
        data = self._client.get("/api/v1/assunto/sistemas")
        return [models.SistemaDTO.model_validate(x) for x in data]

    def servicos(self) -> list[models.ServicoDTO]:
        data = self._client.get("/api/v1/assunto/servicos")
        return [models.ServicoDTO.model_validate(x) for x in data]

    def busca(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageConsultaAssunto:
        """Paginated search, `POST /api/v1/assunto/`. Not usable as of 2026-09-06: the API answers
        HTTP 500 "PaginationBuilder.getColumn() because filtro is null" to a JSON body, and the
        path without the trailing slash is a 404 (fixture `err_assunto_busca`). Filter keys from
        ANVISA's example: `codigosAssunto`, `servicos`, `sistemas`, `tiposProduto`,
        `tiposSolicitacao`. Use `lista` and filter locally instead."""
        data = self._client.post("/api/v1/assunto/", page_body(page, size, sort, filters))
        return models.PageConsultaAssunto.model_validate(data)
