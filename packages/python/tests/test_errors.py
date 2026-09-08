from datetime import datetime

import httpx
import pytest
from conftest import response_for

from anvisa.errors import (
    ApiError,
    AuthError,
    BlockedError,
    EmptyExportError,
    InvalidPageError,
    MalformedRequestError,
    MissingFilterError,
    NoResultError,
    NotAcceptableError,
    NotFoundError,
    RateLimitError,
    RequestRejectedError,
    parse_data_hora,
    raise_for_response,
)


@pytest.mark.parametrize(
    ("fixture", "exc"),
    [
        ("err_msg062", MissingFilterError),
        ("err_subfila", MissingFilterError),
        ("err_sublista", MissingFilterError),
        ("err_page_index", InvalidPageError),
        ("err_jackson", MalformedRequestError),
        ("err_formulario_body", MalformedRequestError),
        ("err_not_acceptable", NotAcceptableError),
        ("err_export_vazia", EmptyExportError),
        ("err_formulario_inexistente", NoResultError),
    ],
)
def test_validation_failures_reported_as_500_become_typed(fixture, exc):
    with pytest.raises(exc) as info:
        raise_for_response(response_for(fixture))
    assert info.value.status_code == 500
    assert isinstance(info.value.data_hora, datetime)


@pytest.mark.parametrize("fixture", ["err_subfila", "err_sublista"])
def test_missing_filter_names_the_key(fixture):
    # lista/consulta asks for `subfila` too, not `sublista`
    with pytest.raises(MissingFilterError) as info:
        raise_for_response(response_for(fixture))
    assert info.value.filters == ["subfila"]
    assert "subfila" in str(info.value)


def test_msg062_is_explained():
    with pytest.raises(MissingFilterError, match="at least one filter"):
        raise_for_response(response_for("err_msg062"))


def test_documented_but_undeployed_endpoint_is_not_found():
    # portal-documented endpoints answer Spring's default 404 body, not the ErroApi envelope
    with pytest.raises(NotFoundError) as info:
        raise_for_response(response_for("err_not_deployed"))
    assert "/api/v1/empresa/" in str(info.value)


def test_assunto_busca_is_a_plain_api_error():
    # the body is not bound server-side; nothing to map, but the message must survive
    with pytest.raises(ApiError) as info:
        raise_for_response(response_for("err_assunto_busca"))
    assert not isinstance(info.value, RequestRejectedError)
    assert "filtro" in (info.value.mensagem_detalhada or "")


def test_download_errors_explain_themselves():
    with pytest.raises(NotAcceptableError, match=r"Accept: \*/\*"):
        raise_for_response(response_for("err_not_acceptable"))
    with pytest.raises(EmptyExportError, match="nothing to export"):
        raise_for_response(response_for("err_export_vazia"))


def test_udi_download_historico_npe_stays_a_plain_api_error():
    # the export dereferences a null instead of 404ing; nothing to map, keep the message
    with pytest.raises(ApiError) as info:
        raise_for_response(response_for("err_udi_download_historico"))
    assert not isinstance(info.value, RequestRejectedError)
    assert "getDispositivo()" in info.value.mensagem_detalhada


def test_success_is_silent():
    raise_for_response(response_for("areafila"))


def _resp(status, **kw):
    return httpx.Response(status, request=httpx.Request("GET", "https://example/x"), **kw)


def test_cloudflare_block():
    with pytest.raises(BlockedError, match="User-Agent"):
        raise_for_response(
            _resp(
                403, headers={"Content-Type": "text/html"}, text="<html>Attention Required!</html>"
            )
        )


@pytest.mark.parametrize(
    ("status", "exc"), [(401, AuthError), (404, NotFoundError), (429, RateLimitError)]
)
def test_plain_status_codes(status, exc):
    with pytest.raises(exc):
        raise_for_response(_resp(status))


def test_unknown_envelope_and_non_json():
    with pytest.raises(ApiError) as info:
        raise_for_response(
            _resp(
                500,
                json={
                    "status": "INTERNAL_SERVER_ERROR",
                    "mensagem": "boom",
                    "mensagem_detalhada": "",
                },
            )
        )
    assert info.value.mensagem == "boom"
    with pytest.raises(ApiError, match="Bad Gateway"):
        raise_for_response(_resp(502, text="Bad Gateway"))


def test_parse_data_hora():
    assert parse_data_hora([2026, 9, 6, 15, 38, 30, 879369873]) == datetime(
        2026, 9, 6, 15, 38, 30, 879369
    )
    assert parse_data_hora(None) is None
    assert parse_data_hora([2026, 13, 1, 0, 0, 0, 0]) is None
