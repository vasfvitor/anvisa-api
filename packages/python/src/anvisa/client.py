"""The HTTP client and the API domains: `fila`, `lista`, `udi`, `nome_tecnico`, `assunto` and,
since 2026-10-01 on the gateway, `saude`, `certificado`, `certificado_medicamento` and `tabaco`."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx

from . import __version__, models
from .auth import Credentials, TokenAuth
from .download import Download, filename_from, target_path, write_stream
from .errors import InvalidPageError, MissingFilterError, NotFoundError, raise_for_response
from .throttle import Throttle

BASE_URL = "https://api-gateway.prd.apps.anvisa.gov.br/consultas-externas-api"
USER_AGENT = f"anvisa-python/{__version__} (+https://github.com/vasfvitor/anvisa-api)"
ANY = "*/*"  # the Accept the download endpoints need


class Client:
    """Sync client. Use as a context manager or call `close()`. Without `credentials` they
    come from the environment or `~/.config/anvisa/credentials.env` (`Credentials.from_env`).

    >>> with Client() as anvisa:
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
        self.saude = Saude(self)
        self.certificado = Certificado(self)
        self.certificado_medicamento = CertificadoMedicamento(self)
        self.tabaco = Tabaco(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, path: str) -> Any:
        return self._send(self._http.build_request("GET", path)).json()

    def post(self, path: str, body: Any) -> Any:
        """`body` is whatever the endpoint wants: usually a dict, a bare int for
        downloadAssuntoFormulario."""
        return self._send(self._http.build_request("POST", path, json=body)).json()

    def get_bytes(self, path: str, params: dict[str, str] | None = None) -> Download:
        return self._download(self._http.build_request("GET", path, params=params))

    def post_bytes(self, path: str, body: Any) -> Download:
        return self._download(self._http.build_request("POST", path, json=body))

    def get_stream(self, path: str, target: str | Path, default_name: str) -> Path:
        """GET `path` straight to disk and return the file written: for responses too big to
        buffer (the UDI snapshot zip arrives chunked, with no `Content-Length`). A directory
        `target` is filled in from `Content-Disposition`, or `default_name`."""
        response = self._send(self._http.build_request("GET", path), accept=ANY, stream=True)
        try:
            name = filename_from(response.headers.get("content-disposition"))
            dest = target_path(target, name, default_name)
            write_stream(response.iter_bytes(), dest)
        finally:
            response.close()
        return dest

    def _send(
        self, request: httpx.Request, *, accept: str | None = None, stream: bool = False
    ) -> httpx.Response:
        """The one path every request takes: throttle, send, learn the bucket state from the
        response headers, turn an error status into the matching exception."""
        if accept:
            request.headers["Accept"] = accept
        self.throttle.before()
        response = self._http.send(request, stream=stream)
        try:
            self.throttle.after(response.headers)
            if stream and not response.is_success:
                response.read()  # the error envelope, for the message
            raise_for_response(response)
        except BaseException:
            response.close()
            raise
        return response

    def _download(self, request: httpx.Request) -> Download:
        """Every download endpoint but `GET /udi/{id}/download` answers HTTP 500 "Could not
        find acceptable representation" to the `Accept: application/json` this client sends
        by default (fixture `err_not_acceptable`), hence `Accept: */*`."""
        response = self._send(request, accept=ANY)
        return Download(
            response.content,
            filename_from(response.headers.get("content-disposition")),
            response.headers.get("content-type"),
        )


def page_body(
    page: int, size: int, sort: dict[str, str] | None, filters: dict[str, Any]
) -> dict[str, Any]:
    """ANVISA's `PaginationBuilder` body (the generated model, so the spec's constraints hold
    before the request goes out). Pages are 1-based on the wire."""
    if page < 1:
        raise InvalidPageError(f"page must be >= 1 (got {page}); ANVISA pages are 1-based")
    body = models.PaginationBuilder(page=page, size=size, sorting=sort or {}, filter=filters)
    return body.model_dump(mode="json", exclude_none=True)


