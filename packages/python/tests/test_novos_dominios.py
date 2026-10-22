"""The four domains ANVISA added on 2026-10-01 (saude, certificado, certificadoMedicamento,
tabaco), against the responses recorded on 2026-10-22."""

import json

import httpx
import pytest
from conftest import FIXTURES, load, response_for
from typer.testing import CliRunner

from anvisa import cli
from anvisa.auth import Credentials
from anvisa.client import Client
from anvisa.errors import MissingFilterError, NoResultError, NotFoundError
from anvisa.throttle import Throttle

runner = CliRunner()


def client_answering(name: str) -> Client:
    """A client whose transport serves one recorded non-200 response (FakeApi routes only
    the successful ones), after the token."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=load("token.json"))
        return response_for(name)

    return Client(
        Credentials("id", "s"),
        transport=httpx.MockTransport(handler),
        throttle=Throttle(sleep=lambda s: None),
    )


# --- saude ------------------------------------------------------------------


def test_saude_search_and_get(client, fake_api):
    page = client.saude.search(size=2)
    assert page.totalElements == 193109 and len(page.content) == 2
    assert fake_api.json_bodies()[-1] == {"page": 1, "size": 2, "sorting": {}, "filter": {}}

    detail = client.saude.get("25351279419201172")
    assert detail.produto == "1000VINYL" and detail.nomeTecnico == "Luvas Descartaveis"
    assert detail.apresentacoesPage.content[0].modelos == ["P, M, G"]
    assert fake_api.json_bodies()[-1] == {}  # the body the spec declares is ignored


def test_saude_filters_go_in_the_body(client, fake_api):
    client.saude.search(size=2, nomeProduto="1000VINYL")
    client.saude.search(size=2, cnpj="05993698000107")
    assert [b["filter"] for b in fake_api.json_bodies()] == [
        {"nomeProduto": "1000VINYL"},
        {"cnpj": "05993698000107"},
    ]


def test_saude_downloads(client, fake_api):
    pdf = client.saude.download_pdf("25351279419201172")
    assert pdf.content[:5] == b"%PDF-" and pdf.filename == "consulta_alimento.pdf"
    xls = client.saude.download("25351279419201172")
    assert xls.content[:4] == b"\xd0\xcf\x11\xe0" and xls.filename == "DetalheProdutoSaude.xls"
    assert {r.headers["Accept"] for r in fake_api.requests if "/download" in r.url.path} == {"*/*"}


# --- certificado ------------------------------------------------------------


@pytest.mark.parametrize("domain", ["certificado", "certificado_medicamento"])
def test_certificado_search_needs_a_real_filter(client, fake_api, domain):
    with pytest.raises(MissingFilterError):
        getattr(client, domain).search()
    with pytest.raises(MissingFilterError):
        getattr(client, domain).search(contexto="certificado")  # contexto alone is MSG-013 live
    assert fake_api.requests == []


def test_certificado_search_fills_in_contexto(client, fake_api):
    page = client.certificado.search(size=2, nacional=True, internacional=True)
    assert page.totalElements == 6886 and page.content[0].idCertificado == 33521614
    assert fake_api.json_bodies()[-1]["filter"] == {
        "contexto": "certificado",
        "nacional": True,
        "internacional": True,
    }
    client.certificado_medicamento.search(size=2, cnpjSolicitante="39809660000153")
    assert fake_api.json_bodies()[-1]["filter"] == {
        "contexto": "certificadoMedicamento",
        "cnpjSolicitante": "39809660000153",
    }


def test_certificado_no_match_is_an_empty_404():
    with client_answering("certificado_vazio") as c, pytest.raises(NotFoundError):
        c.certificado.search(size=2, cnpjCertificada="76475789000107")


def test_certificado_empty_filter_is_msg013_live():
    """What the local check prevents: the recorded answer to an empty filter."""
    with client_answering("err_msg013") as c, pytest.raises(MissingFilterError) as info:
        c.certificado._client.post("/api/v1/certificado/", {"filter": {}})
    assert "MSG-013" in str(info.value)


def test_certificado_detail_and_lookups(client):
    detail = client.certificado.get(33521614)
    assert detail.tipoCertificado == "CBPF" and detail.cidade == "PATO BRANCO"
    assert [s.descricao for s in client.certificado.status()] == [
        "Válido",
        "Indeferido",
        "Cancelado",
        "Vencido",
    ]
    assert len(client.certificado.concedida_por()) == 10


def test_certificado_medicamento_detail_and_lookups(client):
    detail = client.certificado_medicamento.get(33318813)
    campos = {c.campo: c.valores[0].valor for c in detail.linhasCertificacao[0].campos}
    assert campos["classe-certificacao"] == "Cefalosporínicos"
    assert len(client.certificado_medicamento.linhas_certificacao()) == 16
    assert len(client.certificado_medicamento.formas_farmaceuticas()) == 80
    assert len(client.certificado_medicamento.classes_certificacao()) == 9


@pytest.mark.parametrize(
    ("call", "contexto"),
    [
        (lambda c: c.certificado.download(cnpjCertificada="05993698000450"), "certificado"),
        (
            lambda c: c.certificado_medicamento.download(cnpjSolicitante="39809660000153"),
            "certificadoMedicamento",
        ),
    ],
)
def test_certificado_download_is_ooxml_named_xls(client, fake_api, call, contexto):
    download = call(client)
    assert download.content[:2] == b"PK" and download.filename == "consulta_certificado.xls"
    body = fake_api.json_bodies()[-1]
    assert body["filter"]["contexto"] == contexto and body["size"] == 10


# --- tabaco -----------------------------------------------------------------


def test_tabaco_search_and_get(client, fake_api):
    page = client.tabaco.search(size=2)
    assert page.totalElements == 3578 and page.content[0].rotulos == []
    one = client.tabaco.search(size=2, nomeProduto="7 PONTAS")
    assert one.totalElements == 1 and one.content[0].codDocumento == 33007111

    detail = client.tabaco.get(33007111)
    assert detail.idProduto == 3790919 and detail.rotulos[0].id == 3631632
    assert detail.embalagens[0].tipoEmbalagem == "CAIXA"
    assert fake_api.requests[-1].url.path.endswith("/tabaco/detailTabaco/33007111")


def test_tabaco_get_rejects_the_processo_number():
    with client_answering("err_tabaco_detail") as c, pytest.raises(NoResultError):
        c.tabaco.get(25351262578201699)


def test_tabaco_lookups(client):
    assert len(client.tabaco.tipos()) == 14
    assert [(s.descricao, s.id) for s in client.tabaco.situacoes()] == [
        ("Ativo", "S"),
        ("Inativo", "N"),
    ]
    assert all(o.id is None for o in client.tabaco.origens())
    assert len(client.tabaco.destinos()) == 3
    assert len(client.tabaco.categorias_aditivo()) == 11


def test_tabaco_download_sends_filtro_as_json_in_the_query(client, fake_api):
    download = client.tabaco.download(nomeProduto="7 PONTAS")
    assert download.filename == "consulta_fumigenos.xls"  # headers-only fixture
    request = fake_api.requests[-1]
    assert request.method == "GET" and request.headers["Accept"] == "*/*"
    assert json.loads(request.url.params["filtro"])["filter"] == {"nomeProduto": "7 PONTAS"}


def test_tabaco_pdfs(client):
    assert client.tabaco.download_pdf(33007111).filename == "consulta_tabaco.pdf"
    rotulo = client.tabaco.download_rotulo(3631632)
    assert rotulo.filename == "rotulo_3631632.PDF"
    assert rotulo.content_type == "application/force-download"


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def fake_cli_client(fake_api, monkeypatch):
    def make_client():
        return Client(
            Credentials("id", "s"),
            transport=httpx.MockTransport(fake_api.handler),
            throttle=Throttle(sleep=lambda s: None),
        )

    monkeypatch.setattr(cli, "make_client", make_client)
    return fake_api


def test_cli_saude_search_json(fake_cli_client):
    result = runner.invoke(cli.app, ["--format", "json", "saude", "search", "--size", "2"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["produto"] == "1000VINYL"


def test_cli_certificado_search_without_filter_fails_locally(fake_cli_client):
    result = runner.invoke(cli.app, ["certificado", "search"])
    assert result.exit_code == 1 and "MSG-013" in result.output
    assert fake_cli_client.requests == []


def test_cli_certificado_medicamento_search_table(fake_cli_client):
    args = ["-f", "table", "certificado-medicamento", "search", "--nacional", "--internacional"]
    result = runner.invoke(cli.app, args + ["--status", "0", "--size", "2"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    assert "33318813" in result.output  # idCertificado; the long names wrap


def test_cli_tabaco_get_and_download_rotulo(fake_cli_client, tmp_path):
    result = runner.invoke(cli.app, ["--format", "json", "tabaco", "get", "33007111"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["nomeProduto"] == "7 PONTAS"
    result = runner.invoke(cli.app, ["tabaco", "download-pdf", "33007111", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "consulta_tabaco.pdf").read_bytes() == (
        FIXTURES / "tabaco_downloadPDF_33007111.pdf"
    ).read_bytes()
