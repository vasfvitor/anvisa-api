"""The five domains ANVISA added on 2026-11-15 (bulario, medicamento, documento, cannabis,
restricao) and the saude anexo download, against the responses recorded on 2026-11-16."""

import inspect
import json

import httpx
import pytest
from conftest import FIXTURES, load, response_for
from typer.main import get_command
from typer.testing import CliRunner

from anvisa import cli
from anvisa import client as client_module
from anvisa.auth import Credentials
from anvisa.client import Client
from anvisa.errors import ApiError, MissingFilterError, NotFoundError
from anvisa.throttle import Throttle


def client_answering(name: str) -> Client:
    """A client whose transport serves one recorded response after the token (FakeApi routes
    only the successful ones, the first per path)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=load("token.json"))
        return response_for(name)

    return Client(
        Credentials("id", "s"),
        transport=httpx.MockTransport(handler),
        throttle=Throttle(sleep=lambda s: None),
    )


def query_of(fake_api, path_end: str) -> dict[str, str]:
    request = next(r for r in reversed(fake_api.requests) if r.url.path.endswith(path_end))
    return dict(request.url.params)


# --- bulario ----------------------------------------------------------------


def test_bulario_search_and_get(client, fake_api):
    page = client.bulario.search(size=2)
    assert page.totalElements == 8855 and len(page.content) == 2
    assert page.content[0].idBulaPacienteProtegido.startswith("eyJ")  # a JWT valid 5 minutes
    assert fake_api.json_bodies()[-1] == {"page": 1, "size": 2, "sorting": {}, "filter": {}}

    detail = client.bulario.get(3551564)
    assert detail.nomeProduto == "NOVALGINA FLASH" and detail.bulaAtual.idDocumento == 34901683
    # page and size go as separate query parameters, not as the JSON `filtro` the spec declares
    assert query_of(fake_api, "/bulario/3551564") == {"page": "1", "size": "10"}


# --- medicamento ------------------------------------------------------------


def test_medicamento_search_needs_a_filter_and_sends_it_in_the_body(client, fake_api):
    with pytest.raises(MissingFilterError):
        client.medicamento.search()
    assert fake_api.requests == []
    client.medicamento.search(size=2, nomeProduto="novalgina")
    assert fake_api.json_bodies()[-1]["filter"] == {"nomeProduto": "novalgina"}


def test_medicamento_empty_filter_is_msg013_live():
    """What the local check prevents: the recorded answer to an empty filter."""
    c = client_answering("err_msg013_medicamento_produtos")
    with c, pytest.raises(MissingFilterError, match="MSG-013"):
        c.post("/api/v1/medicamento/produtos", {"page": 1, "size": 2, "filter": {}})


def test_medicamento_detail_by_processo_and_by_codigo(client):
    mounjaro = client.medicamento.get("25351880657202129")
    assert mounjaro.nomeComercial == "MOUNJARO"
    assert mounjaro.anexoRotulos[0].codigoAnexo == 41320222
    assert mounjaro.anexoRotulos[0].origem == "P"
    tylenol = client.medicamento.get_codigo(3652639)
    assert tylenol.nomeComercial == "TYLENOL" and len(tylenol.apresentacoes) == 53


def test_medicamento_clones(client):
    clones = client.medicamento.clones("25351375698201983")
    assert len(clones) == 6 and clones[0].nomeProduto == "dipirona monoidratada"
    with client_answering("medicamento_processoColen_vazio") as c:
        assert c.medicamento.clones("25351464826202349") == []  # an empty 404


def test_medicamento_lookups(client, fake_api):
    assert len(client.medicamento.categorias_regulatorias()) == 11
    assert len(client.medicamento.tipos_categoria_regulatoria()) == 11
    assert len(client.medicamento.formas_fisicas()) == 639
    assert client.medicamento.categorias()[0].id == "V03AB27"  # a code, not a number
    assert len(client.medicamento.atc()) == 5625
    assert len(client.medicamento.restricoes()) == 19
    assert [t.descricao for t in client.medicamento.tarjas()][0] == "Sem Tarja"
    assert len(client.medicamento.ppam()) == 1216

    assert [t.nome for t in client.medicamento.tipos_priorizacao()][0] == "Prioritário"
    assert query_of(fake_api, "/tipos-priorizacao") == {"showEmpty": "false"}


def test_medicamento_tipos_priorizacao_without_show_empty_is_a_500_live():
    """Why `showEmpty` is always sent."""
    with (
        client_answering("err_tiposPriorizacao") as c,
        pytest.raises(ApiError, match="comValorVazio"),
    ):
        c.get("/api/v1/medicamento/filtros/tipos-priorizacao")


def test_medicamento_substancias_send_flattened_filters(client, fake_api):
    page = client.medicamento.substancias("dipirona", size=2)
    assert page.totalElements == 3 and page.content[0].descricao == "dipirona"
    assert query_of(fake_api, "/substancias") == {
        "page": "1",
        "size": "2",
        "filter[nome]": "dipirona",
    }
    notificados = client.medicamento.produtos_notificados()
    assert notificados.totalElements == 1037 and len(notificados.content) == 1037


def test_medicamento_json_filtro_is_msg011_live():
    """The JSON `filtro` the spec declares, answered with MSG-011."""
    with client_answering("err_msg011_substancias") as c, pytest.raises(ApiError, match="MSG-011"):
        c.get("/api/v1/medicamento/filtros/substancias")


def test_medicamento_downloads(client, fake_api):
    xls = client.medicamento.download(nomeProduto="tylenol")
    assert xls.content[:4] == b"\xd0\xcf\x11\xe0" and xls.filename == "consulta_medicamento.xls"
    rotulo = client.medicamento.download_rotulo(1, "25351880657202129", "P", 41320222)
    assert rotulo.filename == "rotulo_41320222.PDF"
    assert rotulo.content_type == "application/octet-stream"
    downloads = [
        r for r in fake_api.requests if "/download" in r.url.path or "/rotulo/" in r.url.path
    ]
    assert {r.headers["Accept"] for r in downloads} == {"*/*"}


# --- documento --------------------------------------------------------------


def test_documento_search_get_and_history(client, fake_api):
    with pytest.raises(MissingFilterError):
        client.documento.search()
    page = client.documento.search(size=2, processo="25351189946201975")
    assert page.totalElements == 127
    assert fake_api.json_bodies()[-1]["filter"] == {"processo": "25351189946201975"}

    detail = client.documento.get("25351189946201975")
    assert detail.empresa.razaoSocial == "SANOFI MEDLEY FARMACÊUTICA LTDA."
    peticao = client.documento.get_expediente("5045315217")
    assert peticao.processo.numero == "25351189946201975"
    situacoes = client.documento.situacoes("5045315217")
    assert [s.situacao for s in situacoes] == ["Anuído", "Distribuído para a área responsável"]


def test_documento_publicacao_and_lookups(client):
    pub = client.documento.get_publicacao(1115142)
    assert pub.numeroDou == "74" and pub.numeroResolucao == "1.592"
    with client_answering("documento_dou_vazio") as c:
        assert c.documento.get_publicacao(17601299) is None  # 200 with an empty body

    listas = client.documento.listas()
    assert len(listas.listaSituacao) == 167
    assert "28,76,182" in {s.codigo for s in listas.listaSituacao}  # a string, see the overlay
    assert len(client.documento.tipos_produto()) == 10


def test_documento_unknown_parecer_is_an_empty_404():
    with client_answering("err_documento_downloadParecer") as c, pytest.raises(NotFoundError):
        c.documento.download_parecer(1, "4712554213")


# --- cannabis ---------------------------------------------------------------


def test_cannabis_search_get_and_files(client, fake_api):
    with pytest.raises(MissingFilterError):
        client.cannabis.search()
    page = client.cannabis.search(size=2, nomeProduto="canabidiol")
    assert page.totalElements == 4

    detail = client.cannabis.get("25351033121202383")
    assert detail.nomeComercial == "CANABIDIOL ACHÉ"
    hash_ = detail.codigosRotulo[0].codigoHash
    rotulo = client.cannabis.download_anexo("rotulo", hash_)
    assert rotulo.filename == "rotulo_1794877247250.pdf"
    assert fake_api.requests[-1].url.path.endswith(f"/cannabis/download/rotulo/{hash_}/")

    xls = client.cannabis.download(nomeProduto="canabidiol")
    assert xls.filename == "consulta_produto_cannabis.xls"
    assert "filtro" in query_of(fake_api, "/cannabis/download")


# --- restricao and saude ----------------------------------------------------


def test_restricao(client):
    assert len(client.restricao.all()) == 19
    assert [r.id for r in client.restricao.cannabis()] == [537, 538]


def test_saude_unknown_anexo_is_a_500():
    with (
        client_answering("err_saude_anexo") as c,
        pytest.raises(ApiError, match="For input string"),
    ):
        c.saude.download_anexo("25351321393200637", "1", "x.pdf")


# --- CLI --------------------------------------------------------------------

runner = CliRunner()


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


@pytest.mark.parametrize(
    "group, cls",
    [
        ("bulario", client_module.Bulario),
        ("medicamento", client_module.Medicamento),
        ("documento", client_module.Documento),
        ("cannabis", client_module.Cannabis),
        ("restricao", client_module.Restricao),
    ],
)
def test_every_new_method_has_a_cli_command(group, cls):
    """CONTRIBUTING: the CLI command is the method name with `-` for `_` (`--all` covers
    iter_search)."""
    methods = {
        name.replace("_", "-")
        for name, _ in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_") and name != "iter_search"
    }
    commands = set(get_command(cli.app).commands[group].commands)
    assert methods == commands


def test_cli_searches_without_filter_fail_locally(fake_cli_client):
    for group in ("medicamento", "cannabis"):
        result = runner.invoke(cli.app, [group, "search"])
        assert result.exit_code == 1 and "MSG-013" in result.output, group
    assert fake_cli_client.requests == []


def test_cli_bulario_and_medicamento_json(fake_cli_client):
    result = runner.invoke(cli.app, ["-f", "json", "bulario", "search", "--size", "2"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["nomeProduto"] == "IRNO"
    result = runner.invoke(cli.app, ["-f", "json", "medicamento", "formas-fisicas"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)) == 639
    result = runner.invoke(cli.app, ["-f", "json", "medicamento", "ppam"])
    assert json.loads(result.output)[:2][0] == "."
    result = runner.invoke(cli.app, ["-f", "json", "medicamento", "substancias", "dipirona"])
    assert json.loads(result.output)[0]["descricao"] == "dipirona"


def test_cli_documento_listas_and_publicacao(fake_cli_client):
    result = runner.invoke(cli.app, ["-f", "json", "documento", "listas", "assuntos"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)) == 200  # the fixture keeps the first 200
    result = runner.invoke(cli.app, ["-f", "json", "documento", "get-publicacao", "1115142"])
    assert json.loads(result.output)["numeroDou"] == "74"


def test_cli_downloads_write_the_files(fake_cli_client, tmp_path):
    result = runner.invoke(
        cli.app, ["medicamento", "download", "-n", "tylenol", "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "consulta_medicamento.xls").read_bytes() == (
        FIXTURES / "medicamento_download.xls"
    ).read_bytes()
    result = runner.invoke(cli.app, ["cannabis", "download", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "consulta_produto_cannabis.xls").exists()