T = TypeVar("T")


class PageLike(Protocol[T]):
    content: list[T] | None
    number: int | None
    last: bool | None


def iterate_pages(fetch: Callable[[int], PageLike[T]]) -> Iterator[T]:
    """Yield items across pages. `fetch` takes a 1-based page (responses report it 0-based in
    `number`, which is not trusted: a null there must not send us back to page 2 forever)."""
    page_number = 1
    while True:
        page = fetch(page_number)
        yield from page.content or []
        if page.last or not page.content:
            return
        page_number += 1


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
        """Subfilas of a grupo; their ids are what `query` takes."""
        data = self._client.get(f"/api/v1/fila/{grupo_id}/subfila")
        return [models.ChaveValorInteger.model_validate(x) for x in data]

    def query(self, subfila_id: int) -> list[models.FilaCalculadaDTO]:
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
        Pagination is ignored, as in `query`. A subfila with nothing queued raises
        `EmptyExportError` here, where `query` returns `[]`."""
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
        """Sublistas of a grupo; their ids are what `query` takes."""
        data = self._client.get(f"/api/v1/lista/{grupo_id}/sublista")
        return [models.ChaveValorInteger.model_validate(x) for x in data]

    def query(self, sublista_id: int) -> list[models.FilaCalculadaDTO]:
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

    def get(self, dispositivo_id: int) -> models.DetalheDispositivoDTO:
        return models.DetalheDispositivoDTO.model_validate(
            self._client.get(f"/api/v1/udi/{dispositivo_id}")
        )

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

    def download(self, dispositivo_id: int) -> Download:
        """The device detail as `udi.xlsx` (OOXML, one sheet "UDI-DI - Dispositivo" with a
        header row and one data row). The only download ANVISA also serves to
        `Accept: application/json`."""
        return self._client.get_bytes(f"/api/v1/udi/{dispositivo_id}/download")

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

    def search_gmdn(
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

    def get_gmdn(self, codigo: str) -> models.TermoGMDNDTO:
        return models.TermoGMDNDTO.model_validate(
            self._client.get(f"/api/v1/udi/termoGmdn/{codigo}")
        )


class Assunto:
    """Assuntos de peticionamento: subject codes, and per code the required documents,
    forms, legal basis and fees. `all`, `get`, the four catalogs, `servicos_associados`
    and the two downloads are verified live; `search` is not usable as of 2026-09-06 (see its
    docstring)."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def all(self) -> list[models.AssuntoDTO]:
        """Every subject code with its description (~2,600 entries, one request)."""
        data = self._client.get("/api/v1/assunto/assuntos")
        return [models.AssuntoDTO.model_validate(x) for x in data]

    def get(self, codigo: int | str) -> models.DetalheAssunto:
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
        2026-09-08), unlike `search`, whose body the server cannot bind at all."""
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

    def search(
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
        `tiposSolicitacao`. Use `all` and filter locally instead."""
        data = self._client.post("/api/v1/assunto/", page_body(page, size, sort, filters))
        return models.PageConsultaAssunto.model_validate(data)


