import json

import httpx
import pytest
from conftest import load

from anvisa import models
from anvisa.auth import Credentials
from anvisa.client import Client, iterate_pages, page_body
from anvisa.errors import InvalidPageError, MissingFilterError, NotFoundError
from anvisa.throttle import Throttle


def test_fila_chain(client, fake_api):
    areas = client.fila.areas()
    assert len(areas) == 11
    assert {a.id: a.descricao for a in areas}[8] == "Dispositivos Médicos"

    grupos = client.fila.grupos(8)
    assert len(grupos) == 8
    assert fake_api.requests[-1].url.path.endswith("/api/v1/fila/8/fila")

    subfilas = client.fila.subfilas(285)
    assert len(subfilas) == 13
    assert any(s.id == 167 for s in subfilas)

    fila = client.fila.consulta(167)
    assert len(fila) == 40
    assert fila[0].nuOrdem == 1
    assert fake_api.json_bodies()[-1] == {"filter": {"subfila": 167}}
    assert len(client.fila.consulta(161)) == 35  # a second recorded queue; sizes vary, no cap


def test_empty_queue_is_an_empty_404_returned_as_a_list(fake_api):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=load("token.json"))
        return httpx.Response(404, headers={"X-RateLimit-Remaining": "24"})  # no body at all

    with Client(
        Credentials("id", "s"),
        transport=httpx.MockTransport(handler),
        throttle=Throttle(sleep=lambda s: None),
    ) as c:
        assert c.fila.consulta(1721) == []
        assert c.lista.consulta(1721) == []
        with pytest.raises(NotFoundError):  # other endpoints keep raising
            c.udi.get(999999)


def test_lista_chain_uses_the_subfila_key(client, fake_api):
    areas = client.lista.areas()
    assert [a.id for a in areas] == [7, 15, 1, 9]
    grupos = client.lista.grupos(1)
    assert grupos[0].descricao == "Bula, Rotulagem e Nome Comercial"
    subs = client.lista.sublistas(921)
    assert [s.id for s in subs] == [2141]
    rows = client.lista.consulta(2141)
    assert len(rows) == 555 and rows[0].numeroProcessoFormatado == "25351.459189/2024-70"
    assert fake_api.json_bodies()[-1] == {"filter": {"subfila": 2141}}


def test_nome_tecnico_search_and_categorias(client, fake_api):
    page = client.nome_tecnico.search(size=2)
    assert isinstance(page, models.PageNomeTecnicoDTO)
    assert (page.totalElements, page.number, page.last) == (2678, 0, False)
    assert page.content[0].classeRisco == "II"
    assert fake_api.json_bodies()[-1] == {"page": 1, "size": 2, "sorting": {}, "filter": {}}
    assert [c.id for c in client.nome_tecnico.categorias()] == [8, 12]


def test_every_request_carries_ua_and_bearer(client, fake_api):
    client.fila.areas()
    api_calls = [r for r in fake_api.requests if "/api/v1/" in r.url.path]
    assert api_calls[0].headers["User-Agent"].startswith("anvisa-python/")
    assert api_calls[0].headers["Authorization"] == "Bearer FAKE.TOKEN.FOR-TESTS"
    assert client.throttle.remaining == 24  # updated from the recorded header


def test_udi_search_sends_1_based_page_and_filters(client, fake_api):
    page = client.udi.search(nomeComercial="cateter", size=2)
    assert isinstance(page, models.PageUdiDTO)
    assert (page.totalElements, page.number) == (69, 0)
    assert fake_api.json_bodies()[-1] == {
        "page": 1,
        "size": 2,
        "sorting": {},
        "filter": {"nomeComercial": "cateter"},
    }


@pytest.mark.parametrize(
    ("filters", "total"),
    [
        ({"udiDi": "07898620922696"}, 1),
        ({"cnpjDetentora": "06167295000171"}, 12),
        ({"codigoGmdn": "47852"}, 4),
        ({"nuRegistro": "80454410018"}, 4),
    ],
)
def test_udi_verified_filter_keys(client, fake_api, filters, total):
    page = client.udi.search(size=2, **filters)  # routed to the fixture recorded for this body
    assert page.totalElements == total
    assert page.content[0].id == 377
    assert fake_api.json_bodies()[-1]["filter"] == filters


def test_udi_historicos_and_gmdn_search(client, fake_api):
    snapshots = client.udi.historicos()
    assert len(snapshots) == 171
    first = snapshots[0]
    assert (first.id, first.tipoHistorico, first.totalRegistros) == (171, "DIARIO", 88)
    page = client.udi.termos_gmdn(size=2, conteudo="pacing")
    assert page.totalElements == 3
    assert page.content[1].codigo == "47852"


def test_nome_tecnico_verified_filters(client):
    assert client.nome_tecnico.search(size=2, nomeTecnico="ANIDROGLUCITOL").totalElements == 1
    assert client.nome_tecnico.search(size=2, categoriaProduto="12").totalElements == 993


def test_assunto_catalogs(client):
    tipos = client.assunto.tipos_solicitacao()
    assert len(tipos) == 2 and tipos[0].valor == "S"
    assert len(client.assunto.tipos_produto()) == 13
    assert client.assunto.sistemas()[0].id == "COSMETICOS"
    assert len(client.assunto.servicos()) == 378


def test_udi_search_without_filter_fails_before_any_request(client, fake_api):
    with pytest.raises(MissingFilterError):
        client.udi.search()
    assert fake_api.requests == []


def test_page_below_one_fails_locally():
    with pytest.raises(InvalidPageError):
        page_body(0, 10, None, {"a": 1})


def test_udi_get_and_gmdn(client):
    detail = client.udi.get(377)
    assert detail.dispositivo.nomeComercial.startswith("CATETER ELETRODO")
    assert detail.autorizacaoFuncionamento.cnpj == "06167295000171"
    termo = client.udi.termo_gmdn("47852")
    assert termo.nomeOriginal == "Temporary cardiac pacing catheter"


def test_assunto_lista_and_detalhe(client):
    assuntos = client.assunto.lista()
    assert len(assuntos) == 2595
    assert assuntos[0].id == 10013

    detalhe = client.assunto.detalhe(10013)
    assert detalhe.assunto.startswith("BIOEQUIVALÊNCIA")
    assert detalhe.sistemas[0].codigoSistema == "SOLICITA"
    assert len(detalhe.documentosRequeridos) == 5
    assert [t.porte for t in detalhe.valoresTaxaEmpresa][0] == "Grande I"


def test_iter_search_walks_pages_with_0_based_response_numbers():
    first = load("udi_filtro.json")
    second = dict(
        first, number=1, first=False, last=True, content=[dict(first["content"][0], id=999)]
    )
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=load("token.json"))
        body = json.loads(request.read())
        bodies.append(body)
        return httpx.Response(200, json=first if body["page"] == 1 else second)

    with Client(
        Credentials("id", "s"),
        transport=httpx.MockTransport(handler),
        throttle=Throttle(sleep=lambda s: None),
    ) as c:
        ids = [d.id for d in c.udi.iter_search(size=2, nomeComercial="cateter")]
    assert ids == [377, 378, 999]
    assert [b["page"] for b in bodies] == [1, 2]


def test_iterate_pages_stops_on_empty_content():
    class Page:
        content = []
        number = 0
        last = False

    assert list(iterate_pages(lambda p: Page())) == []
