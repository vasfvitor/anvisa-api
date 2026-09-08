"""The HTTP client and the API domains: `fila`, `lista`, `udi`, `nome_tecnico` and `assunto`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx

from . import __version__, models
from .auth import Credentials, TokenAuth
from .download import Download, filename_from
from .errors import InvalidPageError, MissingFilterError, NotFoundError, raise_for_response
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

    def get_bytes(self, path: str) -> Download:
        return self._send_bytes(self._http.build_request("GET", path))

    def post_bytes(self, path: str, body: Any) -> Download:
        """`body` is whatever the endpoint wants; downloadAssuntoFormulario wants a bare int."""
        return self._send_bytes(self._http.build_request("POST", path, json=body))

    def get_stream(self, path: str, target: str | Path, default_name: str) -> Path:
        return self.stream_to(self._http.build_request("GET", path), target, default_name)

    def _send_bytes(self, request: httpx.Request) -> Download:
        """Like `_send`, but keeps the body as bytes and asks for `Accept: */*`.

        Every download endpoint but `GET /udi/{id}/download` answers HTTP 500 "Could not find
        acceptable representation" to the `Accept: application/json` this client sends by
        default (fixture `err_not_acceptable`)."""
        request.headers["Accept"] = "*/*"
        self.throttle.before()
        response = self._http.send(request)
        self.throttle.after(response.headers)
        raise_for_response(response)
        return Download(
            response.content,
            filename_from(response.headers.get("content-disposition")),
            response.headers.get("content-type"),
        )

    def stream_to(self, request: httpx.Request, path: str | Path, default_name: str) -> Path:
        """Send `request` and write the body straight to disk, returning the file written.

        For responses too big to buffer: the UDI snapshot zip arrives chunked, with no
        `Content-Length`. A directory `path` is filled in from `Content-Disposition`."""
        request.headers["Accept"] = "*/*"
        self.throttle.before()
        response = self._http.send(request, stream=True)
        try:
            self.throttle.after(response.headers)
            if not response.is_success:
                response.read()
                raise_for_response(response)
            target = Path(path)
            if target.is_dir():
                name = filename_from(response.headers.get("content-disposition"))
                target = target / (name or default_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        finally:
            response.close()
        return target


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
        """The whole calculated queue of a subfila, in order. Not paginated by the API.

        A subfila with nothing queued answers HTTP 404 with an empty body (88 of 314 subfilas
        on 2026-09-06); that is returned as `[]`. The API gives an unknown id the same answer."""
        try:
            data = self._client.post("/api/v1/fila/consulta", {"filter": {"subfila": subfila_id}})
        except NotFoundError:
            return []
        return [models.FilaCalculadaDTO.model_validate(x) for x in data]

    def download(self, subfila_id: int) -> Download:
        """The whole subfila as a spreadsheet, named `consulta_fila.xlsx` by the server.

        The bytes are OOXML (`PK`) despite the `application/vnd.ms-excel` content type, and
        hold a 7-row header block above the rows (40 rows for subfila 167 on 2026-09-08).
        Pagination is ignored, as in `consulta`. A subfila with nothing queued raises
        `EmptyExportError` here, where `consulta` returns `[]`."""
        return self._client.post_bytes(
            "/api/v1/fila/downloadfila", {"filter": {"subfila": subfila_id}}
        )


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
        informado." to anything else). An empty 404 is returned as `[]`, as for `fila`
        (observed there, assumed here)."""
        try:
            data = self._client.post("/api/v1/lista/consulta", {"filter": {"subfila": sublista_id}})
        except NotFoundError:
            return []
        return [models.FilaCalculadaDTO.model_validate(x) for x in data]

    def download(self, sublista_id: int) -> Download:
        """The whole sublista as a spreadsheet, named `consulta_lista.xlsx` by the server
        (OOXML, 555 rows for sublista 2141 on 2026-09-08). The filter key is `subfila` here
        too. An empty sublista is assumed to raise `EmptyExportError`, as observed for
        `fila.download`."""
        return self._client.post_bytes(
            "/api/v1/lista/downloadlista", {"filter": {"subfila": sublista_id}}
        )


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

    def download(self, **filters: Any) -> Download:
        """Technical names as `consulta_nomes_tecnicos_produto_saude.xls` (BIFF .xls).

        No filter is required; the keys are those of `search`. Not paginated: sending
        `size: 5` with `nomeTecnico="cateter"` still returned all 71 matching rows on
        2026-09-08, so only the filter is sent."""
        return self._client.post_bytes("/api/v1/nomeTecnico/download", {"filter": dict(filters)})


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

    def download(self, id: int) -> Download:
        """The device detail as `udi.xlsx` (OOXML, one sheet "UDI-DI - Dispositivo" with a
        header row and one data row). The only download ANVISA also serves to
        `Accept: application/json`."""
        return self._client.get_bytes(f"/api/v1/udi/{id}/download")

    def download_historico(self, id_dispositivo: int, id_historico: int) -> Download:
        """The device as recorded in one snapshot, as a spreadsheet.

        When the device is not part of that snapshot this raises a plain `ApiError`: the
        export throws a NullPointerException, `Cannot invoke
        "...DetalheDispositivoDTO.getDispositivo()" because "item" is null` (device 377 in
        snapshot 173, 2026-09-08), where `get_historico` answers an empty 404. The success
        case has not been observed live."""
        return self._client.get_bytes(f"/api/v1/udi/{id_dispositivo}/{id_historico}/download")

    def download_snapshot(self, id_historico: int, path: str | Path = ".") -> Path:
        """Stream one snapshot's zip to disk and return the file written.

        The zip holds the week's daily UDI files, named after the range it covers
        (`BR_UDIDI_semanal_atualizacao_20260831_20260906.zip`, 232,435 bytes for snapshot 173
        on 2026-09-08). It arrives chunked with no `Content-Length`, so it is never
        buffered; a directory `path` gets the server's name."""
        return self._client.get_stream(
            f"/api/v1/udi/historico/{id_historico}/download",
            path,
            f"udi_historico_{id_historico}.zip",
        )

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
    forms, legal basis and fees. `lista`, `detalhe`, the four catalogs, `servicos_associados`
    and the two downloads are verified live; `busca` is not usable as of 2026-09-06 (see its
    docstring)."""

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

    def servicos_associados(self, codigo_servico: int | str) -> list[models.ServicoDTO]:
        """The gov.br services associated with a serviço code, each with its `hiperlink`.
        One entry for 13497 on 2026-09-08."""
        data = self._client.get(f"/api/v1/assunto/servicosAssociados/{codigo_servico}")
        return [models.ServicoDTO.model_validate(x) for x in data]

    def download(self, **filters: Any) -> Download:
        """Assuntos as `consulta_assuntos.xls` (BIFF .xls). Not paginated.

        Without filters this is every assunto: 2,599 rows, about 5 MB, buffered in memory.
        `codigosAssunto` takes a list of ids and does bind here (13 KB for `[10013]` on
        2026-09-08), unlike `busca`, whose body the server cannot bind at all."""
        return self._client.post_bytes("/api/v1/assunto/download", {"filter": dict(filters)})

    def formulario(self, formulario_id: int, filename: str | None = None) -> Download:
        """One assunto's form file, by `DetalheAssunto.formularios[].id`.

        The request body is a bare JSON integer, not the `PaginationBuilder` the spec
        declares: an object raises `MalformedRequestError` and an unknown id raises
        `NoResultError`. The response carries neither `Content-Type` nor
        `Content-Disposition`, so pass `filename` (from `formularios[].nomeArquivo`) or take
        the `formulario_<id>` default. Formulário 8016 came back as a .docx on 2026-09-08."""
        download = self._client.post_bytes(
            "/api/v1/assunto/downloadAssuntoFormulario", formulario_id
        )
        name = download.filename or filename or f"formulario_{formulario_id}"
        return replace(download, filename=name)

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