class Saude:
    """Produtos para saúde (dispositivos médicos regularizados): registry, detail, exports.
    On the gateway since 2026-10-01, recorded 2026-10-22."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def search(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageConsultaProdutoSaude:
        """One page of products (193,109 rows on 2026-10-22). No filter is required. Verified
        filters: `nomeProduto` (prefix/substring) and `cnpj`; `razaoSocial`, `nomeTecnico`,
        `registro`, `numeroRegistro`, `numeroProcesso`, `tipoAssunto` and
        `situacaoNotificacaoRegistro` come from ANVISA's example and are untested."""
        data = self._client.post("/api/v1/saude", page_body(page, size, sort, filters))
        return models.PageConsultaProdutoSaude.model_validate(data)

    def iter_search(
        self, size: int = 100, sort: dict[str, str] | None = None, **filters: Any
    ) -> Iterator[models.ConsultaProdutoSaude]:
        """Every product matching the filters, across pages."""
        return iterate_pages(lambda page: self.search(page, size, sort, **filters))

    def get(self, numero_processo: str) -> models.DetalheProdutoSaude:
        """One product by processo number (17 digits, no punctuation), with apresentações,
        fabricantes and risk class. A POST whose body is ignored as far as observed: `{}`
        returned the product on 2026-10-22."""
        data = self._client.post(f"/api/v1/saude/{numero_processo}", {})
        return models.DetalheProdutoSaude.model_validate(data)

    def download(self, numero_processo: str) -> Download:
        """The product detail as `DetalheProdutoSaude.xls` (BIFF .xls)."""
        return self._client.get_bytes(f"/api/v1/saude/downloadExcel/{numero_processo}")

    def download_pdf(self, numero_processo: str) -> Download:
        """The product detail as a PDF; the server names it `consulta_alimento.pdf` (sic)."""
        return self._client.get_bytes(f"/api/v1/saude/downloadPDF/{numero_processo}")


def certificado_filters(contexto: str, filters: dict[str, Any]) -> dict[str, Any]:
    """The certificado searches need one real filter key: an empty filter, and `contexto`
    alone, answer HTTP 500 `mensagens.MSG-013` (fixture `err_msg013`). Checked here, before
    any request, and `contexto` is filled in."""
    if not {k for k in filters if k != "contexto"}:
        raise MissingFilterError(
            f"{contexto}.search needs at least one filter, e.g. nacional=True, "
            "internacional=True or status='0' (mensagens.MSG-013)"
        )
    return {"contexto": contexto, **filters}


