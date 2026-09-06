import json

import httpx
import pytest
from typer.testing import CliRunner

from anvisa import cli
from anvisa.auth import Credentials
from anvisa.client import Client
from anvisa.errors import CredentialsError
from anvisa.throttle import Throttle

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


def test_fila_areas_json(fake_cli_client):
    result = runner.invoke(cli.app, ["--format", "json", "fila", "areas"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 11 and data[0] == {"id": 6, "descricao": "Alimento"}


def test_fila_consulta_table(fake_cli_client):
    result = runner.invoke(
        cli.app, ["-f", "table", "fila", "consulta", "167"], env={"COLUMNS": "250"}
    )
    assert result.exit_code == 0, result.output
    assert "25351.216322/2025-86" in result.output
    assert "2026-08-18" in result.output  # dtEntrada rendered as a date


def test_udi_search_and_get(fake_cli_client):
    result = runner.invoke(
        cli.app, ["-f", "json", "udi", "search", "--nome", "cateter", "--size", "2"]
    )
    assert result.exit_code == 0, result.output
    assert [d["udiDi"] for d in json.loads(result.output)] == ["07898620922696", "07898620922702"]
    assert fake_cli_client.json_bodies()[-1]["filter"] == {"nomeComercial": "cateter"}

    result = runner.invoke(cli.app, ["-f", "json", "udi", "get", "377"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["dispositivo"]["versaoModelo"] == "VCET-5110CK1"


def test_udi_search_without_filter_is_a_clean_error(fake_cli_client):
    result = runner.invoke(cli.app, ["udi", "search"])
    assert result.exit_code == 1
    assert "at least one filter" in result.output


def test_missing_credentials_exit_2(monkeypatch):
    def no_client():
        raise CredentialsError("no ANVISA credentials")

    monkeypatch.setattr(cli, "make_client", no_client)
    result = runner.invoke(cli.app, ["fila", "areas"])
    assert result.exit_code == 2
    assert "no ANVISA credentials" in result.output
