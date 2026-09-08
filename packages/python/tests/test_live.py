"""Opt-in smoke test against the real API: 4 requests + 1 token. Run with `pytest -m live`."""

import pytest

from anvisa.auth import Credentials
from anvisa.client import Client
from anvisa.errors import CredentialsError

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live():
    try:
        creds = Credentials.from_env()
    except CredentialsError as exc:
        pytest.skip(str(exc))
    with Client(creds) as client:
        yield client


def test_live_fila_and_udi(live: Client):
    areas = live.fila.areas()
    assert any(a.descricao == "Dispositivos Médicos" for a in areas)
    assert live.throttle.burst == 25 and live.throttle.rate == 1

    subfilas = live.fila.subfilas(285)
    assert subfilas

    page = live.udi.search(nomeComercial="cateter", size=1)
    assert page.content and page.totalElements >= 1


def test_live_download_is_a_spreadsheet(live: Client):
    download = live.fila.download(167)
    assert download.filename == "consulta_fila.xlsx"
    assert download.content[:2] == b"PK"  # OOXML, whatever the content type says