class Certificado:
    """Certificados de boas práticas (CBPF/CBPD) of produtos para saúde and the other
    non-medicine families. On the gateway since 2026-10-01, recorded 2026-10-22."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def search(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageCertificadoResultado:
        """One page of certificates (6,886 with `nacional=True, internacional=True` on
        2026-10-22). At least one filter is required (`MissingFilterError` locally). Verified:
        `nacional`, `internacional` (booleans), `status` (ids from `status()`, comma-separated)
        and `cnpjCertificada`; `certificacaoConcedidaPor`, `ufs`, `tipoCertificado` and the
        date keys come from ANVISA's example and are untested. A filter that matches nothing is
        an empty 404 (`NotFoundError`)."""
        body = page_body(page, size, sort, certificado_filters("certificado", filters))
        return models.PageCertificadoResultado.model_validate(
            self._client.post("/api/v1/certificado/", body)
        )

    def iter_search(
        self, size: int = 100, sort: dict[str, str] | None = None, **filters: Any
    ) -> Iterator[models.CertificadoResultado]:
        """Every certificate matching the filters, across pages."""
        return iterate_pages(lambda page: self.search(page, size, sort, **filters))

    def get(self, certificado_id: int) -> models.CertificadoDetalhe:
        """One certificate by `idCertificado`, with its linhas de certificação."""
        data = self._client.get(f"/api/v1/certificado/{certificado_id}")
        return models.CertificadoDetalhe.model_validate(data)

    def status(self) -> list[models.ChaveValorInteger]:
        """Válido=0, Indeferido=1, Cancelado=2, Vencido=3."""
        data = self._client.get("/api/v1/certificado/status")
        return [models.ChaveValorInteger.model_validate(x) for x in data]

    def concedida_por(self) -> list[models.EmissaoCertificadoDTO]:
        """How a certification was granted (inspeção, análise de risco, ...): 10 entries."""
        data = self._client.get("/api/v1/certificado/certificacaoConcedidaPor")
        return [models.EmissaoCertificadoDTO.model_validate(x) for x in data]

    def download(self, size: int = 10, **filters: Any) -> Download:
        """The matching certificates as `consulta_certificado.xls`, which is OOXML despite
        the name. Takes the filter keys of `search`; `size` was sent as 10 and the file held
        every match (3 rows) on 2026-10-22. An empty filter was not tried."""
        body = page_body(1, size, None, certificado_filters("certificado", filters))
        return self._client.post_bytes("/api/v1/certificado/download", body)


class CertificadoMedicamento:
    """Certificados de boas práticas de fabricação de medicamentos, with their lines, classes
    and pharmaceutical forms. On the gateway since 2026-10-01, recorded 2026-10-22."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def search(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageCertificadoResultado:
        """One page of certificates (2,774 with `nacional=True, internacional=True,
        status="0"` on 2026-10-22). At least one filter is required (`MissingFilterError`
        locally). Verified: `nacional`, `internacional`, `status` and `cnpjSolicitante`
        (international certificates carry no `cnpjCertificada`); `classesCertificacao`,
        `formasFarmaceuticas`, `linhasCertificacao` (ids from the lookups), `codigoUnico`,
        `pais` and the date keys are untested."""
        body = page_body(page, size, sort, certificado_filters("certificadoMedicamento", filters))
        return models.PageCertificadoResultado.model_validate(
            self._client.post("/api/v1/certificadoMedicamento/", body)
        )

    def iter_search(
        self, size: int = 100, sort: dict[str, str] | None = None, **filters: Any
    ) -> Iterator[models.CertificadoResultado]:
        """Every certificate matching the filters, across pages."""
        return iterate_pages(lambda page: self.search(page, size, sort, **filters))

    def get(self, certificado_id: int) -> models.CertificadoDetalhe:
        """One certificate by `idCertificado`; `linhasCertificacao[].campos` carries the
        line, class, steps and pharmaceutical form of each certified line."""
        data = self._client.get(f"/api/v1/certificadoMedicamento/{certificado_id}")
        return models.CertificadoDetalhe.model_validate(data)

    def status(self) -> list[models.ChaveValorInteger]:
        """Válido=0, Indeferido=1, Cancelado=2, Vencido=3."""
        data = self._client.get("/api/v1/certificadoMedicamento/status")
        return [models.ChaveValorInteger.model_validate(x) for x in data]

    def concedida_por(self) -> list[models.EmissaoCertificadoDTO]:
        """How a certification was granted; the same 10 entries as `certificado`."""
        data = self._client.get("/api/v1/certificadoMedicamento/certificacaoConcedidaPor")
        return [models.EmissaoCertificadoDTO.model_validate(x) for x in data]

    def linhas_certificacao(self) -> list[models.LinhaCbpf]:
        """Certification lines (carbapenêmicos, cefalosporínicos, citotóxicos, ...): 16."""
        data = self._client.get("/api/v1/certificadoMedicamento/linhasCertificacao")
        return [models.LinhaCbpf.model_validate(x) for x in data]

    def formas_farmaceuticas(self) -> list[models.FormaFarmaceuticaBasica]:
        """Basic pharmaceutical forms: 80 entries."""
        data = self._client.get("/api/v1/certificadoMedicamento/formasFarmaceuticas")
        return [models.FormaFarmaceuticaBasica.model_validate(x) for x in data]

    def classes_certificacao(self) -> list[models.ClasseCertificacao]:
        """Certification classes: 9 entries."""
        data = self._client.get("/api/v1/certificadoMedicamento/classesCertificacao")
        return [models.ClasseCertificacao.model_validate(x) for x in data]

    def download(self, size: int = 10, **filters: Any) -> Download:
        """The matching certificates as `consulta_certificado.xls` (OOXML despite the name).
        Takes the filter keys of `search`; an empty filter was not tried."""
        body = page_body(1, size, None, certificado_filters("certificadoMedicamento", filters))
        return self._client.post_bytes("/api/v1/certificadoMedicamento/download", body)


class Tabaco:
    """Produtos derivados do tabaco: the registry, labels and lookups. On the gateway since
    2026-10-01, recorded 2026-10-22."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def search(
        self,
        page: int = 1,
        size: int = 20,
        sort: dict[str, str] | None = None,
        **filters: Any,
    ) -> models.PageConsultaTabacoDTO:
        """One page of products (3,578 rows on 2026-10-22). No filter is required. Verified:
        `nomeProduto`; `cnpjEmpresa` and `cnpj` are silently ignored by the API (the whole
        table comes back). The list view leaves `embalagens`, `rotulos`, `idProduto`, `origem`
        and `categoriaAditivos` empty; `get` fills them."""
        data = self._client.post("/api/v1/tabaco/tabacos", page_body(page, size, sort, filters))
        return models.PageConsultaTabacoDTO.model_validate(data)

    def iter_search(
        self, size: int = 100, sort: dict[str, str] | None = None, **filters: Any
    ) -> Iterator[models.ConsultaTabacoDTO]:
        """Every product matching the filters, across pages."""
        return iterate_pages(lambda page: self.search(page, size, sort, **filters))

    def get(self, cod_documento: int) -> models.ConsultaTabacoDTO:
        """One product by `codDocumento` from `search`, with embalagens, rótulos and medidas
        cautelares. The processo number and `idProduto` are not accepted (`NoResultError`)."""
        data = self._client.get(f"/api/v1/tabaco/detailTabaco/{cod_documento}")
        return models.ConsultaTabacoDTO.model_validate(data)

    def tipos(self) -> list[models.TipoProdutoTabaco]:
        """Product types (bidis, blunt, charuto, cigarro, ...): 14."""
        data = self._client.get("/api/v1/tabaco/tipoTabaco")
        return [models.TipoProdutoTabaco.model_validate(x) for x in data]

    def situacoes(self) -> list[models.ChaveValorObject]:
        """Ativo=S, Inativo=N."""
        data = self._client.get("/api/v1/tabaco/situacoes")
        return [models.ChaveValorObject.model_validate(x) for x in data]

    def origens(self) -> list[models.ChaveValorObject]:
        """Importado, fabricação nacional, ...; `id` is null on every entry."""
        data = self._client.get("/api/v1/tabaco/origemTabaco")
        return [models.ChaveValorObject.model_validate(x) for x in data]

    def destinos(self) -> list[models.ChaveValorObject]:
        """Mercado interno, interno e externo, exportação; `id` is null on every entry."""
        data = self._client.get("/api/v1/tabaco/destinoTabaco")
        return [models.ChaveValorObject.model_validate(x) for x in data]

    def categorias_aditivo(self) -> list[models.CategoriaAditivo]:
        """Additive categories (adesivos, aglutinantes, umectantes, ...): 11."""
        data = self._client.get("/api/v1/tabaco/categoriaAditivo")
        return [models.CategoriaAditivo.model_validate(x) for x in data]

    def download(self, **filters: Any) -> Download:
        """The whole table as `consulta_fumigenos.xls` (BIFF .xls, 897 KB on 2026-10-22).
        `filtro` goes in the query string as JSON, and the API ignored both the filter and the
        size sent; the keys are accepted here only so the request documents what was asked."""
        filtro = json.dumps({"page": 1, "size": 10, "sorting": {}, "filter": dict(filters)})
        return self._client.get_bytes("/api/v1/tabaco/download", params={"filtro": filtro})

    def download_pdf(self, cod_documento: int) -> Download:
        """The product detail as a PDF (`consulta_tabaco.pdf`)."""
        return self._client.get_bytes(f"/api/v1/tabaco/downloadPDF/{cod_documento}")

    def download_rotulo(self, rotulo_id: int) -> Download:
        """The label as a PDF/A (`rotulo_<id>.PDF`, 387 KB for 3631632), by `rotulos[].id`
        from `get`. Sent as `application/force-download`."""
        return self._client.get_bytes(f"/api/v1/tabaco/embalagem/{rotulo_id}")
