"""anvisa.dados: parsing ANVISA's CSV dialect, downloading, converting to Parquet, building."""

from __future__ import annotations

import dataclasses
import hashlib
import html
import importlib.util
import json
import re
import shutil
import sys
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import headers_only
from typer.testing import CliRunner

from anvisa import cli
from anvisa.dados import (
    CATALOG,
    DERIVADAS,
    Unchanged,
    build,
    busca,
    cmed,
    dcb,
    derivar,
    fetch,
    parse,
    select,
)
from anvisa.dados.catalog import (
    ALIMENTOS,
    ALIMENTOS_RESULTADO,
    CBPF,
    CBPF_MEDICAMENTOS,
    CMED,
    COSMETICOS,
    DCB,
    MEDICAMENTOS,
    PETICOES_ALIMENTO,
    PETICOES_ALIMENTO_ANDAMENTO,
    PRODUTOS_IRREGULARES,
    PRODUTOS_SAUDE,
    SANEANTES,
    TYPES,
)
from anvisa.dados.descricoes import DESCRICOES
from anvisa.dados.fetch import cmed_stamp, download, fetch_source, http_client, local_dcb
from anvisa.dados.parse import (
    check_header,
    decode,
    normalize,
    read_header,
    records,
    stream,
    unescape,
)
from anvisa.errors import DadosError, SchemaDriftError

DADOS = Path(__file__).resolve().parents[3] / "fixtures" / "dados"
SAMPLES = {
    ALIMENTOS.name: "alimentos_head.csv",
    ALIMENTOS_RESULTADO.name: "alimentos_resultado_head.csv",
    SANEANTES.name: "saneantes_head.csv",
    PETICOES_ALIMENTO.name: "peticoes_alimento_head.csv",
    PETICOES_ALIMENTO_ANDAMENTO.name: "peticoes_alimento_andamento_head.csv",
    PRODUTOS_IRREGULARES.name: "produtos_irregulares_head.csv",
    COSMETICOS.name: "cosmeticos_head.csv",
    MEDICAMENTOS.name: "medicamentos_head.csv",
    PRODUTOS_SAUDE.name: "produtos_saude_head.csv",
    CBPF.name: "cbpf_head.csv",
    CBPF_MEDICAMENTOS.name: "cbpf_medicamentos_head.csv",
}
HEADERS = {
    ALIMENTOS.name: "headers_alimentos.txt",
    ALIMENTOS_RESULTADO.name: "headers_alimentos_resultado.txt",
    SANEANTES.name: "headers_saneantes.txt",
    PETICOES_ALIMENTO.name: "headers_peticoes_alimento.txt",
    PETICOES_ALIMENTO_ANDAMENTO.name: "headers_peticoes_alimento_andamento.txt",
    PRODUTOS_IRREGULARES.name: "headers_produtos_irregulares.txt",
    COSMETICOS.name: "headers_cosmeticos.txt",
    MEDICAMENTOS.name: "headers_medicamentos.txt",
    PRODUTOS_SAUDE.name: "headers_produtos_saude.txt",
    CBPF.name: "headers_cbpf.txt",
    CBPF_MEDICAMENTOS.name: "headers_cbpf_medicamentos.txt",
}
# the CMED pair is read by cmed.py, not the Windows-1252 parser: its samples are separate
CMED_SAMPLES = {"pmc": "cmed_pmc_head.csv", "pmvg": "cmed_pmvg_head.csv"}
CMED_HEADERS = {"pmc": "headers_cmed_pmc.txt", "pmvg": "headers_cmed_pmvg.txt"}
# the DCB comes from the Biblioteca Digital: collection page, item page, then the xlsx
DCB_ITEM_URL = "https://bibliotecadigital.anvisa.gov.br/jspui/handle/anvisa/22551"
DCB_XLSX_URL = (
    "https://bibliotecadigital.anvisa.gov.br/jspui/bitstream/anvisa/22551/1/"
    "3-Lista%20DCB%20consolidada%20out%202026.xlsx"
)
DCB_TITLE = "Lista consolidada das DCB: versão IN Anvisa n° 473, de 2 de outubro de 2026 - VIGENTE"
DCB_SAMPLE = DADOS / "dcb_head.xlsx"
CSV_DATASETS = tuple(ds for ds in CATALOG if ds.kind == "csv")
NOW = datetime(2026, 10, 6, 21, 3, 12, tzinfo=timezone.utc)
needs_duckdb = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None, reason="needs the anvisa[dados] extra"
)


def sample(ds) -> Path:
    return DADOS / SAMPLES[ds.name]


def catalog_url(file: str) -> str:
    return f"https://dados.anvisa.gov.br/dados/{file}"


def sample_records(ds) -> list[list[str]]:
    text = decode(sample(ds).read_bytes())
    if not ds.quoted:
        return list(parse.unquoted([text]))
    return list(records(text, len(ds.columns)))


def write_dialect(path: Path, rows: list[list[str]]) -> Path:
    """Write rows the way ANVISA does: every field quoted, nothing escaped, Windows-1252."""
    path.write_bytes(
        "".join(";".join(f'"{v}"' for v in row) + "\n" for row in rows).encode("cp1252")
    )
    return path


class FakeDados:
    """Serves the fixture samples at their catalog URLs with the recorded headers, honours
    If-None-Match (If-Modified-Since for a file without ETag, the DCB xlsx), serves the
    Biblioteca Digital's two pages (`pages`, not data files) and `manifest` (when set) at
    MANIFEST_URL. A URL in `status` answers that status instead (a source refusing the runner)."""

    SITE_URL = "https://example.github.io/anvisa-api/"
    MANIFEST_URL = SITE_URL + "manifest.json"

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.manifest: dict | None = None
        self.manifest_status = 200
        self.site: Path | None = None  # a previous build's `out`, served under SITE_URL
        self.status: dict[str, int] = {}
        self.files = {}
        for ds in CSV_DATASETS:
            headers, _ = headers_only(DADOS / HEADERS[ds.name])
            headers.pop("Content-Length")  # recorded for the full file; httpx sets the sample's
            self.files[ds.url] = (headers, sample(ds).read_bytes())
        for kind, file in cmed.FILES.items():
            headers, _ = headers_only(DADOS / CMED_HEADERS[kind])
            headers.pop("Content-Length")
            self.files[catalog_url(file)] = (headers, (DADOS / CMED_SAMPLES[kind]).read_bytes())
        headers, _ = headers_only(DADOS / "headers_dcb.txt")
        headers.pop("Content-Length")
        self.files[DCB_XLSX_URL] = (headers, DCB_SAMPLE.read_bytes())
        self.pages = {
            DCB.url: (DADOS / "dcb_colecao.html").read_bytes(),
            DCB_ITEM_URL: (DADOS / "dcb_item.html").read_bytes(),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.status:
            return httpx.Response(self.status[url], text="blocked")
        if url == self.MANIFEST_URL:
            if self.manifest is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(self.manifest_status, json=self.manifest)
        if url.startswith(self.SITE_URL):
            file = self.site / url.removeprefix(self.SITE_URL) if self.site else None
            if file is None or not file.is_file():
                return httpx.Response(404, text="not found")
            return httpx.Response(200, content=file.read_bytes())
        if url in self.pages:
            html_type = {"Content-Type": "text/html;charset=UTF-8"}
            return httpx.Response(200, headers=html_type, content=self.pages[url])
        if url not in self.files:
            return httpx.Response(404, text="not found")
        headers, body = self.files[url]
        if "ETag" in headers and request.headers.get("If-None-Match") == headers["ETag"]:
            return httpx.Response(304, headers={"ETag": headers["ETag"]})
        if "ETag" not in headers and request.headers.get("If-Modified-Since") == headers.get(
            "Last-Modified"
        ):
            return httpx.Response(304, headers={"Last-Modified": headers["Last-Modified"]})
        return httpx.Response(200, headers=headers, content=body)

    def client(self) -> httpx.Client:
        return http_client(transport=httpx.MockTransport(self.handler))

    def data_requests(self) -> list[httpx.Request]:
        """The source downloads: not the manifest, not a carried-over Parquet, not the DCB's
        HTML pages."""
        return [r for r in self.requests if str(r.url) in self.files]


@pytest.fixture
def fake_dados() -> FakeDados:
    return FakeDados()


# --- catalog and dialect ------------------------------------------------------


def test_catalog_is_consistent():
    assert len({d.name for d in CATALOG}) == len(CATALOG)
    for ds in CATALOG:
        assert set(ds.columns.values()) <= TYPES
        assert set(ds.sort) <= set(ds.columns)
        assert all(ds.columns[c] == "VARCHAR" for c in ds.unescape)
        assert all(ds.columns[c] in ("DATE", "TIMESTAMP") for c in ds.formats)
        assert ds.url.startswith(
            "https://bibliotecadigital.anvisa.gov.br/"
            if ds.kind == "dcb"
            else "https://dados.anvisa.gov.br/"
        )
        assert ds.kind != "csv" or ds.url.endswith(ds.file)
        assert ds.directory == "" or ds.directory.endswith("/")
        assert not ds.directory.startswith("/") and "//" not in ds.url.split("://", 1)[1]
    # ANVISA reuses file names across folders, so the work directory is keyed by dataset
    assert len({d.local_file for d in CATALOG}) == len(CATALOG)
    with pytest.raises(ValueError, match="must be '' or end in '/'"):
        dataclasses.replace(ALIMENTOS, directory="CONSULTAS/PRODUTOS")
    assert select(["alimentos"]) == (ALIMENTOS, ALIMENTOS_RESULTADO)  # the group
    assert select(["alimentos_resultado"]) == (ALIMENTOS_RESULTADO,)
    assert select(["saneantes", "alimentos_resultado"]) == (ALIMENTOS_RESULTADO, SANEANTES)
    assert select(["peticoes_alimento"]) == (PETICOES_ALIMENTO, PETICOES_ALIMENTO_ANDAMENTO)
    assert PETICOES_ALIMENTO.url == (
        "https://dados.anvisa.gov.br/dados/CICLO_ANALISE_PETICOES_ALIMENTO.CSV"
    )
    with pytest.raises(DadosError, match="unknown dataset"):
        select(["cannabis"])
    # the group `medicamentos` is the registry and the price list, in catalog order
    assert select(["medicamentos"]) == (MEDICAMENTOS, CMED) and select(["cmed"]) == (CMED,)
    assert select(["produtos_saude"]) == (PRODUTOS_SAUDE,)
    assert MEDICAMENTOS.quoted and not PRODUTOS_SAUDE.quoted
    assert CMED.url == catalog_url("TA_PRECO_MEDICAMENTO.csv") and CMED.local_file == "cmed.csv"
    assert (
        CMED.source_name("pf_17_5_alc") == "PF 17,5 % ALC" and CMED.source_name("tarja") == "TARJA"
    )
    with pytest.raises(ValueError, match="unknown kind"):
        dataclasses.replace(ALIMENTOS, kind="xlsx")
    assert select(["cosmeticos"]) == (COSMETICOS,)
    # the CBPF pair shares a group and a folder
    assert select(["cbpf"]) == (CBPF, CBPF_MEDICAMENTOS)
    assert CBPF_MEDICAMENTOS.url == catalog_url(
        "CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/TA_CONSULTA_CBPF_MEDICAMENTO_DETALHE.CSV"
    )
    # the DCB: its own group, found through the Biblioteca Digital collection
    assert select(["dcb"]) == (DCB,) and DCB.url == dcb.COLLECTION_URL
    assert DCB.local_file == "dcb.csv" and DCB.columns == dcb.COLUMNS
    assert DCB.source_name("cas_referencia") == "Nº CAS ([Ref. n])"


def test_fixture_header_matches_catalog():
    for ds in CSV_DATASETS:
        assert read_header(sample(ds)) == list(ds.columns)
        check_header(ds, read_header(sample(ds)))
    # the CMED pair: every source column maps to an output column (or is ignored on purpose)
    for kind, file in CMED_SAMPLES.items():
        header = cmed.read_header(DADOS / file)
        assert len(header) == 74 and header[0] == "SUBSTÂNCIA"  # the BOM is stripped
        mapped = [cmed.column_for(h, kind) for h in header]
        assert mapped.count(None) == 1  # DESTINAÇÃO COMERCIAL
        assert set(mapped) - {None} <= set(cmed.COLUMNS)
    # the DCB workbook: the header row below the title, `Nº DCB ` with its trailing space
    header = dcb.read_header(DCB_SAMPLE)
    assert header == [
        "Nº DCB ",
        "DENOMINAÇÃO COMUM BRASILEIRA",
        "Nº CAS",
        "CLASSIFICAÇÃO",
        "HISTÓRICO",
    ]
    dcb.check_header(header)
    # the `#` comment marker on the finalized-petitions header is dropped, not a schema change
    assert sample(PETICOES_ALIMENTO).read_bytes().startswith(b"#NUM_EXPEDIENTE_PETICAO;")


PUBLISHED = [ds.name for ds in CATALOG] + [d.name for d in DERIVADAS]  # manifest order


def test_descriptions_cover_every_column():
    """Every published column has a Portuguese description and every table its notes: the
    manifest and the Pages dictionary are generated from them. The derived tables' columns
    are checked against the built file in test_produtos_on_the_fixtures."""
    for ds in CATALOG:
        assert set(ds.descriptions) == set(ds.columns), ds.name
        assert all(text.strip() for text in ds.descriptions.values()), ds.name
        assert ds.notes and all(note.strip() for note in ds.notes), ds.name
    for d in DERIVADAS:
        assert all(text.strip() for text in d.descriptions.values()), d.name
        assert d.notes and set(d.sort) <= set(d.descriptions), d.name
        assert set(d.fontes) <= {ds.name for ds in CATALOG}, d.name
    assert set(DESCRICOES) == set(PUBLISHED)


def test_header_drift_is_loud():
    header = list(ALIMENTOS.columns)
    header[2] = "NU_PROCESSO_NOVO"
    with pytest.raises(SchemaDriftError) as exc:
        check_header(ALIMENTOS, header)
    message = str(exc.value)
    assert "NU_PROCESSO_NOVO" in message and "'NU_PROCESSO'" in message
    assert "column 3" in message and "TA_CONSULTA_ALIMENTOS.CSV" in message


def test_dialect_keeps_every_record_whole():
    """The fixtures hold the cases that break stock CSV readers (see parse.py)."""
    for ds in CSV_DATASETS:
        rows = sample_records(ds)
        assert {len(r) for r in rows} == {len(ds.columns)}
    main = sample_records(ALIMENTOS)
    names = [r[0] for r in main]
    assert any("\n" in n for n in names)  # a line break inside a quoted field
    assert any('"' in n for n in names)  # an unescaped quote inside a quoted field
    detail = {r[0]: r for r in sample_records(ALIMENTOS_RESULTADO)[1:]}
    assert 'biscoito tipo "cookies"' in detail["429807342"][12]
    ending = next(r for r in detail.values() if r[15] == "3929988")
    assert ending[12].endswith('brilhante. "')  # written as `brilhante. "";"Contém...`
    assert ending[13] == "Contém Glúten - Não | Contém Lactose - Não"
    # saneantes: an inner `"` followed by CRLF CRLF, which only the record width disambiguates
    meninas = [r[0] for r in sample_records(SANEANTES) if "AS MENINAS" in r[0]]
    assert any(n.endswith('"AS MENINAS"\r\n\r\nSPRAY') for n in meninas), meninas


def test_records_tells_inner_quotes_from_truncation():
    """A `"` + line break inside a non-last field is read as part of the value only when the
    width-blind reading of the same span cannot parse; a truncated record stays a short one."""
    normal = '"X";"1";"2"\n'
    inner = '"A "B"\n\nC";"1";"2"\n'
    assert list(records(inner + normal, 3)) == [['A "B"\n\nC', "1", "2"], ["X", "1", "2"]]
    truncated = '"A";"1"\n'
    assert list(records(truncated + normal, 3)) == [["A", "1"], ["X", "1", "2"]]
    # the same truncated record, but the next one begins with a bare field: still two records
    assert list(records(truncated + '7;"1";"2"\n', 3)) == [["A", "1"], ["7", "1", "2"]]


def test_decode_is_windows_1252():
    assert decode(b"LEDA SIM\xd5ES \x96 FI 6\x92 DR\x99") == "LEDA SIMÕES – FI 6’ DR™"
    assert decode(b"\x81\x9d") == "\x81\x9d"  # undefined in cp1252: passed through, no error
    text = decode(sample(ALIMENTOS).read_bytes())
    assert "–" in text and not re.search("[\x80-\x9f]", text)


def test_unescape_only_terminated_entities():
    assert unescape("L&apos;ANA MED ; &quot;X&quot; ; A&#8208;B") == 'L\'ANA MED ; "X" ; A‐B'
    assert unescape("M&notícia & P&G &amp") == "M&notícia & P&G &amp"


def test_normalize_skips_and_reports_short_records(tmp_path):
    rows = sample_records(ALIMENTOS)
    rows[3] = rows[3][:-1]
    src = write_dialect(tmp_path / "in.csv", rows)
    result = normalize(ALIMENTOS, src, tmp_path / "out.csv")
    assert result.rejected == (3,) and result.rows == len(rows) - 2
    assert normalize(ALIMENTOS, src, tmp_path / "out5.csv", chunk_size=5) == result


def chunked(text, n: int):
    return (text[i : i + n] for i in range(0, len(text), n))


def test_stream_matches_records_at_every_chunk_size():
    """Chunk boundaries cannot change a reading: the end-of-text rule only applies to the last
    chunk, and a field cut short by the buffer is parsed again with the next one."""
    for ds in CSV_DATASETS:
        raw = sample(ds).read_bytes()
        width = len(ds.columns)
        text = decode(raw)
        if not ds.quoted:  # the line reader: a chunk may cut a line anywhere
            for n in (1, 2, 3, 7, 64, 4096, len(text) + 1):
                assert list(parse.unquoted(chunked(text, n))) == sample_records(ds), (ds.name, n)
            assert list(parse.unquoted(map(decode, chunked(raw, 7)))) == sample_records(ds)
            continue
        for n in (1, 2, 3, 7, 64, 4096, len(text) + 1):
            assert list(stream(chunked(text, n), width)) == sample_records(ds), (ds.name, n)
        # the way `normalize` feeds it: bytes cut anywhere, decoded piece by piece
        assert list(stream(map(decode, chunked(raw, 7)), width)) == sample_records(ds)
        # no trailing line break: `\Z` closes the last record
        bare = text.rstrip("\r\n")
        assert list(stream(chunked(bare, 5), width)) == list(records(bare, width))


def test_stream_boundaries_do_not_change_a_reading():
    """Every possible cut of the texts that exercise the dialect's corner cases."""
    normal = '"X";"1";"2"\n'
    texts = [
        '"a";"b";"c"\r\n"d";"e";"f"\r\n',  # cut between `"` and `\r`, between `\r` and `\n`
        '"A "B"\n\nC";"1";"2"\n' + normal,  # inside the inner `"..."\n` (the merged path)
        '"A";"1"\n' + normal,  # a truncated record, re-split the width-blind way
        '"A";"1"\n7;"1";"2"\n',  # the same, but the next record begins with a bare field
        'x;y;z\r\n"q";"r\r\n";"s"\r\n',  # bare fields; CRLF inside a quoted non-last field
        "x;y;z",  # bare, no line break at all
        '"a";"b";',  # a trailing partial record (dropped, as `records` drops it)
    ]
    for text in texts:
        expected = list(records(text, 3))
        for n in range(1, len(text) + 2):
            assert list(stream(chunked(text, n), 3)) == expected, (text, n)


def test_stream_errors_match_whole_text(tmp_path):
    """A malformed file fails at the same character with the same context, however it is cut."""
    normal = '"X";"1";"2"\n'
    texts = [
        normal * 5 + 'ab"c;"1";"2"\n',  # a quote in a bare field, beyond 40 characters in
        "x;y;z\rq;r;s\n",  # a lone `\r`
        '"a";"b"\n',  # a short last record is fine...
        '"a";"b";"c"\r',  # ...but a `"` + `\r` + end of file is not
        '"a";"b";"c\n',  # an unclosed quote
    ]
    for text in texts:
        try:
            expected = list(records(text, 3))
        except DadosError as exc:
            expected = str(exc)
        for n in (1, 3, 7, 1000):
            try:
                got = list(stream(chunked(text, n), 3))
            except DadosError as exc:
                got = str(exc)
            assert got == expected, (text, n)
    rows = sample_records(ALIMENTOS)
    src = write_dialect(tmp_path / "in.csv", rows)
    # a quote inside a quoted value is fine; a bare field with one is not
    quoted = b'"' + rows[5][0].encode("cp1252") + b'"'
    broken = src.read_bytes().replace(quoted, b'ab"c', 1)
    src.write_bytes(broken)
    with pytest.raises(DadosError, match="unparseable CSV at character") as whole:
        normalize(ALIMENTOS, src, tmp_path / "out.csv")
    with pytest.raises(DadosError) as streamed:
        normalize(ALIMENTOS, src, tmp_path / "out7.csv", chunk_size=7)
    assert str(streamed.value) == str(whole.value)


def test_stream_caps_a_record_that_never_ends(monkeypatch):
    monkeypatch.setattr(parse, "MAX_RECORD", 100)
    it = stream(chunked('"never closes;' + "x" * 500, 50), 3)
    with pytest.raises(DadosError, match="no record ends within the next 100 characters"):
        list(it)


def test_normalize_output_is_chunk_size_independent(tmp_path):
    whole = normalize(ALIMENTOS, sample(ALIMENTOS), tmp_path / "whole.csv")
    small = normalize(ALIMENTOS, sample(ALIMENTOS), tmp_path / "small.csv", chunk_size=7)
    assert whole == small
    assert (tmp_path / "whole.csv").read_bytes() == (tmp_path / "small.csv").read_bytes()


# --- download -----------------------------------------------------------------


def test_download_streams_via_part(fake_dados, tmp_path):
    with fake_dados.client() as http:
        source = download(http, ALIMENTOS, tmp_path)
    body = sample(ALIMENTOS).read_bytes()
    assert source.etag == '"20230eb-65d1981268f1f"'
    assert source.last_modified == "Mon, 05 Oct 2026 15:26:22 GMT"
    assert source.bytes == len(body) and source.sha256 == hashlib.sha256(body).hexdigest()
    assert (tmp_path / ALIMENTOS.local_file).read_bytes() == body
    assert not list(tmp_path.glob("*.part"))
    request = fake_dados.requests[0]
    assert request.headers["User-Agent"].startswith("anvisa-python/")
    assert request.headers["Accept-Encoding"] == "identity"
    assert "If-None-Match" not in request.headers


def test_download_304_returns_none(fake_dados, tmp_path):
    with fake_dados.client() as http:
        assert download(http, ALIMENTOS, tmp_path, etag='"20230eb-65d1981268f1f"') is None
        assert download(http, ALIMENTOS, tmp_path, last_modified="Mon, 05 Oct 2026") is not None
    assert fake_dados.requests[1].headers["If-Modified-Since"] == "Mon, 05 Oct 2026"
    assert [p.name for p in tmp_path.iterdir()] == [ALIMENTOS.local_file]


def test_download_truncated_is_an_error(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"Content-Length": "100"}, content=b"short")

    with (
        http_client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(DadosError, match="truncated"),
    ):
        download(http, ALIMENTOS, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_http_errors_are_dados_errors(tmp_path, monkeypatch):
    pauses = []
    monkeypatch.setattr(fetch, "_sleep", pauses.append)
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        if "RESULTADO" in str(request.url):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(503, text="down")

    with http_client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DadosError, match="HTTP 503"):
            download(http, ALIMENTOS, tmp_path)
        with pytest.raises(DadosError, match=r"boom \(after 3 attempts\)"):
            download(http, ALIMENTOS_RESULTADO, tmp_path)
    assert list(tmp_path.iterdir()) == []
    # a status is an answer: one request; a dropped connection is retried after each pause
    assert len([u for u in attempts if "RESULTADO" not in u]) == 1
    assert len([u for u in attempts if "RESULTADO" in u]) == 3
    assert pauses == list(fetch.BACKOFF)


def test_download_retries_a_dropped_connection(fake_dados, tmp_path, monkeypatch):
    # dados.anvisa.gov.br, 2026-10-23: "Server disconnected without sending a response" twice
    # in a row, on different files, each killing a dados.yml run
    monkeypatch.setattr(fetch, "_sleep", lambda pause: None)
    failures = iter([True, True, False])
    real = fake_dados.handler

    def handler(request):
        if next(failures):
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request
            )
        return real(request)

    with http_client(transport=httpx.MockTransport(handler)) as http:
        source = download(http, ALIMENTOS, tmp_path)
    assert source is not None and source.bytes == len(sample(ALIMENTOS).read_bytes())
    assert (tmp_path / ALIMENTOS.local_file).exists()
    assert not list(tmp_path.glob("*.part"))


# --- convert ------------------------------------------------------------------


def converted(ds, tmp_path, **kwargs):
    from anvisa.dados.convert import convert

    csv = tmp_path / ds.file
    csv.write_bytes(sample(ds).read_bytes())
    parquet = tmp_path / f"{ds.name}.parquet"
    return convert(ds, csv, parquet, **kwargs), parquet


@needs_duckdb
def test_convert_types_and_values(tmp_path):
    import duckdb

    _, parquet = converted(ALIMENTOS, tmp_path)
    con = duckdb.connect()
    described = con.sql(f"DESCRIBE SELECT * FROM '{parquet}'").fetchall()
    assert [(name, kind) for name, kind, *_ in described] == [
        (c.lower(), t) for c, t in ALIMENTOS.columns.items()
    ]

    def one(sql):
        return con.sql(sql.replace("FROM T", f"FROM '{parquet}'")).fetchone()

    # MMYYYY and MM/YYYY month of expiry; 122029 is every notificação's placeholder
    sample_row = "SELECT dt_vencimento_registro FROM T WHERE nu_cnpj_empresa = '40208221000174'"
    assert one(sample_row) == (date(2029, 12, 1),)
    assert one("SELECT count(*) FROM T WHERE dt_vencimento_registro = DATE '2031-03-01'")[0] == 1
    assert one("SELECT count(*) FROM T WHERE dt_vencimento_registro IS NULL")[0] == 0
    assert one("SELECT max(dt_carga_etl) FROM T")[0] == datetime(2026, 10, 5)
    assert one(
        "SELECT count(*) FILTER (NOT st_produto_ativo), count(*) FILTER (st_produto_ativo) FROM T"
    ) >= (1, 1)
    marcas = [m for (m,) in con.sql(f"SELECT marcas FROM '{parquet}'").fetchall() if m]
    assert any("L'ANA MED" in m for m in marcas)
    assert not any("&apos;" in m or "&quot;" in m for m in marcas)
    assert one("SELECT count(*) FROM T WHERE no_produto LIKE '%&#%'")[0] == 0
    cnpjs = [c for (c,) in con.sql(f"SELECT nu_cnpj_empresa FROM '{parquet}'").fetchall()]
    assert "00000000091308" in cnpjs and {len(c) for c in cnpjs} == {14}  # zeros kept
    # Windows-1252 0x96 and 0x92, decoded to – and ’ rather than C1 control characters
    assert one("SELECT count(*) FROM T WHERE no_razao_social_empresa LIKE '%COSTA – FI'")[0]
    assert one("SELECT count(*) FROM T WHERE no_produto LIKE '%DE 6’-SIALIL%'")[0]
    # the \xa0\r\n tail is stripped, so the category groups with its clean spelling
    assert one("SELECT count(*) FROM T WHERE ds_categoria_produto LIKE '%infância'")[0] == 1


@needs_duckdb
def test_convert_reports_nulls_added(tmp_path):
    stats, _ = converted(ALIMENTOS, tmp_path)
    assert stats.rows == len(sample_records(ALIMENTOS)) - 1 and stats.rejected == ()
    assert stats.nulls_added["st_produto_ativo"] == 1  # the one "X"
    assert {k for k, v in stats.nulls_added.items() if v} == {"st_produto_ativo"}
    assert stats.loaded_at == "2026-10-05T00:00:00"
    assert not list(tmp_path.glob("*.utf8.csv"))  # the intermediate file is cleaned up


@needs_duckdb
def test_convert_guard_fails_on_mass_nulls(tmp_path):
    from anvisa.dados.convert import convert

    rows = sample_records(ALIMENTOS)
    iso = [[re.sub(r"(\d\d)/(\d\d)/(\d{4})", r"\3-\2-\1", v) for v in r] for r in rows]
    csv = write_dialect(tmp_path / "iso.csv", iso)
    with pytest.raises(DadosError, match="DT_REGULARIZACAO.*did ANVISA change the format"):
        convert(ALIMENTOS, csv, tmp_path / "x.parquet")
    assert not (tmp_path / "x.parquet").exists()
    assert not list(tmp_path.glob("*.utf8.csv"))


@needs_duckdb
def test_convert_guard_fails_on_malformed_records(tmp_path):
    from anvisa.dados.convert import convert

    rows = sample_records(ALIMENTOS)
    rows[5] = rows[5][:-2]
    csv = write_dialect(tmp_path / "short.csv", rows)
    with pytest.raises(DadosError, match="wrong number of fields .first: record 5"):
        convert(ALIMENTOS, csv, tmp_path / "x.parquet")
    assert not list(tmp_path.glob("*.utf8.csv"))  # cleaned up on failure too


@needs_duckdb
def test_convert_saneantes_values(tmp_path):
    """Month-first dates, 0/1 booleans, the load time from DT_ATUALIZACAO, and the inner quote
    followed by CRLF CRLF kept inside the product name."""
    import duckdb

    from anvisa.dados.convert import convert

    csv = tmp_path / SANEANTES.file
    csv.write_bytes(sample(SANEANTES).read_bytes())
    stats = convert(SANEANTES, csv, tmp_path / "s.parquet")
    assert stats.rows == 25 and stats.rejected == ()
    # the two records of the pair come from a later file: the load time is the newest
    assert stats.loaded_at == "2026-10-06T00:00:00"
    assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()
    first = con.execute(
        "SELECT dt_vencimento_produto, is_registrado, st_produto_ativo, nu_cnpj_empresa "
        f"FROM '{tmp_path}/s.parquet' WHERE nu_processo = '25351650678202111'"
    ).fetchone()
    assert first == (datetime(2031, 6, 21, 10, 1, 26), False, True, "08409808000139")
    kinds = con.execute(
        f"SELECT is_registrado, count(*) FROM '{tmp_path}/s.parquet' GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert [k for k, _ in kinds] == [False, True]
    names = con.execute(
        f"SELECT no_produto FROM '{tmp_path}/s.parquet' WHERE no_produto LIKE '%AS MENINAS%'"
        " ORDER BY 1"
    ).fetchall()
    assert ('AROMATIZADOR DE AMBIENTE "AS MENINAS"\r\n\r\nSPRAY',) in names  # whole, CRLFs kept
    assert ('AROMATIZADOR DE AMBIENTE "AS MENINAS"',) in names  # its sibling, a separate record
    aquaflex = con.execute(
        f"SELECT no_produto FROM '{tmp_path}/s.parquet' WHERE no_produto LIKE 'AQUAFLEX%'"
    ).fetchone()[0]
    assert aquaflex.endswith("‏")  # &#8207; decoded, and that is what ANVISA wrote


@needs_duckdb
def test_convert_petitions(tmp_path):
    """Month-first dates in the finalized file and day-first in the open one, the `#` header,
    the open file's `Todos` rows, a processo's history contiguous, and the join to alimentos."""
    import duckdb

    from anvisa.dados.convert import convert

    tables = {}
    for ds in (PETICOES_ALIMENTO, PETICOES_ALIMENTO_ANDAMENTO, ALIMENTOS):
        csv = tmp_path / ds.file
        csv.write_bytes(sample(ds).read_bytes())
        stats = convert(ds, csv, tmp_path / f"{ds.name}.parquet")
        tables[ds.name] = f"'{tmp_path / ds.name}.parquet'"
        if ds is not ALIMENTOS:
            assert stats.rejected == () and stats.loaded_at is None
            assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()

    def rows(sql):
        return con.execute(sql.format(**tables)).fetchall()

    done = "{peticoes_alimento}"
    first = rows(
        "SELECT num_processo_peticao, s_n_peticao_primaria, cod_assunto_peticao, "
        "data_situacao_atual_peticao, data_ini_ocorrencia_grp_etapa, "
        "ordem_ocorre_grupo_etapa_asc, ordem_ocorre_grupo_etapa_desc "
        f"FROM {done} WHERE num_expediente_peticao = '0875314158' ORDER BY 6"
    )
    assert first[0] == (
        "25351734343201491",
        False,
        457,
        datetime(2016, 3, 21),
        datetime(2015, 9, 29),
        1,
        3,
    )
    assert [r[5:] for r in first] == [(1, 3), (2, 2), (3, 1)]
    closed = rows(
        f"SELECT DISTINCT data_primeira_finalizacao FROM {done} "
        "WHERE num_expediente_peticao = '524004112'"
    )
    assert closed == [(datetime(2012, 1, 25, 16, 37, 27),)]  # `01/25/2012 16:37:27`
    assert rows(f"SELECT count(*) FROM {done} WHERE desc_sub_fila_lista_analise LIKE '% '") == [
        (0,)
    ]  # the trailing spaces are stripped
    assunto = rows(
        f"SELECT DISTINCT desc_assunto_peticao FROM {done} WHERE cod_assunto_peticao = 4092"
    )
    assert assunto[0][0].endswith("primeira infância&#8203,")  # kept as ANVISA wrote it
    processos = rows(f"SELECT num_processo_peticao FROM {done}")
    assert processos == sorted(processos)  # a processo's petitions are contiguous

    open_ = "{peticoes_alimento_andamento}"
    queue = rows(
        "SELECT data_ini_ocorrencia_grp_etapa, data_fim_ocorrencia_grp_etapa "
        f"FROM {open_} WHERE num_expediente_peticao = '0823936261' "
        "AND desc_grupo_etapa_ciclo_analise = 'Fila de Análise'"
    )
    # `12/08/2026 18:03:00` is 12 August, day first; month first would read 8 December
    assert queue == [(datetime(2026, 8, 12, 18, 3), datetime(2026, 9, 23, 14, 7, 8))]
    todos = rows(
        "SELECT desc_grupo_etapa_ciclo_analise, count(*), count(data_fim_ocorrencia_grp_etapa), "
        f"count(DISTINCT num_expediente_peticao) FROM {open_} "
        "WHERE ordem_ocorre_grupo_etapa_asc = 0 GROUP BY 1"
    )
    assert todos == [("Todos", 5, 0, 5)]  # one per petição, never closed
    assert "data_primeira_finalizacao" not in [c for c, *_ in rows(f"DESCRIBE {open_}")]

    joined = rows(
        f"SELECT DISTINCT p.num_processo_peticao FROM {done} p "
        "JOIN {alimentos} a ON a.nu_processo = p.num_processo_peticao"
    )
    assert ("25351053143202611",) in joined  # a petition's company comes from alimentos


@needs_duckdb
def test_convert_produtos_irregulares(tmp_path):
    """The company acted against is NU_CNPJ_EMPRESA_INVESTIGADA, not NU_CNPJ (who filed the
    dossiê, often ANVISA); entities and inner quotes in product names; area-first sort."""
    import duckdb

    from anvisa.dados.convert import convert

    tables = {}
    for ds in (PRODUTOS_IRREGULARES, ALIMENTOS):
        csv = tmp_path / ds.file
        csv.write_bytes(sample(ds).read_bytes())
        stats = convert(ds, csv, tmp_path / f"{ds.name}.parquet")
        tables[ds.name] = f"'{tmp_path / ds.name}.parquet'"
        if ds is PRODUTOS_IRREGULARES:
            assert stats.rows == 54 and stats.rejected == ()
            assert stats.loaded_at == "2026-10-05T00:00:02"
            assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()

    def rows(sql):
        return con.execute(sql.format(**tables)).fetchall()

    t = "{produtos_irregulares}"
    areas = [a for (a,) in rows(f"SELECT co_tipo_produto FROM {t}")]
    assert areas == sorted(areas)  # one area's rows are contiguous
    # ANVISA (03112386000111) filed this one; the company is in the investigada column
    filed = rows(
        "SELECT DISTINCT nu_cnpj, nu_cnpj_empresa_investigada, ds_tipo_produto "
        f"FROM {t} WHERE co_seq_dossie_investig_med = 45947"
    )
    assert filed[0][0] == "03112386000111" and filed[0][1] != filed[0][0]
    assert filed[0][2] == "Alimento"
    # what the investigada column holds besides CNPJs, kept as written
    investigated = {v for (v,) in rows(f"SELECT nu_cnpj_empresa_investigada FROM {t}")}
    assert {"DESCONHECIDO", "desconhecido", "03855103143", None} <= investigated
    joined = rows(
        f"SELECT DISTINCT i.co_seq_dossie_investig_med FROM {t} i "
        "JOIN {alimentos} a ON a.nu_cnpj_empresa = i.nu_cnpj_empresa_investigada"
    )
    assert (53236,) in joined  # a company with regularized products and a measure
    # per dossiê: the latest publication, and how many measures it took
    measures = rows(
        "SELECT total_medida_cautelar, dt_publicacao_medida, count(DISTINCT dt_publicacao), "
        f"max(dt_publicacao) FROM {t} WHERE co_seq_dossie_investig_med = 33727 GROUP BY ALL"
    )
    assert measures == [(2, datetime(2022, 8, 25), 2, datetime(2022, 8, 25))]
    names = [n for (n,) in rows(f"SELECT DISTINCT produto FROM {t} WHERE produto IS NOT NULL")]
    assert 'GENGIBRE VÉDICO EM PÓ" - MARCA SOULY' in names  # unescaped quote, kept
    assert any("BABY & KIDS" in n for n in names)  # &amp; decoded
    assert not any(re.search(r"&(#\d+|[a-z]+);", n) for n in names)
    company = rows(
        f"SELECT no_empresa_investigada FROM {t} WHERE co_seq_dossie_investig_med = 44903"
    )
    assert company[0][0] == "EBAZAR.COM.BR. LTDA\u200b (MERCADO LIVRE)"  # &#8203; decoded
    assert rows(f"SELECT count(registro) FROM {t} WHERE co_tipo_produto = 6") == [(0,)]
    # `HARVONI` and `HARVONI `: two records upstream, one value once stripped
    harvoni = rows(f"SELECT count(*), count(DISTINCT produto) FROM {t} WHERE produto = 'HARVONI'")
    assert harvoni == [(2, 1)]


@needs_duckdb
def test_convert_cosmeticos(tmp_path):
    """Day-first dates (unlike saneantes), S/N and 0/1 booleans, `&AMP;` in capitals decoded and
    `&KISS;` kept, control characters kept, and a processo listed twice (notified, registered)."""
    import duckdb

    from anvisa.dados.convert import convert

    csv = tmp_path / COSMETICOS.file
    csv.write_bytes(sample(COSMETICOS).read_bytes())
    stats = convert(COSMETICOS, csv, tmp_path / "c.parquet")
    assert stats.rows == 34 and stats.rejected == ()
    assert stats.loaded_at == "2026-10-06T00:00:00"
    assert all(v == 0 for v in stats.nulls_added.values())
    assert not (tmp_path / ".duckdb_tmp").exists()
    con = duckdb.connect()

    def rows(sql):
        return con.execute(sql.format(t=f"'{tmp_path}/c.parquet'")).fetchall()

    def expiry(processo):
        return rows(f"SELECT dt_vencimento FROM {{t}} WHERE nu_processo = '{processo}'")[0][0]

    assert expiry("250000479529921") == datetime(2005, 9, 29)  # 29/09/2005
    assert expiry("250000480459991") == datetime(2005, 9, 6)  # 06/09/2005: day first
    assert expiry("25351000105202657") == datetime(2036, 1, 1, 9, 3, 40)
    assert expiry("250000000259470") == datetime(1999, 12, 7)
    keys = rows("SELECT nu_cnpj_empresa, nu_processo, st_registrado FROM {t}")
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    # listed twice upstream: notified (or exempt) first, registered second, same registro
    pair = rows(
        "SELECT nu_cnpj_empresa, st_registrado, ds_tipo_peticao, nu_registro, dt_vencimento, "
        "st_situacao_produto FROM {t} WHERE nu_processo = '250000018199711'"
    )
    assert pair == [
        ("00317372000146", False, "ISENTO DE REGISTRO", "222620013", None, False),
        ("00317372000146", True, None, "222620013", datetime(2027, 6, 27), False),
    ]
    names = {n for (n,) in rows("SELECT no_produto FROM {t}")}
    assert "CREME PARA AS MAOS M&N COSMETICA" in names  # M&AMP;N
    assert "REESTRUTURADOR MIX OIL CUPUAÇU●TUTANO● KARITÊ TOGÊ" in names  # &#9679;
    assert "\tMASK CONCEPTION CUPUAÇU" in names  # &#61656; then a tab, kept
    assert any(n.endswith("PEACH&KISS;BLUE WATER") for n in names)  # no such entity
    assert any(n.endswith("UVA M&N COSMETICOS") for n in names)  # bare `&`, untouched
    assert 'NUTRITIVO OCEANIDE "O"' in names  # written `"NUTRITIVO OCEANIDE "O""`
    assert "PROTETOR PRE RELAXAMENTO LEFIF AFRO LINE" in names  # leading space stripped
    assert "SABONETE DE AROEIRA \x81‰ \x81Š - BIO SEIVA" in names  # undefined bytes passed
    companies = {n for (n,) in rows("SELECT no_razao_social_empresa FROM {t}")}
    assert "\x17\x10\x16\x18VITORIA FACE COMERCIO DE COSMETICOS LTDA" in companies
    assert "HENLAU QUIMICA LTDA" in companies  # trailing space stripped
    assert rows("SELECT nu_registro FROM {t} WHERE nu_processo = '25351014236200325'") == [
        ("null",)  # as ANVISA wrote it
    ]
    kinds = rows("SELECT ds_tipo_peticao, bool_and(NOT st_registrado) FROM {t} GROUP BY 1")
    assert dict(kinds) == {
        "Notificado": True,
        "ISENTO DE REGISTRO": True,
        "REGISTRO": True,
        "DESCARTAVEL": True,
        None: False,
    }
    # month first, as saneantes, leaves every expiry with a day above 12 unparsed
    wrong = dataclasses.replace(COSMETICOS, timestamp_formats=SANEANTES.timestamp_formats)
    with pytest.raises(DadosError, match="did ANVISA change the format"):
        convert(wrong, csv, tmp_path / "x.parquet")


@needs_duckdb
def test_petition_date_order_is_not_interchangeable(tmp_path):
    """Each file gets exactly one date order: with the other one, too many values stop parsing
    and the build fails, instead of 12/08 quietly turning into 8 December."""
    from anvisa.dados.convert import convert

    pairs = (
        (PETICOES_ALIMENTO, PETICOES_ALIMENTO_ANDAMENTO),
        (PETICOES_ALIMENTO_ANDAMENTO, PETICOES_ALIMENTO),
    )
    for ds, other in pairs:
        wrong = dataclasses.replace(ds, timestamp_formats=other.timestamp_formats)
        csv = tmp_path / ds.file
        csv.write_bytes(sample(ds).read_bytes())
        with pytest.raises(DadosError, match="did ANVISA change the format"):
            convert(wrong, csv, tmp_path / "x.parquet")


@needs_duckdb
def test_parquet_is_sorted_in_small_row_groups(tmp_path):
    """DuckDB rounds ROW_GROUP_SIZE up to a multiple of 2048, so this needs > 4096 rows."""
    import duckdb

    from anvisa.dados.convert import convert

    header, *rows = sample_records(ALIMENTOS)
    cnpj = header.index("NU_CNPJ_EMPRESA")
    many = []
    for i in range(5000):
        row = list(rows[i % len(rows)])
        row[cnpj] = f"{(i * 7919) % 5000:014d}"  # every CNPJ once, in scrambled order
        many.append(row)
    csv = write_dialect(tmp_path / "many.csv", [header, *many])
    stats = convert(ALIMENTOS, csv, tmp_path / "many.parquet", row_group_size=2048)
    assert stats.rows == 5000
    groups = duckdb.sql(
        "SELECT row_group_id, stats_min, stats_max "
        f"FROM parquet_metadata('{tmp_path}/many.parquet')"
        " WHERE path_in_schema = 'nu_cnpj_empresa' ORDER BY row_group_id"
    ).fetchall()
    assert len(groups) == 3
    assert all(lo is not None and hi is not None for _, lo, hi in groups)
    assert all(groups[i][2] <= groups[i + 1][1] for i in range(len(groups) - 1))


@needs_duckdb
def test_convert_medicamentos(tmp_path):
    """The registry as published: repeated rows kept, dd/mm/yyyy dates, month-precision
    vencimento in both spellings, entities decoded, the CNPJ still inside the company column,
    no load time."""
    import duckdb

    from anvisa.dados.convert import convert

    csv = tmp_path / MEDICAMENTOS.file
    csv.write_bytes(sample(MEDICAMENTOS).read_bytes())
    stats = convert(MEDICAMENTOS, csv, tmp_path / "m.parquet")
    assert stats.rows == 30 and stats.rejected == () and stats.loaded_at is None
    assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()
    q = lambda sql: con.execute(re.sub(r"\bTBL\b", f"'{tmp_path}/m.parquet'", sql)).fetchall()  # noqa: E731
    # the three LYSUGI rows are identical, as in the file: the converted table does not dedupe
    assert q(
        "SELECT count(*), count(DISTINCT numero_processo) FROM TBL WHERE nome_produto = 'LYSUGI'"
    ) == [(3, 1)]
    lysugi = q(
        "SELECT numero_registro_produto, data_finalizacao_processo, data_vencimento_registro, "
        "empresa_detentora_registro, situacao_registro "
        "FROM TBL WHERE nome_produto = 'LYSUGI' LIMIT 1"
    )[0]
    assert lysugi == (
        "151430058",
        date(2020, 9, 8),
        date(2030, 9, 1),
        "03978166000175 - DR. REDDYS FARMACÊUTICA DO BRASIL LTDA",
        "Ativo",
    )
    # mm/yyyy, the rarer spelling of the month-precision vencimento
    assert q("SELECT data_vencimento_registro FROM TBL WHERE nome_produto = 'ANDOLBA'") == [
        (date(2020, 12, 1),)
    ]
    # numeric entities decoded in the name; the source's own spelling of categories kept
    assert q("SELECT nome_produto FROM TBL WHERE nome_produto LIKE 'SOLU%'") == [
        ("SOLUçãO FISIOLóGICA DE CLORETO DE SóDIO 0,9%",)
    ]
    assert q(
        "SELECT count(*) FROM TBL WHERE categoria_regulatoria IN ('FITOTER?PICO', 'Gases Medici')"
    ) == [(2,)]
    # registro empty on the notified low-risk products, 9 digits otherwise (one 8-digit oddity)
    assert q("SELECT count(*) FROM TBL WHERE numero_registro_produto IS NULL") == [(8,)]
    assert q("SELECT numero_registro_produto FROM TBL WHERE nome_produto = 'DALACIN T'") == [
        ("12389000",)
    ]
    assert q("SELECT count(*) FROM TBL WHERE data_finalizacao_processo IS NULL") == [
        (1,)
    ]  # the gas


@needs_duckdb
def test_convert_produtos_saude(tmp_path):
    """The unquoted dialect: inner quotes kept, every line one record; VIGENTE kept as text
    beside the dates; the load time read month first while the publication date is day
    first; one row per fabricante."""
    import duckdb

    from anvisa.dados.convert import convert

    csv = tmp_path / PRODUTOS_SAUDE.file
    csv.write_bytes(sample(PRODUTOS_SAUDE).read_bytes())
    stats = convert(PRODUTOS_SAUDE, csv, tmp_path / "p.parquet")
    assert stats.rows == 29 and stats.rejected == ()
    assert stats.loaded_at == "2026-10-12T19:00:00"  # 10/12/2026 19:00:00 is 12 October
    assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()
    q = lambda sql: con.execute(re.sub(r"\bTBL\b", f"'{tmp_path}/p.parquet'", sql)).fetchall()  # noqa: E731
    assert q("SELECT nome_tecnico FROM TBL WHERE numero_registro_cadastro = '10361050002'") == [
        ('Sistema "Holter"',)
    ]
    assert q("SELECT nome_comercial FROM TBL WHERE numero_registro_cadastro = '80020550060'") == [
        ('PONTEIRA DE SILICONE "CAP" DE PROTEÇÃO PARA ENDOSCOPIA DIGESTIVA',)
    ]
    assert q(
        "SELECT validade_registro_cadastro, count(*) FROM TBL GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
    ) == [("VIGENTE", 19)]
    assert q(
        "SELECT dt_pub_registro_cadastro, validade_registro_cadastro, dt_atualizacao_dado "
        "FROM TBL WHERE numero_registro_cadastro = '10163770014' "
        "AND nome_fabricante = 'SURETEX LIMITED'"
    ) == [(date(2000, 5, 26), "26/05/2035", datetime(2026, 10, 12, 19, 0))]
    # one row per fabricante: the registro repeats, everything else equal
    assert q(
        "SELECT count(*), count(DISTINCT nome_comercial), "
        "list(nome_pais_fabric ORDER BY nome_pais_fabric) "
        "FROM TBL WHERE numero_registro_cadastro = '10150470123'"
    ) == [(3, 1, ["ALEMANHA", "ESTADOS UNIDOS DA AMÉRICA", "MALÁSIA"])]
    assert q("SELECT count(*) FROM TBL WHERE nome_fabricante IS NULL") == [(3,)]
    assert q("SELECT count(*) FROM TBL WHERE length(numero_registro_cadastro) = 10") == [(2,)]
    # the source writes trailing spaces on 1,845 nomes técnicos: stripped, like every value
    assert q("SELECT count(*) FROM TBL WHERE nome_tecnico <> trim(nome_tecnico)") == [(0,)]
    assert q(
        "SELECT nome_tecnico FROM TBL WHERE numero_registro_cadastro = '10008530286' LIMIT 1"
    ) == [("CABO/ELETRODO ELETROCIRÚRGICO (Potência maior que 50W)",)]
    assert q("SELECT count(*) FROM TBL WHERE dt_pub_registro_cadastro IS NULL") == [(1,)]


@needs_duckdb
def test_convert_cbpf(tmp_path):
    """Exact copies of a petição kept as rows; dates month first; inner quotes, a cp1252 dash
    and entities whose `;` became `-` kept as written; the plant is a CNPJ in Brazil, a code
    abroad or nothing, never both."""
    import duckdb

    from anvisa.dados.convert import convert

    csv = tmp_path / CBPF.file
    csv.write_bytes(sample(CBPF).read_bytes())
    stats = convert(CBPF, csv, tmp_path / "c.parquet")
    assert stats.rows == 41 and stats.rejected == ()
    assert stats.loaded_at == "2026-10-13T00:00:01"
    assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()
    q = lambda sql: con.execute(re.sub(r"\bTBL\b", f"'{tmp_path}/c.parquet'", sql)).fetchall()  # noqa: E731
    assert q("SELECT count(DISTINCT co_documento_peticao) FROM TBL") == [(37,)]
    assert q(
        "SELECT co_documento_peticao, count(*) FROM TBL GROUP BY 1 HAVING count(*) > 1 ORDER BY 1"
    ) == [(32165176, 4), (34532485, 2)]
    assert q("SELECT count(*) FROM (SELECT DISTINCT * FROM TBL)") == [(37,)]  # copies are exact
    assert q(
        "SELECT typeof(co_documento_peticao), min(co_documento_peticao) FROM TBL GROUP BY 1"
    ) == [("INTEGER", 9720823)]
    assert dict(q("SELECT no_tipo_certificado, count(*) FROM TBL GROUP BY 1")) == {
        "CBPDA": 6,
        "CBPF": 35,
    }
    assert dict(q("SELECT status, count(*) FROM TBL GROUP BY 1")) == {
        "VALIDO": 13,
        "VENCIDO": 15,
        "INDEFERIDO": 6,
        "CANCELADO": 1,
        None: 6,
    }
    assert q("SELECT count(dt_validade) FROM TBL") == [(30,)]
    # month first: 07/20/2026 is 20 July
    assert q(
        "SELECT dt_publicacao, dt_validade FROM TBL WHERE co_documento_peticao = 34129091"
    ) == [(datetime(2026, 7, 20), datetime(2028, 7, 20))]
    assert q("SELECT dt_publicacao FROM TBL WHERE co_documento_peticao = 23492834") == [
        (datetime(2022, 10, 31),)
    ]
    (assunto,) = q("SELECT assunto FROM TBL WHERE co_documento_peticao = 31521226")[0]
    assert assunto.endswith(
        '(equipamento, materiais e produtos para diagnóstico de uso "in vitro")'
    )
    assert q("SELECT assunto LIKE '%&#8203-' FROM TBL WHERE co_documento_peticao = 30482180") == [
        (True,)
    ]
    assert q(
        "SELECT razaosocial_solicitante FROM TBL WHERE co_documento_peticao IN (9720823, 12189153) "
        "ORDER BY co_documento_peticao"
    ) == [
        ("LABVIX - COM&Eacute-RCIO E REPRESENTA&Ccedil-&Atilde-O LTDA.",),
        ("EPEX INDUSTRIA DE PRODUTOS PARA SAUDE LTDA – ME",),  # 0x96
    ]
    # 11543733's razão ends in whitespace in the source
    assert q(
        "SELECT count(*) FROM TBL WHERE razaosocial_solicitante <> trim(razaosocial_solicitante)"
    ) == [(0,)]
    assert dict(q("SELECT length(nu_processo), count(*) FROM TBL GROUP BY 1")) == {
        17: 37,
        16: 1,
        15: 1,
        13: 1,
        7: 1,
    }
    plant = (
        "SELECT count(*) FILTER (WHERE regexp_full_match(nu_cnpj_empresa_inspecionada, "
        "'[0-9]{14}') AND no_pais_inspecionada = 'BRASIL'), "
        "count(*) FILTER (WHERE regexp_full_match(codigounico_inspecionada, '[ABC][0-9]{6}')), "
        "count(*) FILTER (WHERE codigounico_inspecionada IS NULL "
        "AND nu_cnpj_empresa_inspecionada IS NULL AND razaosocial_inspecionada IS NULL), "
        "count(*) FILTER (WHERE codigounico_inspecionada IS NOT NULL "
        "AND nu_cnpj_empresa_inspecionada IS NOT NULL) FROM TBL"
    )
    assert q(plant) == [(14, 13, 14, 0)]
    assert dict(
        q(
            "SELECT codigounico_inspecionada[1], count(*) FROM TBL "
            "WHERE codigounico_inspecionada IS NOT NULL GROUP BY 1"
        )
    ) == {"A": 4, "B": 3, "C": 6}
    assert dict(q("SELECT situacao, count(*) FROM TBL GROUP BY 1")) == {
        "Publicado deferimento": 30,
        "Publicado indeferimento": 6,
        "Revalidação automática": 3,
        "Renovação automática": 1,
        "Certificado emitido": 1,
    }


@needs_duckdb
def test_convert_cbpf_medicamentos(tmp_path):
    """One row per certified line: a petição's rows differ in the line columns only (two can
    still repeat a LINHACERTIFICACAO); S/N is a boolean; the four date columns are month first;
    the `¿` ANVISA left in the motivos stays; no petição is also in cbpf."""
    import duckdb

    from anvisa.dados.convert import convert

    csv = tmp_path / CBPF_MEDICAMENTOS.file
    csv.write_bytes(sample(CBPF_MEDICAMENTOS).read_bytes())
    stats = convert(CBPF_MEDICAMENTOS, csv, tmp_path / "m.parquet")
    assert stats.rows == 32 and stats.rejected == ()
    assert stats.loaded_at == "2025-09-08T15:28:52"  # ANVISA's last load of this file
    assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()
    q = lambda sql: con.execute(re.sub(r"\bTBL\b", f"'{tmp_path}/m.parquet'", sql)).fetchall()  # noqa: E731
    assert q("SELECT count(DISTINCT co_documento_peticao) FROM TBL") == [(22,)]
    assert q(
        "SELECT co_documento_peticao, count(*) FROM TBL GROUP BY 1 HAVING count(*) > 1 ORDER BY 1"
    ) == [(12392173, 2), (13232768, 2), (26977292, 6), (30679096, 3), (32461648, 2)]
    assert q("SELECT count(*) FROM (SELECT DISTINCT * FROM TBL)") == [(31,)]  # 12392173's copy
    assert q(
        "SELECT count(DISTINCT linhacertificacao) FROM TBL WHERE co_documento_peticao = 13232768"
    ) == [(1,)]
    assert dict(q("SELECT status, count(*) FROM TBL GROUP BY 1")) == {
        "VALIDO": 10,
        "VENCIDO": 12,
        "INDEFERIDO": 6,
        "CANCELADO": 2,
        None: 2,
    }
    assert dict(q("SELECT st_liberacao_parametrica, count(*) FROM TBL GROUP BY 1")) == {
        False: 27,
        True: 1,
        None: 4,
    }
    assert q(
        "SELECT linhacertificacao LIKE '%(LIBERAÇÃO PARAMÉTRICA)%' FROM TBL "
        "WHERE co_documento_peticao = 32461648 AND st_liberacao_parametrica"
    ) == [(True,)]
    assert q(
        "SELECT DISTINCT dt_publicacao, dt_validade, dt_resolucao FROM TBL "
        "WHERE co_documento_peticao = 30679096"
    ) == [(datetime(2024, 6, 10), datetime(2026, 6, 10), datetime(2024, 6, 3))]
    (motivo,) = q("SELECT ds_motivo_cancelamento FROM TBL WHERE co_documento_peticao = 27818097")[0]
    assert motivo == "A empresa solicitante peticionou o Cancelamento da certifica¿¿o."
    (motivo,) = q("SELECT ds_motivo_cancelamento FROM TBL WHERE co_documento_peticao = 32945995")[0]
    assert 'forma farmac¿utica "emuls¿es"' in motivo
    assert q("SELECT count(*) FROM TBL WHERE dt_validade IS NULL") == [(8,)]  # indef. + cancel.
    assert q("SELECT formafarmaceutica FROM TBL WHERE co_documento_peticao = 12973270") == [
        ("Pó com\xa0preparação asséptica",)
    ]
    assert q(
        "SELECT nu_dou, nu_processo FROM TBL WHERE co_documento_peticao IN (14081514, 25042268) "
        "ORDER BY co_documento_peticao"
    ) == [("-", "25351045681201746"), ("11", "2535101369820219")]
    # razaosocial is the inspected plant; a plant abroad may come without code
    assert q(
        "SELECT razaosocial, pais, codigounico_inspecionada FROM TBL "
        "WHERE co_documento_peticao = 12458871"
    ) == [("LEMERY, S.A. DE C.V.", "MÉXICO", None)]
    assert q(
        "SELECT count(*) FROM TBL WHERE nu_cnpj_empresa_inspecionada IS NOT NULL "
        "AND (pais <> 'BRASIL' OR co_uf_inspecionada IS NULL)"
    ) == [(0,)]
    ids = {r[0] for r in q("SELECT co_documento_peticao FROM TBL")}
    ids_cbpf = {int(r[0]) for r in sample_records(CBPF)[1:]}
    assert not ids & ids_cbpf
    # petições of companies in medicamentos_head.csv
    medicamentos = {r[8][:14] for r in sample_records(MEDICAMENTOS)[1:]}
    solicitantes = {r[0] for r in q("SELECT DISTINCT nu_cnpj_solicitante FROM TBL")}
    assert solicitantes & medicamentos == {
        "44734671000151",
        "60318797000100",
        "33247743000110",
        "92265552000905",
    }


# --- cmed: the price list (two UTF-8 files merged by GGREM) -------------------------------

PUBLICADO = "09/10/2026 12:43:03"  # the files' Last-Modified, Fri 09 Oct 2026 15:43:03 GMT


def cmed_rows(kind: str) -> dict[str, dict]:
    return {r["ggrem"]: r for r in cmed.read_source(DADOS / CMED_SAMPLES[kind], kind)}


def test_cmed_reads_prices_flags_and_placeholders():
    """BOM stripped, `;` split, `27,44` -> 2744, `Sim`/`Não` -> S/N, `-` -> None, CNPJ digits
    only, registro_produto derived, the `*` moved from the prices to a flag."""
    pmc = cmed_rows("pmc")
    assert len(pmc) == 22 and list(pmc) != sorted(pmc)  # file order, not sorted yet
    first = pmc["538912020009303"]  # BAYCUTEN N, two actives
    assert first["substancia"] == "21-ACETATO DE DEXAMETASONA;CLOTRIMAZOL"
    assert first["cnpj"] == "18459628000115" and first["laboratorio"] == "BAYER S.A."
    assert first["registro"] == "1705600230032" and first["registro_produto"] == "170560023"
    assert first["ean_1"] == "7891106000956" and first["ean_2"] is None
    assert first["pf_sem_impostos"] == 2744 and first["pf_0"] == 3073 and first["pmc_0"] == 4122
    assert first["pf_17_5_alc"] == 3326 and first["pmc_23_alc"] == 4926
    assert first["restricao_hospitalar"] == "N" and first["cap"] == "N"
    assert first["comercializacao"] == "S" and first["comercializacao_periodo"] == "2025"
    assert first["tarja"] == "- (*)" and first["analise_recursal"] is None
    assert first["pmc_asterisco"] == "N" and "pmvg_asterisco" not in first
    assert "pmvg_0" not in first  # the PMC file has no PMVG block
    hospital = pmc["520925100017204"]  # NUTRIFLEX LIPID: restricted, no PMC
    assert hospital["restricao_hospitalar"] == "S" and hospital["pf_0"] == 183913
    assert all(hospital[c] is None for c in cmed.price_columns("pmc"))
    assert hospital["analise_recursal"] == "(AR)"
    marked = pmc["507525080028406"]  # ACETATO DE ABIRATERONA: ICMS 0% Sim, prices starred
    assert marked["icms_0"] == "S" and marked["pmc_asterisco"] == "S"
    assert marked["pf_12"] == 933716 and marked["pf_0"] == 821670
    assert pmc["529226090029607"]["registro"] is None  # EVRYSDI, the `-` registro
    assert pmc["529226090029607"]["registro_produto"] is None
    assert pmc["526520120096607"]["pf_0"] == 875988412  # ZOLGENSMA, the biggest price
    assert pmc["525425110054703"]["tipo_produto"] is None  # the `-` tipo
    assert pmc["520729802163114"]["tarja"] == "Tarja Sem Tarja"
    assert pmc["538921030033407"]["regime_preco"] == "Liberado"
    pmvg = cmed_rows("pmvg")
    assert len(pmvg) == 22 and "pmc_0" not in pmvg["538912020009303"]
    assert pmvg["538912020009303"]["pmvg_0"] == 2465  # below the PF even without CAP
    assert pmvg["505113100020505"]["pmvg_asterisco"] == "S"  # ORENCIA, CAP Sim
    assert pmvg["505113100020505"]["cap"] == "S" and pmvg["505113100020505"]["pmvg_0"] < 674391


def test_cmed_header_drift_and_bad_values_fail(tmp_path):
    assert cmed.column_for("PF 17,5 %  ALC", "pmc") == "pf_17_5_alc"
    assert cmed.column_for("PMVG 0 %", "pmvg") == "pmvg_0"
    assert cmed.column_for("DESTINAÇÃO COMERCIAL 9", "pmvg") is None
    assert cmed.column_for("COMERCIALIZAÇÃO 2026", "pmc") == "comercializacao"
    with pytest.raises(SchemaDriftError, match="unknown column 'PREÇO 18 %'"):
        cmed.column_for("PREÇO 18 %", "pmc")
    # a new ICMS rate matches the price pattern but has no column: it must not vanish in merge
    with pytest.raises(SchemaDriftError, match=r"price column 'PMC 21,5 %' \(pmc_21_5\)"):
        cmed.column_for("PMC 21,5 %", "pmc")
    data = (DADOS / CMED_SAMPLES["pmc"]).read_bytes()
    drifted = tmp_path / "a.csv"
    drifted.write_bytes(data.replace("CÓDIGO GGREM".encode(), b"GGREM", 1))
    with pytest.raises(SchemaDriftError, match="unknown column 'GGREM'"):
        list(cmed.read_source(drifted, "pmc"))
    assert cmed.centavos("1.234,56*") == (123456, True)
    assert cmed.centavos("    -     ") == (None, False)
    with pytest.raises(DadosError, match="not a price"):
        cmed.centavos("R$ 1")
    assert cmed.flag("Não") == "N" and cmed.flag("sim") == "S" and cmed.flag("") is None
    with pytest.raises(DadosError, match="not Sim/Não"):
        cmed.flag("Talvez")


def test_cmed_merge_is_one_row_per_ggrem():
    rows, stats = cmed.read_merged(
        DADOS / CMED_SAMPLES["pmc"], DADOS / CMED_SAMPLES["pmvg"], publicado_em=PUBLICADO
    )
    assert stats == cmed.MergeStats(rows=23, only_pmc=1, only_pmvg=1)
    ggrems = [r["ggrem"] for r in rows]
    assert ggrems == sorted(ggrems) and len(dict.fromkeys(ggrems)) == 23
    by = {r["ggrem"]: r for r in rows}
    assert all(list(r) == list(cmed.COLUMNS) for r in rows)
    assert all(r["publicado_em"] == PUBLICADO for r in rows)
    both = by["538912020009303"]
    assert both["pf_0"] == 3073 and both["pmc_0"] == 4122 and both["pmvg_0"] == 2465
    assert both["pmc_asterisco"] == "N" and both["pmvg_asterisco"] == "N"
    only_pmc = by["522718110082501"]
    assert only_pmc["pf_0"] and only_pmc["pmc_0"] is not None
    assert all(only_pmc[c] is None for c in cmed.price_columns("pmvg"))
    assert only_pmc["pmvg_asterisco"] is None
    only_pmvg = by["524820010008207"]
    assert only_pmvg["pf_0"] and only_pmvg["pmvg_0"] is not None and only_pmvg["substancia"]
    assert all(only_pmvg[c] is None for c in cmed.price_columns("pmc"))
    assert only_pmvg["pmc_asterisco"] is None and only_pmvg["pmvg_asterisco"] == "N"
    with pytest.raises(DadosError, match="listed twice"):
        cmed.merge([both, both], [], publicado_em=None)


@needs_duckdb
def test_convert_cmed(tmp_path):
    import duckdb

    from anvisa.dados.convert import convert

    rows, _ = cmed.read_merged(
        DADOS / CMED_SAMPLES["pmc"], DADOS / CMED_SAMPLES["pmvg"], publicado_em=PUBLICADO
    )
    csv_path = tmp_path / CMED.local_file
    assert cmed.write_csv(rows, csv_path) == 23
    assert cmed.read_csv_header(csv_path) == list(CMED.columns)
    check_header(CMED, cmed.read_csv_header(csv_path))
    parquet = tmp_path / "cmed.parquet"
    stats = convert(CMED, csv_path, parquet)
    assert stats.rows == 23 and stats.rejected == ()
    assert stats.loaded_at == "2026-10-09T12:43:03"
    assert all(v == 0 for v in stats.nulls_added.values())
    assert csv_path.exists()  # the pre-normalized file is the caller's, not deleted
    con = duckdb.connect()
    described = con.execute(f"DESCRIBE SELECT * FROM '{parquet}'").fetchall()
    cols = {name: kind for name, kind, *_ in described}
    assert cols["pf_0"] == "INTEGER" and cols["cap"] == "BOOLEAN"
    assert cols["publicado_em"] == "TIMESTAMP" and cols["ggrem"] == "VARCHAR"
    assert list(cols) == list(CMED.columns)
    rows_db = con.execute(
        f"SELECT ggrem, pf_0, pmc_0, pmvg_0, cap, pmc_asterisco, registro_produto FROM '{parquet}' "
        "ORDER BY ggrem"
    ).fetchall()
    assert [r[0] for r in rows_db] == sorted(r[0] for r in rows_db)
    bayer = next(r for r in rows_db if r[0] == "538912020009303")
    assert bayer[1:] == (3073, 4122, 2465, False, False, "170560023")
    orencia = next(r for r in rows_db if r[0] == "505113100020505")
    assert orencia[4] is True and orencia[3] < orencia[1]
    assert con.execute(f"SELECT count(*) FROM '{parquet}' WHERE pmc_0 IS NULL").fetchone()[0] >= 5


@needs_duckdb
def test_build_cmed_conditional_on_both_files(fake_dados, tmp_path):
    """The two files are one table: 304 on both skips it; a change in either refetches both."""
    with fake_dados.client() as http:
        first = build(tmp_path / "first", http=http, now=NOW, commit="c1", datasets=(CMED,))
    assert list(first["tables"]) == ["cmed"]
    entry = first["tables"]["cmed"]
    assert entry["rows"] == 23 and entry["group"] == "medicamentos"
    source = entry["source"]
    assert source["stamp"] == "20261009" and source["loaded_at"] == "2026-10-09T12:43:03"
    assert [f["name"] for f in source["files"]] == list(cmed.FILES.values())
    assert source["etag"] == source["files"][0]["etag"] == '"105a8a6-65d6a342ad836"'
    assert source["files"][1]["etag"] == '"10fc04a-65d6a358b7d85"'
    assert source["bytes"] == sum(f["bytes"] for f in source["files"])
    assert entry["columns"][0] == {
        "name": "ggrem",
        "type": "VARCHAR",
        "source": "CÓDIGO GGREM",
        "description": CMED.descriptions["ggrem"],
    }
    assert next(c for c in entry["columns"] if c["name"] == "pmvg_18")["source"] == (
        "PMVG 18 % (TA_PRECO_MEDICAMENTO_GOV.csv)"
    )
    docs = (tmp_path / "first" / "dicionario.html").read_text(encoding="utf-8")
    assert "pmvg_17_5_alc" in docs and "CÓDIGO GGREM" in docs

    fake_dados.manifest, fake_dados.site = first, tmp_path / "first"
    fake_dados.requests.clear()
    with fake_dados.client() as http:
        again = build(
            tmp_path / "second",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="c1",
            datasets=(CMED,),
        )
    assert again == Unchanged(first["build_id"])
    sent = [r.headers.get("If-None-Match") for r in fake_dados.data_requests()]
    assert sent == [f["etag"] for f in source["files"]]

    pmvg_url = catalog_url(cmed.FILES["pmvg"])
    headers, body = fake_dados.files[pmvg_url]
    fake_dados.files[pmvg_url] = ({**headers, "ETag": '"new"'}, body)
    fake_dados.requests.clear()
    with fake_dados.client() as http:
        third = build(
            tmp_path / "third",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="c1",
            datasets=(CMED,),
        )
    assert third["tables"]["cmed"]["source"]["files"][1]["etag"] == '"new"'
    urls = [str(r.url) for r in fake_dados.data_requests()]
    # PMC answered 304, PMVG changed, then PMC was fetched again so the pair is one cut
    assert urls == [catalog_url(cmed.FILES["pmc"]), pmvg_url, catalog_url(cmed.FILES["pmc"])]


# --- dcb: the DCB list (an xlsx in the Biblioteca Digital) ---------------------------------


DCB_PUBLICADO = "09/10/2026 12:31:12"  # the xlsx's Last-Modified, Fri 09 Oct 2026 15:31:12 GMT
DCB_HEADER = ["Nº DCB ", "DENOMINAÇÃO COMUM BRASILEIRA", "Nº CAS", "CLASSIFICAÇÃO", "HISTÓRICO"]


def tiny_xlsx(path: Path, rows: list[list]) -> Path:
    """A minimal workbook with one sheet: text as inline strings, ints as number cells, None
    as no cell. Only the parts dcb.py reads."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xml_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for i, value in enumerate(row):
            ref = f"{chr(65 + i)}{r}"
            if isinstance(value, int):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif value is not None:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{html.escape(value)}</t></is></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
            '<sheet name="Planilha1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
        )
    return path


def test_dcb_finds_the_current_version():
    page = (DADOS / "dcb_colecao.html").read_text(encoding="utf-8")
    assert dcb.current_item(page) == (DCB_TITLE, DCB_ITEM_URL)
    # no item says VIGENTE: the first listed, the newest deposit
    title, url = dcb.current_item(page.replace("-&#x20;VIGENTE", "-&#x20;não&#x20;vigente"))
    assert url == DCB_ITEM_URL and title.endswith("2026 - não vigente")
    # the collection's other item (the 2013 manual) is never a version
    manual = '<a href="/jspui/handle/anvisa/12415">Manual&#x20;das&#x20;DCB&#x20;-&#x20;VIGENTE</a>'
    with pytest.raises(DadosError, match="no 'Lista consolidada das DCB' item"):
        dcb.current_item(manual)
    item = (DADOS / "dcb_item.html").read_text(encoding="utf-8")
    assert dcb.spreadsheet_url(item, DCB_ITEM_URL) == DCB_XLSX_URL
    assert dcb.file_name(DCB_XLSX_URL) == "3-Lista DCB consolidada out 2026.xlsx"
    with pytest.raises(DadosError, match="no xlsx file"):
        dcb.spreadsheet_url(item.replace(".xlsx", ".pdf"), DCB_ITEM_URL)


def test_dcb_reads_the_odd_cases():
    """45 sheet rows: the title, the header, 40 entries and 3 rows merged under the entry above.
    Every case below is a real row (fixtures/dados/headers_dcb.txt lists them)."""
    entries = dcb.read_entries(DCB_SAMPLE, publicado_em=DCB_PUBLICADO)
    by = {e["numero_dcb"]: e for e in entries}
    assert len(entries) == len(by) == 40 and list(by) == sorted(by)
    assert all(list(e) == list(dcb.COLUMNS) for e in entries)
    assert {e["publicado_em"] for e in entries} == {DCB_PUBLICADO}
    # salts are entries of their own; numbers have gaps (no 5)
    assert by[1]["denominacao"] == "abacavir" and by[2]["denominacao"] == "sulfato de abacavir"
    assert 5 not in by and by[6]["denominacao"] == "abaperidona"
    # CAS numbers Excel stored as dates (style 14) come back as the text that was typed
    assert by[205]["cas"] == "7758-11-4" and by[3244]["cas"] == "1972-08-3"
    # whitespace collapsed, no-break spaces included; look-alike hyphens made `-`
    assert (by[9663]["denominacao"], by[9663]["cas"]) == ("dronedarona", "141626-36-0")
    assert by[10842]["denominacao"] == "Vanilla planifolia Jacks. ex Andrews"
    assert by[11322]["denominacao"] == "cloridrato de flurazepam"
    assert by[9771]["denominacao"] == "arcitumomabe (99m Tc)"
    assert by[10092]["denominacao"] == "Acidum carbolicum"
    assert by[11503]["cas"] == "1365970-03-1" and by[13041]["cas"] == "2272904-53-5"
    assert by[11117]["cas"] == "878672-00-5"  # U+2011 in the source
    assert by[6963]["cas"] == "9001-75-6."  # malformed: as written
    # a reference instead of a CAS, in each spelling the list uses
    refs = {n: (by[n]["cas"], by[n]["cas_referencia"]) for n in (10092, 11452, 11318, 12794)}
    assert refs == {10092: (None, 8), 11452: (None, 6), 11318: (None, 8), 12794: (None, 8)}
    assert by[13025]["cas_referencia"] == 11 and by[10877]["cas_referencia"] == 11
    assert sum(1 for e in entries if e["cas"] is None) == 9
    assert all((e["cas"] is None) != (e["cas_referencia"] is None) for e in entries)
    # rich-text cells: the runs joined (superscript and italic runs)
    assert by[1985]["denominacao"] == "cianocobalamina (57 Co)"
    assert by[9892]["denominacao"] == "Aesculus hippocastanum L."
    # an entry merged over two rows: the second row's HISTÓRICO appended
    assert by[342]["historico"] == (
        "Consolidada pela Instrução Normativa - IN nº 342, de 20 de dezembro de 2024 | "
        'Adequação da nomenclatura de "salicilato de bismuto monobásico" para "subsalicilato '
        'de bismuto" pela Instrução Normativa - IN n° 462, de 23 de julho de 2026'
    )
    assert by[4486]["historico"].endswith("IN nº 439, de 16 de abril de 2026")
    assert sum(1 for e in entries if dcb.HISTORICO_SEP in e["historico"]) == 3
    assert by[13017]["classificacao"] == "Outro" and "IN nº 473" in by[13055]["historico"]
    assert {e["classificacao"] for e in entries} == {
        "IFA",
        "INF",
        "BIO",
        "HOM",
        "PM",
        "RAD",
        "Outro",
    }


def test_dcb_drift_and_bad_values_fail(tmp_path):
    title = ["LISTA DCB - CONSOLIDADA"]
    ok = tiny_xlsx(
        tmp_path / "ok.xlsx",
        [
            title,
            DCB_HEADER,
            [1, "abacavir", "136470-78-5", "IFA", "Consolidada"],
            [None, None, None, None, "Alteração"],
            [],
            [3, " b ", "[Ref. 2]", "INF", "Incluída"],
        ],
    )
    entries = dcb.read_entries(ok, publicado_em=None)
    assert [(e["numero_dcb"], e["denominacao"], e["historico"]) for e in entries] == [
        (1, "abacavir", "Consolidada | Alteração"),
        (3, "b", "Incluída"),
    ]
    renamed = tiny_xlsx(
        tmp_path / "renamed.xlsx", [title, [*DCB_HEADER[:2], "CAS", *DCB_HEADER[3:]]]
    )
    with pytest.raises(SchemaDriftError, match="header is"):
        dcb.read_entries(renamed, publicado_em=None)
    wider = tiny_xlsx(tmp_path / "wider.xlsx", [DCB_HEADER, [1, "a", "1-1-1", "IFA", "x", "?"]])
    with pytest.raises(SchemaDriftError, match="past column 5"):
        dcb.read_entries(wider, publicado_em=None)
    twice = tiny_xlsx(tmp_path / "twice.xlsx", [DCB_HEADER, [1, "a", "", "", ""], [1, "b"]])
    with pytest.raises(DadosError, match="DCB 1 on rows 2 and 3"):
        dcb.read_entries(twice, publicado_em=None)
    orphan = tiny_xlsx(tmp_path / "orphan.xlsx", [DCB_HEADER, [None, None, "1-1-1", None, "x"]])
    with pytest.raises(DadosError, match="no DCB number or name"):
        dcb.read_entries(orphan, publicado_em=None)
    with pytest.raises(DadosError, match="no header row"):
        dcb.read_entries(tiny_xlsx(tmp_path / "empty.xlsx", [title]), publicado_em=None)
    assert dcb.numero("12") == 12 and dcb.numero("12.0") == 12 and dcb.numero(" ") is None
    with pytest.raises(DadosError, match="not a whole DCB number"):
        dcb.numero("1.5")
    with pytest.raises(DadosError, match="not a DCB number"):
        dcb.numero("doze")
    assert dcb.cas("[Ref. 8.]") == (None, 8) and dcb.cas("Ref. 11") == (None, 11)
    assert dcb.cas(None) == (None, None) and dcb.cas(date(7758, 11, 4)) == ("7758-11-4", None)
    assert dcb.excel_date("2139900") == date(7758, 11, 4)
    assert dcb.excel_date("0", date1904=True) == date(1904, 1, 1)


@needs_duckdb
def test_convert_dcb(tmp_path):
    import duckdb

    from anvisa.dados.convert import convert

    entries = dcb.read_entries(DCB_SAMPLE, publicado_em=DCB_PUBLICADO)
    csv_path = tmp_path / DCB.local_file
    assert dcb.write_csv(entries, csv_path) == 40
    check_header(DCB, cmed.read_csv_header(csv_path))
    parquet = tmp_path / "dcb.parquet"
    stats = convert(DCB, csv_path, parquet)
    assert stats.rows == 40 and stats.rejected == ()
    assert stats.loaded_at == "2026-10-09T12:31:12"
    assert all(v == 0 for v in stats.nulls_added.values())
    con = duckdb.connect()
    cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM '{parquet}'").fetchall()}
    assert list(cols) == list(dcb.COLUMNS)
    assert cols["numero_dcb"] == cols["cas_referencia"] == "INTEGER"
    assert cols["publicado_em"] == "TIMESTAMP" and cols["cas"] == "VARCHAR"
    numbers = [r[0] for r in con.execute(f"SELECT numero_dcb FROM '{parquet}'").fetchall()]
    assert numbers == sorted(numbers) and numbers[0] == 1 and numbers[-1] == 13055
    assert con.execute(
        f"SELECT denominacao, cas, cas_referencia FROM '{parquet}' WHERE numero_dcb = 10092"
    ).fetchone() == ("Acidum carbolicum", None, 8)


@needs_duckdb
def test_build_dcb_conditional_on_the_xlsx(fake_dados, tmp_path):
    """Three requests: collection, item, xlsx. Unchanged xlsx (304 to If-Modified-Since: it
    has no ETag) skips the build; a new version is a new URL, downloaded unconditionally."""
    with fake_dados.client() as http:
        first = build(tmp_path / "first", http=http, now=NOW, commit="c1", datasets=(DCB,))
    assert list(first["tables"]) == ["dcb"]
    entry = first["tables"]["dcb"]
    assert entry["rows"] == 40 and entry["group"] == "dcb" and entry["sort"] == ["numero_dcb"]
    source = entry["source"]
    assert source["name"] == DCB_TITLE and source["url"] == DCB_XLSX_URL
    assert source["etag"] is None and source["last_modified"] == "Fri, 09 Oct 2026 15:31:12 GMT"
    assert source["bytes"] == DCB_SAMPLE.stat().st_size
    assert source["loaded_at"] == "2026-10-09T12:31:12"
    assert entry["columns"][1] == {
        "name": "denominacao",
        "type": "VARCHAR",
        "source": "DENOMINAÇÃO COMUM BRASILEIRA",
        "description": DCB.descriptions["denominacao"],
    }
    assert [str(r.url) for r in fake_dados.requests] == [DCB.url, DCB_ITEM_URL, DCB_XLSX_URL]
    assert all(r.headers["User-Agent"].startswith("anvisa-python/") for r in fake_dados.requests)

    fake_dados.manifest, fake_dados.site = first, tmp_path / "first"
    fake_dados.requests.clear()
    with fake_dados.client() as http:
        again = build(
            tmp_path / "second",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="c1",
            datasets=(DCB,),
        )
    assert again == Unchanged(first["build_id"])
    (xlsx,) = fake_dados.data_requests()
    assert xlsx.headers["If-Modified-Since"] == source["last_modified"]

    # a new version: the item page links another file (same Last-Modified, new URL)
    new_url = DCB_XLSX_URL.replace("/22551/1/", "/22551/2/")
    fake_dados.files[new_url] = fake_dados.files[DCB_XLSX_URL]
    fake_dados.pages[DCB_ITEM_URL] = fake_dados.pages[DCB_ITEM_URL].replace(
        b"/22551/1/", b"/22551/2/"
    )
    fake_dados.requests.clear()
    with fake_dados.client() as http:
        third = build(
            tmp_path / "third",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="c1",
            datasets=(DCB,),
        )
    assert third["tables"]["dcb"]["source"]["url"] == new_url
    (xlsx,) = fake_dados.data_requests()
    assert str(xlsx.url) == new_url and "If-Modified-Since" not in xlsx.headers


# --- busca: the search files --------------------------------------------------


def test_words_matches_the_cases_shared_with_the_site():
    """fixtures/dados/tokens.json is the contract with anvisa-dash: the two tokenizers must
    split every case the same way, or searches miss products without any error."""
    cases = json.loads((DADOS / "tokens.json").read_text(encoding="utf-8"))
    assert cases["versao"] == busca.VERSION and len(cases["casos"]) == 40
    assert busca.words(None) == []
    for case in cases["casos"]:
        assert busca.words(case["entrada"]) == case["palavras"], case["entrada"]


@needs_duckdb
def test_chave_matches_the_cases_shared_with_the_site():
    """fixtures/dados/chaves.json is the second contract with anvisa-dash: the manufacturer key
    the data computes in SQL (derivar.chave_sql) must equal the site's, or a device page links
    to a manufacturer that does not exist. Python's NFKD is the reference both sides check."""
    import string

    import duckdb

    cases = json.loads((DADOS / "chaves.json").read_text(encoding="utf-8"))
    assert cases["versao"] == 1 and len(cases["casos"]) == 21
    keep = string.ascii_lowercase + string.digits
    con = duckdb.connect()
    sql = f"SELECT {derivar.chave_sql('CAST(? AS VARCHAR)')}"
    for case in cases["casos"]:
        entrada, chave = case["entrada"], case["chave"]
        assert "".join(c for c in busca.normalize(entrada) if c in keep) == chave, entrada
        assert con.execute(sql, [entrada]).fetchone() == (chave,), entrada


def test_pack_fills_files_to_the_target_and_isolates_big_keys():
    counts = [("a", 1000), ("b", 2000), ("c", 1), ("d", 5000), ("e", 10)]
    groups = busca.pack(counts, 3000)
    assert [(g.first, g.rows, g.keys) for g in groups] == [
        ("a", 3000, ["a", "b"]),
        ("c", 1, ["c"]),
        ("d", 5000, ["d"]),  # above the target: a file of its own
        ("e", 10, ["e"]),
    ]


@needs_duckdb
def test_busca_layout_on_the_cosmeticos_fixture(tmp_path):
    """Every file the index lists exists with that size and nothing else is there; the words
    are in byte order; every source row is found by each of its words, by its CNPJ, by its
    processo and by its numeric registro; and the whole thing is deterministic."""
    import duckdb

    _, parquet = converted(COSMETICOS, tmp_path)
    info = busca.write(COSMETICOS, parquet, tmp_path / "one", target=20)
    folder = tmp_path / "one"
    index = json.loads((folder / "indice.json").read_text(encoding="utf-8"))
    assert info == {
        "versao": 1,
        "indice": "indice.json",
        "bytes": (folder / "indice.json").stat().st_size,
        "sha256": hashlib.sha256((folder / "indice.json").read_bytes()).hexdigest(),
        "arquivos": sum(1 for p in folder.rglob("*") if p.is_file()),
        "bytes_total": sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()),
    }
    listed = dict(busca.listed(index))
    assert {p.relative_to(folder).as_posix() for p in folder.rglob("*.parquet")} == set(listed)
    assert all((folder / name).stat().st_size == size for name, size in listed.items())
    assert listed["palavras.parquet"] == index["palavras_parquet"]
    # an index from before palavras.parquet lists one file less, and nothing else changes
    assert (
        len(busca.listed({k: v for k, v in index.items() if k != "palavras_parquet"}))
        == len(listed) - 1
    )
    firsts = [e[0] for e in index["palavras"]]
    assert firsts == sorted(firsts) and len(index["palavras"]) > 1  # target=20 forces a split
    assert all(len(e[0]) == 14 for e in index["empresas"])
    assert set(index["numeros"]) == {k for k in index["numeros"] if len(k) == 3 and k.isdigit()}

    con = duckdb.connect()
    rows = con.execute(
        "SELECT nu_processo, st_registrado, no_produto, nu_cnpj_empresa, nu_registro "
        f"FROM '{parquet}'"
    ).fetchall()
    assert len(rows) == 34

    def read(name, where, value):
        return {
            tuple(r)
            for r in con.execute(
                f"SELECT nu_processo, st_registrado FROM '{folder / name}' WHERE {where} = ?",
                [value],
            ).fetchall()
        }

    def covering(entries, key):
        return [e for e in entries if e[0] <= key][-1][2]

    for processo, registered, name, cnpj, registro in rows:
        key = (processo, registered)
        for word in busca.words(name):
            assert key in read(covering(index["palavras"], word), "palavra", word), (name, word)
        assert key in read(covering(index["empresas"], cnpj), "nu_cnpj_empresa", cnpj)
        assert key in read(f"numeros/{processo[-3:]}.parquet", "num", processo)
        if registro and registro.isdigit():
            assert key in read(f"numeros/{registro[-3:]}.parquet", "num", registro)
    # a word file's rows are (word, source row) pairs with the row columns, in word order
    described = con.execute(f"DESCRIBE SELECT * FROM '{folder}/palavras/0000.parquet'").fetchall()
    assert [c for c, *_ in described] == ["palavra", *busca.ROW_COLUMNS]
    # palavras.parquet: every distinct word once, with how many (word, row) pairs it has
    vocab = con.execute(
        f"SELECT palavra, linhas FROM '{folder}/palavras.parquet' ORDER BY 1"
    ).fetchall()
    expected = Counter(w for *_, name, _, _ in rows for w in busca.words(name))
    assert vocab == sorted(expected.items())
    companies = con.execute(f"SELECT * FROM '{folder}/empresas.parquet'").fetchall()
    assert len(companies) == len({cnpj for *_, cnpj, _ in rows})
    assert "\x17\x10\x16\x18VITORIA FACE COMERCIO DE COSMETICOS LTDA" in {n for _, n in companies}

    busca.write(COSMETICOS, parquet, tmp_path / "two", target=20)
    assert folder_files(folder / "indice.json") == folder_files(tmp_path / "two" / "indice.json")


def test_busca_needs_the_row_columns(tmp_path):
    with pytest.raises(DadosError, match="busca needs the columns"):
        busca.write(SANEANTES, tmp_path / "x.parquet", tmp_path / "out")


# --- build --------------------------------------------------------------------


@needs_duckdb
def test_build_end_to_end(fake_dados, tmp_path):
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert manifest["schema_version"] == 1 and manifest["build_id"] == "20261006T210312Z"
    assert manifest["built_at"] == "2026-10-06T21:03:12Z" and manifest["commit"] == "abc123"
    assert manifest["generator"].startswith("anvisa-python/")
    assert list(manifest["tables"]) == PUBLISHED
    main = manifest["tables"]["alimentos"]
    assert main["path"] == "data/20261006T210312Z/alimentos.parquet"
    parquet = out / main["path"]
    assert main["bytes"] == parquet.stat().st_size
    assert main["sha256"] == hashlib.sha256(parquet.read_bytes()).hexdigest()
    assert main["sort"] == ["nu_cnpj_empresa", "nu_processo"]
    assert main["columns"][0] == {
        "name": "no_produto",
        "type": "VARCHAR",
        "source": "NO_PRODUTO",
        "description": ALIMENTOS.descriptions["NO_PRODUTO"],
    }
    assert main["notes"] == list(ALIMENTOS.notes) and main["notes"]
    assert manifest["docs"] == "dicionario.html"
    assert manifest["license"] == {
        "data": "dados da ANVISA; a licença própria de uma tabela vem indicada nela",
        "code": "MIT",
    }
    assert main["source"]["etag"] == '"20230eb-65d1981268f1f"'
    assert main["source"]["loaded_at"] == "2026-10-05T00:00:00"
    assert main["rejected_records"] == 0 and main["timezone"] == "America/Sao_Paulo"
    assert main["row_group_size"] == 8192  # the build default
    detail = manifest["tables"]["alimentos_resultado"]
    assert detail["sort"] == ["co_produto", "co_seq_apresentacao_produto"]
    assert detail["row_group_size"] == 2048  # the catalog override, so a product is one group
    products = duckdb.sql(f"SELECT co_produto FROM '{out / detail['path']}'").fetchall()
    assert products == sorted(products)  # a product's apresentações are contiguous
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "alimentos_resultado.parquet" in page and 'href="manifest.json"' in page
    assert 'href="dicionario.html"' in page
    missing = (out / "404.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex">' in missing
    assert 'href="https://vasfvitor.github.io/anvisa-api/manifest.json"' in missing
    # the dictionary describes the files beside it: every table, its notes, every column
    docs = (out / "dicionario.html").read_text(encoding="utf-8")
    assert all(f'<section id="{ds.name}">' in docs for ds in CATALOG)
    assert html.escape(COSMETICOS.notes[0]) in docs
    assert html.escape(ALIMENTOS.descriptions["DT_VENCIMENTO_REGISTRO"]) in docs
    assert "<code>co_seq_apresentacao_produto</code>" in docs and 'href="index.html"' in docs
    joined = duckdb.sql(
        f"SELECT count(*) FROM '{parquet}' m JOIN "
        f"'{out / manifest['tables']['alimentos_resultado']['path']}' r "
        "USING (co_seq_apresentacao_produto)"
    ).fetchone()[0]
    assert joined >= 10
    petitions = manifest["tables"]["peticoes_alimento"]
    assert petitions["source"]["loaded_at"] is None  # the petition files carry no load time
    assert petitions["source"]["url"] == PETICOES_ALIMENTO.url
    # the one dataset with `busca` carries its search files; the others have no such key
    assert [n for n, t in manifest["tables"].items() if "busca" in t] == ["cosmeticos"]
    search = manifest["tables"]["cosmeticos"]["busca"]
    assert search["indice"] == "data/20261006T210312Z/cosmeticos/indice.json"
    index = out / search["indice"]
    assert index.stat().st_size == search["bytes"]
    assert hashlib.sha256(index.read_bytes()).hexdigest() == search["sha256"]
    assert search["versao"] == 1 and search["arquivos"] == len(folder_files(index))
    assert f"{search['arquivos']:,} files" in page and search["indice"] in page


def published(fake_dados, tmp_path, commit="abc123") -> dict:
    with fake_dados.client() as http:
        manifest = build(tmp_path / "first", http=http, now=NOW, commit=commit)
    fake_dados.manifest = manifest
    fake_dados.site = tmp_path / "first"
    fake_dados.requests.clear()
    return manifest


@needs_duckdb
def test_build_skips_when_published_matches(fake_dados, tmp_path):
    first = published(fake_dados, tmp_path)
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="abc123"
        )
    assert result == Unchanged(first["build_id"])
    assert not (tmp_path / "second").exists()
    sent = [r.headers.get("If-None-Match") for r in fake_dados.data_requests()]
    expected = []
    for t in first["tables"].values():
        if "fontes" in t:
            continue
        expected += [f["etag"] for f in t["source"]["files"]] or [t["source"]["etag"]]
    assert sent == expected
    manifest_request = fake_dados.requests[0]
    assert manifest_request.headers["Cache-Control"] == "no-cache"


@needs_duckdb
def test_a_failed_download_carries_the_published_table(fake_dados, tmp_path):
    """The Biblioteca Digital answering 403 must not stop a day's data: the DCB of the
    published build comes along, byte for byte, its derived table is rebuilt from it, and the
    manifest's `falhas` names the table and the error."""
    first = published(fake_dados, tmp_path)
    headers, body = fake_dados.files[ALIMENTOS.url]
    fake_dados.files[ALIMENTOS.url] = ({**headers, "ETag": '"new"'}, body)
    fake_dados.status[DCB.url] = 403
    out = tmp_path / "second"
    with fake_dados.client() as http:
        result = build(
            out,
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="abc123",
            now=NOW + timedelta(days=1),
        )
    assert result["falhas"] == {"dcb": f"HTTP 403 for {DCB.url}"}
    assert list(result["tables"]) == PUBLISHED
    old, new = first["tables"]["dcb"], result["tables"]["dcb"]
    assert new["path"] == f"data/{result['build_id']}/dcb.parquet"
    assert (out / new["path"]).read_bytes() == (tmp_path / "first" / old["path"]).read_bytes()
    assert new["source"] == old["source"]
    assert result["tables"]["alimentos"]["source"]["etag"] == '"new"'
    assert json.loads((out / "manifest.json").read_text())["falhas"] == result["falhas"]


def copia_dcb(tmp_path: Path) -> Path:
    """A `fontes` directory with the DCB sample, as `fontes/dcb/` holds the real list."""
    fontes = tmp_path / "fontes"
    (fontes / "dcb").mkdir(parents=True)
    shutil.copyfile(DCB_SAMPLE, fontes / "dcb" / dcb.XLSX)
    meta = {
        "title": DCB_TITLE,
        "url": DCB_XLSX_URL,
        "last_modified": "Fri, 09 Oct 2026 15:31:12 GMT",
    }
    (fontes / "dcb" / dcb.META).write_text(json.dumps(meta), encoding="utf-8")
    return fontes


def test_local_dcb_writes_what_the_download_writes(fake_dados, tmp_path):
    """Same spreadsheet, same CSV and the same `source`, whichever way it arrived."""
    baixado, local = tmp_path / "baixado", tmp_path / "local"
    baixado.mkdir()
    local.mkdir()
    with fake_dados.client() as http:
        online = fetch_source(http, DCB, baixado)
        copia = fetch_source(http, DCB, local, fontes=copia_dcb(tmp_path))
    assert copia == online
    assert (local / DCB.local_file).read_bytes() == (baixado / DCB.local_file).read_bytes()


def test_local_dcb_is_unchanged_on_the_same_file(fake_dados, tmp_path):
    fontes = copia_dcb(tmp_path)
    fetched = local_dcb(DCB, tmp_path, fontes)
    assert fetched is not None
    assert local_dcb(DCB, tmp_path, fontes, previous={"sha256": fetched.sha256}) is None
    assert local_dcb(DCB, tmp_path, fontes, previous={"sha256": "other"}) == fetched


@pytest.mark.parametrize("gone", [dcb.XLSX, dcb.META])
def test_local_dcb_names_what_is_missing(tmp_path, gone):
    fontes = copia_dcb(tmp_path)
    (fontes / "dcb" / gone).unlink()
    with pytest.raises(DadosError, match="cannot read the DCB copy"):
        local_dcb(DCB, tmp_path, fontes)


def test_local_dcb_needs_every_field(tmp_path):
    fontes = copia_dcb(tmp_path)
    (fontes / "dcb" / dcb.META).write_text('{"title": "x"}', encoding="utf-8")
    with pytest.raises(DadosError, match="cannot read the DCB copy"):
        local_dcb(DCB, tmp_path, fontes)


def test_the_kept_copy_of_the_dcb_is_coherent():
    """fontes/dcb/ is replaced by hand on each new version: the file must be readable, and
    dcb.json must describe a version of the list with a date the table can use."""
    folder = DADOS.parents[1] / "fontes" / "dcb"
    meta = json.loads((folder / dcb.META).read_text(encoding="utf-8"))
    assert meta["title"].lower().startswith(dcb.TITLE_PREFIX)
    assert meta["url"].startswith("https://bibliotecadigital.anvisa.gov.br/")
    publicado_em = cmed_stamp(meta["last_modified"])[1]
    assert publicado_em is not None
    entries = dcb.read_entries(folder / dcb.XLSX, publicado_em=publicado_em)
    assert len(entries) > 12_000 and all(e["publicado_em"] == publicado_em for e in entries)


@needs_duckdb
def test_build_republishes_the_dcb_only_when_the_copy_changes(fake_dados, tmp_path):
    fontes = copia_dcb(tmp_path)
    with fake_dados.client() as http:
        first = build(tmp_path / "first", http=http, now=NOW, commit="abc123", fontes=fontes)
    assert list(first["tables"]) == PUBLISHED
    fake_dados.manifest, fake_dados.site = first, tmp_path / "first"
    with fake_dados.client() as http:
        again = build(
            tmp_path / "again",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="abc123",
            fontes=fontes,
        )
    assert isinstance(again, Unchanged)

    fake_dados.status[DCB.url] = 403  # nothing may ask for it, even for a new version
    with zipfile.ZipFile(fontes / "dcb" / dcb.XLSX, "a") as book:
        book.writestr("docProps/nova.txt", "nova versão")
    with fake_dados.client() as http:
        second = build(
            tmp_path / "second",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="abc123",
            now=NOW + timedelta(days=1),
            fontes=fontes,
        )
    assert second["tables"]["dcb"]["source"]["sha256"] != first["tables"]["dcb"]["source"]["sha256"]
    assert "falhas" not in second


@needs_duckdb
def test_a_broken_copy_of_a_published_dcb_is_carried_and_named(fake_dados, tmp_path):
    fontes = copia_dcb(tmp_path)
    with fake_dados.client() as http:
        first = build(tmp_path / "first", http=http, now=NOW, commit="abc123", fontes=fontes)
    fake_dados.manifest, fake_dados.site = first, tmp_path / "first"
    headers, body = fake_dados.files[ALIMENTOS.url]
    fake_dados.files[ALIMENTOS.url] = ({**headers, "ETag": '"new"'}, body)
    (fontes / "dcb" / dcb.XLSX).unlink()
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="abc123",
            now=NOW + timedelta(days=1),
            fontes=fontes,
        )
    assert list(result["falhas"]) == ["dcb"]
    assert "cannot read the DCB copy" in result["falhas"]["dcb"]
    assert result["tables"]["dcb"]["source"] == first["tables"]["dcb"]["source"]


@needs_duckdb
def test_a_failed_download_with_nothing_else_new_fails_the_build(fake_dados, tmp_path):
    """With nothing to publish, carrying the table over would hide the failure: the build
    fails, naming it, and writes nothing (the live build stays)."""
    first = published(fake_dados, tmp_path)
    fake_dados.status[DCB.url] = 403
    with fake_dados.client() as http, pytest.raises(DadosError) as exc:
        build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="abc123"
        )
    assert "dcb could not be downloaded" in str(exc.value)
    assert "HTTP 403" in str(exc.value) and first["build_id"] in str(exc.value)
    assert not (tmp_path / "second").exists()


@needs_duckdb
def test_a_failed_download_never_published_fails_the_build(fake_dados, tmp_path):
    """No published table, nothing to carry: the first build still fails on the download."""
    fake_dados.status[DCB.url] = 403
    with fake_dados.client() as http, pytest.raises(DadosError, match="HTTP 403"):
        build(tmp_path / "first", http=http, now=NOW)


@needs_duckdb
def test_build_rebuilds_when_one_file_changed(fake_dados, tmp_path):
    published(fake_dados, tmp_path)
    headers, body = fake_dados.files[ALIMENTOS.url]
    fake_dados.files[ALIMENTOS.url] = ({**headers, "ETag": '"new"'}, body)
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="abc123"
        )
    assert list(result["tables"]) == PUBLISHED
    assert result["tables"]["alimentos"]["source"]["etag"] == '"new"'
    # the unchanged files answered 304 first, then were fetched again for a complete build (the
    # DCB xlsx has no ETag: its condition is If-Modified-Since)
    statuses = [
        r.headers.get("If-None-Match") or r.headers.get("If-Modified-Since")
        for r in fake_dados.data_requests()
    ]
    files = sum(len(cmed.FILES) if ds.kind == "cmed" else 1 for ds in CATALOG)
    unchanged = files - 1
    assert statuses.count(None) == unchanged and len(statuses) == files + unchanged


@needs_duckdb
def test_build_rebuilds_when_commit_changed(fake_dados, tmp_path):
    published(fake_dados, tmp_path, commit="old")
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="new"
        )
    assert isinstance(result, dict) and result["commit"] == "new"
    assert (tmp_path / "second" / "manifest.json").exists()


@needs_duckdb
def test_build_refuses_a_table_that_lost_rows(fake_dados, tmp_path):
    # the published build says alimentos had twice the rows this file has: a short, well-formed
    # file must not replace it without a red run
    published(fake_dados, tmp_path, commit="old")
    antes = fake_dados.manifest["tables"]["alimentos"]["rows"]
    fake_dados.manifest["tables"]["alimentos"]["rows"] = antes * 2
    with fake_dados.client() as http, pytest.raises(DadosError, match="alimentos: .* rows against"):
        build(tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, commit="new")
    with fake_dados.client() as http:  # a source that really shrank goes in with a lower ratio
        result = build(
            tmp_path / "third",
            http=http,
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="new",
            min_rows_ratio=0.4,
        )
    assert result["tables"]["alimentos"]["rows"] == antes


@needs_duckdb
def test_build_proceeds_without_published_manifest(fake_dados, tmp_path):
    with fake_dados.client() as http:
        result = build(tmp_path / "dist", http=http, skip_unchanged=FakeDados.MANIFEST_URL)
    assert isinstance(result, dict) and (tmp_path / "dist" / "manifest.json").exists()
    assert all("If-None-Match" not in r.headers for r in fake_dados.data_requests())


@needs_duckdb
def test_build_says_why_it_ignores_a_broken_published_manifest(fake_dados, tmp_path):
    """A 500 from Pages must not look like 'nothing changed': the full rebuild is logged."""
    published(fake_dados, tmp_path)
    fake_dados.manifest_status = 500
    log = []
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second", http=http, skip_unchanged=FakeDados.MANIFEST_URL, log=log.append
        )
    assert isinstance(result, dict)
    assert any("could not read the published manifest" in m and "HTTP 500" in m for m in log)


@needs_duckdb
@pytest.mark.parametrize("where", ["url", "path"])
def test_partial_build_carries_published_tables(fake_dados, tmp_path, where):
    """`-d alimentos` against a live site must not publish a one-table manifest: the other
    tables come along from the published build, verified, under the new build's directory."""
    first = published(fake_dados, tmp_path)
    headers, body = fake_dados.files[ALIMENTOS.url]
    fake_dados.files[ALIMENTOS.url] = ({**headers, "ETag": '"new"'}, body)
    location = (
        FakeDados.MANIFEST_URL if where == "url" else str(tmp_path / "first" / "manifest.json")
    )
    out = tmp_path / "second"
    with fake_dados.client() as http:
        result = build(
            out,
            http=http,
            datasets=(ALIMENTOS,),
            skip_unchanged=location,
            commit="abc123",
            now=NOW + timedelta(days=1),
        )
    assert list(result["tables"]) == PUBLISHED
    assert result["tables"]["alimentos"]["source"]["etag"] == '"new"'
    assert {str(r.url) for r in fake_dados.data_requests()} == {ALIMENTOS.url}
    for name, old in first["tables"].items():
        if name == "alimentos":
            continue
        new = result["tables"][name]
        assert new["path"] == f"data/{result['build_id']}/{name}.parquet"
        assert (out / new["path"]).read_bytes() == (tmp_path / "first" / old["path"]).read_bytes()
        if "fontes" in old:  # derived: rebuilt from the same inputs, byte for byte the same
            assert new["sha256"] == old["sha256"] and new["source"]["url"] == old["source"]["url"]
            continue
        moved = {"path", "busca"}
        assert {k: v for k, v in new.items() if k not in moved} == {
            k: v for k, v in old.items() if k not in moved
        }
    assert not list(out.rglob("*.part"))
    # the search files came along too, file for file, and only their index path changed
    old, new = first["tables"]["cosmeticos"]["busca"], result["tables"]["cosmeticos"]["busca"]
    assert new["indice"] == f"data/{result['build_id']}/cosmeticos/indice.json"
    assert {k: v for k, v in new.items() if k != "indice"} == {
        k: v for k, v in old.items() if k != "indice"
    }
    assert folder_files(out / new["indice"]) == folder_files(tmp_path / "first" / old["indice"])


@needs_duckdb
def test_carry_over_refreshes_the_documentation(fake_dados, tmp_path):
    """A carried table keeps its published file but takes this code's title, notes and column
    descriptions: documentation must not lag until ANVISA happens to change that file. A
    published column the catalog does not know keeps what it carried."""
    published(fake_dados, tmp_path)
    entry = fake_dados.manifest["tables"]["saneantes"]
    entry["title"] = "old title"
    entry["notes"] = ["old note"]
    entry["columns"][0]["description"] = "old text"
    entry["columns"].append({"name": "gone", "type": "VARCHAR", "description": "kept"})
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second",
            http=http,
            datasets=(ALIMENTOS,),
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="docs-changed",  # all sources answer 304: a new commit forces the rebuild
            now=NOW + timedelta(days=1),
        )
    carried = result["tables"]["saneantes"]
    assert carried["title"] == SANEANTES.title and carried["notes"] == list(SANEANTES.notes)
    assert carried["columns"][0]["description"] == SANEANTES.descriptions["NO_PRODUTO"]
    assert carried["columns"][-1] == {"name": "gone", "type": "VARCHAR", "description": "kept"}
    assert carried["sha256"] == entry["sha256"]  # the data is still the published file


@needs_duckdb
def test_the_dcb_license_is_published_with_what_it_binds(fake_dados, tmp_path):
    """The CC license reaches dcb and substancias_dcb, in the manifest and the dictionary, even
    on a table carried over from a build that did not state it, and no other table."""
    first = published(fake_dados, tmp_path)
    tables = first["tables"]
    assert tables["dcb"]["license"] == tables["substancias_dcb"]["license"] == dcb.LICENSE
    assert [name for name, t in tables.items() if "license" in t] == ["dcb", "substancias_dcb"]
    page = (tmp_path / "first" / "dicionario.html").read_text(encoding="utf-8")
    assert page.count(f'Licença: <a href="{dcb.LICENSE["url"]}">CC BY-NC-SA 4.0</a>.') == 2

    del fake_dados.manifest["tables"]["dcb"]["license"]
    with fake_dados.client() as http:
        result = build(
            tmp_path / "second",
            http=http,
            datasets=(ALIMENTOS,),
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="docs-changed",
            now=NOW + timedelta(days=1),
        )
    assert result["tables"]["dcb"]["license"] == dcb.LICENSE


# --- derived tables (derivar.py) ------------------------------------------------------------


@needs_duckdb
def test_produtos_on_the_fixtures(fake_dados, tmp_path):
    """One row per product: alimentos grouped by co_seq_produto, saneantes one per record,
    one schema, sorted by company. The manifest entry reads like a converted table's."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["produtos"]
    assert entry["path"] == "data/20261006T210312Z/produtos.parquet"
    assert entry["fontes"] == [
        "alimentos",
        "alimentos_resultado",
        "saneantes",
        "medicamentos",
        "produtos_saude",
    ]
    assert entry["sort"] == ["cnpj", "area", "id"]
    assert entry["source"]["name"] == (
        "derivada de alimentos, alimentos_resultado, saneantes, medicamentos, produtos_saude"
    )
    assert entry["source"]["url"].endswith(
        "/blob/abc123/packages/python/src/anvisa/dados/derivar.py"
    )
    # the latest load among the sources: produtos_saude's (2026-10-12; medicamentos has none)
    assert entry["source"]["loaded_at"] == "2026-10-12T19:00:00"
    assert entry["nulls_added"] == {} and entry["rejected_records"] == 0
    assert [c["name"] for c in entry["columns"]] == list(derivar.PRODUTOS.descriptions)
    path = out / entry["path"]
    assert entry["bytes"] == path.stat().st_size and entry["rows"] == 36 + 24 + 28 + 21
    con = duckdb.connect()
    rows = con.execute(f"SELECT area, count(*) FROM '{path}' GROUP BY 1 ORDER BY 1").fetchall()
    # 38 apresentações of 36 products; 25 saneante records, one expediente listed twice;
    # 30 medicamento rows of 28 products (three LYSUGI rows, 8 without registro); 29
    # produtos_saude rows of 21 registros (one per fabricante)
    assert rows == [
        ("alimentos", 36),
        ("medicamentos", 28),
        ("produtos_saude", 21),
        ("saneantes", 24),
    ]
    # of the Registrado/Notificado pair, the registered row stays
    assert con.execute(
        f"SELECT tipo, registro FROM '{path}' WHERE id = '0152242186'"
    ).fetchall() == [("Registrado", "371460002")]
    # the two alimentos products with two apresentações carry both registros, in order
    multi = con.execute(
        f"SELECT id, apresentacoes, registros FROM '{path}' WHERE apresentacoes > 1 ORDER BY id"
    ).fetchall()
    assert [m[1] for m in multi] == [2, 2]
    # registros: one per apresentação on registros, an empty list (never NULL) on notificações
    assert all(len(m[2]) in (0, 2) and m[2] == sorted(m[2]) for m in multi)
    assert con.execute(f"SELECT count(*) FROM '{path}' WHERE registros IS NULL").fetchone() == (0,)
    assert con.execute(
        f"SELECT DISTINCT tipo FROM '{path}' WHERE area IN ('alimentos', 'saneantes') ORDER BY 1"
    ).fetchall() == [("Notificado",), ("Registrado",)]
    assert con.execute(
        f"SELECT count(*) FILTER (WHERE ativo), count(*) FROM '{path}' WHERE area = 'saneantes'"
    ).fetchone() == (18, 24)
    key = con.execute(f"SELECT cnpj, area, id FROM '{path}'").fetchall()
    assert key == sorted(key) and len(set(key)) == len(key)
    types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()}
    assert (
        types["ativo"] == "BOOLEAN" and types["desde"] == "DATE" and types["vencimento"] == "DATE"
    )
    assert types["apresentacoes"] == "INTEGER" and types["registros"] == "VARCHAR[]"
    assert types["gluten"] == "VARCHAR" and types["contem"] == "VARCHAR[]"
    assert types["alergia_varia"] == "BOOLEAN"
    # allergens, from the label (alimentos_resultado): 15 of the 36 fixture products declare
    # (4 of the 19 resultado products there have no alimentos row in the fixture); the other 21
    # and every saneante are NULL throughout, not empty lists
    q = lambda sql: con.execute(sql.replace("FROM P", f"FROM '{path}'")).fetchall()  # noqa: E731
    assert q("SELECT count(*) FROM P WHERE contem IS NOT NULL") == [(15,)]
    assert q(
        "SELECT count(*) FROM P WHERE contem IS NULL AND (gluten IS NOT NULL "
        "OR lactose IS NOT NULL OR pode_conter IS NOT NULL OR nao_contem IS NOT NULL "
        "OR alergia_varia IS NOT NULL)"
    ) == [(0,)]
    assert q(
        "SELECT gluten, lactose, count(*) FROM P WHERE contem IS NOT NULL GROUP BY 1, 2 ORDER BY 2"
    ) == [
        ("Não", "Não", 11),
        ("Não", "Sim", 4),
    ]
    # "Contém" and "Contém derivado de" both land in contem; the rest of the legal list in
    # nao_contem
    (row,) = q(
        "SELECT contem, pode_conter, len(nao_contem), alergia_varia FROM P WHERE id = '3932328'"
    )
    assert row[0] == ["Leites de todas as espécies de animais mamíferos", "Soja"]
    assert row[1] == [] and row[2] > 10 and row[3] is False
    (row,) = q("SELECT contem FROM P WHERE id = '3868153'")  # "Contém derivado de"
    assert row[0] == ["Leites de todas as espécies de animais mamíferos", "Soja"]
    # no item sits in two lists, and the lists are sorted
    assert q(
        "SELECT count(*) FROM P WHERE len(list_intersect(contem, pode_conter)) > 0 "
        "OR len(list_intersect(contem, nao_contem)) > 0 "
        "OR len(list_intersect(pode_conter, nao_contem)) > 0 "
        "OR contem <> list_sort(contem) OR nao_contem <> list_sort(nao_contem)"
    ) == [(0,)]
    # the dictionary lists it like the others, naming its sources
    docs = (out / "dicionario.html").read_text(encoding="utf-8")
    assert '<section id="produtos">' in docs and 'Derivada de <a href="#alimentos">' in docs


@needs_duckdb
def test_allergen_summary_rules(tmp_path):
    """The rules the fixtures cannot show (no product there varies or contains gluten): a
    product whose apresentações disagree says Varia, an item one apresentação contains leaves
    the other lists, an unknown rótulo counts as contains, and a product with rows but no
    declaration stays NULL. Synthetic alimentos and alimentos_resultado files, two products."""
    import duckdb

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE a AS SELECT * FROM (VALUES "
        "(1, 101, '11111111000191', 'A', '1', 'Ativo', 'Notificado', DATE '2020-01-01'), "
        "(1, 102, '11111111000191', 'A', '1', 'Ativo', 'Notificado', DATE '2020-01-01'), "
        "(2, 201, '11111111000191', 'B', '2', 'Ativo', 'Notificado', DATE '2020-01-01')) "
        "t(co_seq_produto, co_seq_apresentacao_produto, nu_cnpj_empresa, no_produto, nu_processo, "
        "situacao_registro, tipo_regularizacao, dt_regularizacao)"
    )
    con.execute(
        "ALTER TABLE a ADD COLUMN marcas VARCHAR; ALTER TABLE a ADD COLUMN no_razao_social_empresa "
        "VARCHAR; ALTER TABLE a ADD COLUMN nu_registro_notificacao_produto VARCHAR; "
        "ALTER TABLE a ADD COLUMN ds_categoria_produto VARCHAR; "
        "ALTER TABLE a ADD COLUMN dt_vencimento_registro DATE; "
        "ALTER TABLE a ADD COLUMN nu_registro VARCHAR"
    )
    con.execute(
        "CREATE TABLE r AS SELECT * FROM (VALUES "
        "(101, 1, 'Contém Glúten - Sim | Contém Lactose - Não', "
        " 'Contém - Trigo | Pode conter - Soja#Ovos | Não contém - Amendoim#Soja |'), "
        "(102, 1, 'Contém Glúten - Não | Contém Lactose - Não', "
        " 'Rótulo novo - Ovos | Não contém - Amendoim#Trigo'), "
        "(201, 2, NULL, '')) "
        "t(co_seq_apresentacao_produto, co_produto, intolerancias, alergenicos)"
    )
    con.execute(
        "CREATE TABLE s (nu_expediente VARCHAR, no_produto VARCHAR, "
        "no_razao_social_empresa VARCHAR, nu_cnpj_empresa VARCHAR, nu_processo VARCHAR, "
        "nu_registro_produto VARCHAR, "
        "st_produto_ativo BOOLEAN, is_registrado BOOLEAN, dt_vencimento_produto TIMESTAMP)"
    )
    # the two other areas of produtos, empty: the rules under test are the alimentos ones
    con.execute(
        "CREATE TABLE m (numero_registro_produto VARCHAR, numero_processo VARCHAR, "
        "nome_produto VARCHAR, empresa_detentora_registro VARCHAR, situacao_registro VARCHAR, "
        "categoria_regulatoria VARCHAR, classe_terapeutica VARCHAR, principio_ativo VARCHAR, "
        "data_finalizacao_processo DATE, data_vencimento_registro DATE)"
    )
    con.execute(
        "CREATE TABLE p (numero_registro_cadastro VARCHAR, numero_processo VARCHAR, "
        "nome_comercial VARCHAR, nome_tecnico VARCHAR, classe_risco VARCHAR, "
        "cnpj_detentor_registro_cadastro VARCHAR, detentor_registro_cadastro VARCHAR, "
        "nome_fabricante VARCHAR, nome_pais_fabric VARCHAR, dt_pub_registro_cadastro DATE, "
        "validade_registro_cadastro VARCHAR, dt_atualizacao_dado TIMESTAMP)"
    )
    paths = {}
    tables = (
        ("alimentos", "a"),
        ("alimentos_resultado", "r"),
        ("saneantes", "s"),
        ("medicamentos", "m"),
        ("produtos_saude", "p"),
    )
    for name, table in tables:
        paths[name] = tmp_path / f"{name}.parquet"
        con.execute(f"COPY {table} TO '{paths[name]}' (FORMAT parquet)")
    rows, _ = derivar.write(
        derivar.PRODUTOS, paths, tmp_path / "produtos.parquet", tmp_path / "spill"
    )
    assert rows == 2
    got = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT id, gluten, lactose, contem, pode_conter, nao_contem, alergia_varia "
            f"FROM '{tmp_path / 'produtos.parquet'}'"
        ).fetchall()
    }
    # gluten: one apresentação Sim, one Não -> Varia; lactose agrees. Trigo is contained by one
    # apresentação, so it leaves nao_contem; Soja is "pode conter" on one, so it leaves nao_contem
    # but stays in pode_conter; Ovos under an unknown rótulo counts as contained.
    assert got["1"] == ("Varia", "Não", ["Ovos", "Trigo"], ["Soja"], ["Amendoim"], True)
    assert got["2"] == (None, None, None, None, None, None)


@needs_duckdb
def test_empresas_on_the_fixtures(fake_dados, tmp_path):
    """One row per 14-digit CNPJ across the three product tables, the measures' targets and
    the food packers; counts are products, and only a real CNPJ makes a company."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["empresas"]
    assert entry["fontes"] == [
        "alimentos",
        "saneantes",
        "cosmeticos",
        "medicamentos",
        "produtos_saude",
        "produtos_irregulares",
        "alimentos_resultado",
    ]
    assert [c["name"] for c in entry["columns"]] == list(derivar.EMPRESAS.descriptions)
    path = out / entry["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM E", f"FROM '{path}'")).fetchall()  # noqa: E731
    cnpjs = {r[0] for r in q("SELECT cnpj FROM E")}
    assert len(cnpjs) == entry["rows"] and all(re.fullmatch(r"\d{14}", c) for c in cnpjs)
    holders = set()
    for name in ("alimentos", "saneantes", "cosmeticos"):
        src = out / manifest["tables"][name]["path"]
        holders |= {r[0] for r in con.execute(f"SELECT nu_cnpj_empresa FROM '{src}'").fetchall()}
    assert holders <= cnpjs and len(holders) == 33 + 20 + 29  # disjoint in the fixtures
    # the saneante listed twice counts once for its company
    assert q("SELECT saneantes, saneantes_ativos FROM E WHERE cnpj = '19145925000159'") == [(1, 1)]
    # the one company present in two sources: holds alimentos and was the target of a measure
    (row,) = q(
        "SELECT alimentos, saneantes, cosmeticos, medidas, medida_ultima FROM E "
        "WHERE cnpj = '02200052000137'"
    )
    assert row[0] > 0 and row[1] == 0 and row[2] == 0 and row[3] == 1 and row[4] is not None
    # targets that are not a 14-digit CNPJ make no company; the fixture has these three kinds
    assert not {"0203396000198", "03855103143", "DESCONHECIDO", "00000000000000"} & cnpjs
    # cosméticos: a processo listed twice (one per st_registrado) counts once
    cos = out / manifest["tables"]["cosmeticos"]["path"]
    pairs = con.execute(
        f"SELECT nu_cnpj_empresa, count(*) FROM '{cos}' GROUP BY nu_processo, 1 HAVING count(*) = 2"
    ).fetchall()
    assert len(pairs) == 2
    for cnpj, _ in pairs:
        distinct = con.execute(
            f"SELECT count(DISTINCT nu_processo) FROM '{cos}' WHERE nu_cnpj_empresa = '{cnpj}'"
        ).fetchone()[0]
        assert q(f"SELECT cosmeticos FROM E WHERE cnpj = '{cnpj}'") == [(distinct,)]
    (sums,) = q(
        "SELECT bool_and(produtos = alimentos + saneantes + cosmeticos + medicamentos "
        "+ produtos_saude AND ativos <= produtos) FROM E"
    )
    assert sums == (True,)
    assert q("SELECT count(*) FROM E WHERE nome IS NULL AND produtos > 0") == [(0,)]
    # packing for other companies (alimentos_fabricacao, vinculo grupo or terceira): a packer
    # with no product and no measure gets a row named as the packer string writes it; the
    # holder counts distinct packers, branches of its own root included; packing for oneself
    # counts nowhere
    pack = "SELECT envasa_para, envasado_por, produtos, nome FROM E WHERE cnpj = "
    assert q(pack + "'32098703000190'") == [(1, 0, 0, "GOMES SUPLEMENTOS ALIMENTARES LTDA")]
    assert q(pack + "'57357240000165'") == [(0, 2, 2, "INTERMEDIO AGENCIA LTDa")]
    assert q(pack + "'60409075000152'") == [(0, 2, 2, "NESTLE BRASIL LTDA")]
    assert q(pack + "'60409075002953'") == [(1, 0, 0, "NESTLE DO BRASIL LTDA")]
    assert q(pack + "'00669755000183'") == [(0, 0, 1, "sports nutrition tec alimentos ltda")]
    assert q("SELECT sum(envasa_para), sum(envasado_por) FROM E") == [(14, 14)]
    assert q(
        "SELECT count(*), count(*) FILTER (WHERE envasa_para > 0 AND nome IS NOT NULL) "
        "FROM E WHERE produtos = 0 AND medidas = 0"
    ) == [(14, 14)]


@needs_duckdb
def test_medidas_on_the_fixtures(fake_dados, tmp_path):
    """One row per dossiê × produto of produtos_irregulares, ações and atividades as sorted
    lists, the target's CNPJ only when it is one and nothing about a person."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["medidas"]
    assert entry["fontes"] == ["produtos_irregulares"]
    assert [c["name"] for c in entry["columns"]] == list(derivar.MEDIDAS.descriptions)
    assert {c["type"] for c in entry["columns"]} == {
        "INTEGER",
        "VARCHAR",
        "BOOLEAN",
        "VARCHAR[]",
        "DATE",
    }
    path = out / entry["path"]
    src = out / manifest["tables"]["produtos_irregulares"]["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM M", f"FROM '{path}'")).fetchall()  # noqa: E731
    # 54 source rows (one with an empty PRODUTO, left out) collapse to 19 dossiê × produto rows
    pairs = con.execute(
        f"SELECT count(DISTINCT (co_seq_dossie_investig_med, trim(produto))) FROM '{src}' "
        "WHERE coalesce(trim(produto), '') <> ''"
    ).fetchone()[0]
    assert entry["rows"] == pairs == 19
    assert q("SELECT count(*) FROM M WHERE produto = '' OR produto <> trim(produto)") == [(0,)]
    assert q("SELECT area_codigo, count(*) FROM M WHERE area = 'Alimento' GROUP BY 1") == [(6, 13)]
    # the company also in alimentos_head.csv: one dossiê, a Proibição, published on one day
    assert q("SELECT dossie, acoes, primeira = ultima FROM M WHERE cnpj = '02200052000137'") == [
        (53236, ["Proibição"], True)
    ]
    # a dossiê with two ações and a voluntary recall filed by the company itself
    (row,) = q("SELECT acoes, atividades, autor_cnpj = cnpj FROM M WHERE dossie = 17441")
    assert row == (
        ["Proibição", "Recolhimento"],
        ["Comercialização", "Distribuição", "Voluntário"],
        True,
    )
    # targets that are a CPF, 13 digits or text: no cnpj; a person gets no name either
    assert q("SELECT count(cnpj), count(*) FILTER (WHERE pessoa) FROM M") == [(10, 1)]
    assert q("SELECT cnpj, empresa FROM M WHERE pessoa") == [(None, None)]
    assert q(
        "SELECT count(*) FROM M WHERE cnpj IS NOT NULL AND NOT regexp_full_match(cnpj, '[0-9]{14}')"
    ) == [(0,)]
    assert q(
        "SELECT count(*) FROM M WHERE registros IS NULL OR acoes IS NULL OR atividades IS NULL"
    ) == [(0,)]
    assert q("SELECT count(*) FROM M WHERE len(registros) > 0") == [(3,)]  # never on alimentos
    assert q("SELECT count(*) FROM M WHERE area = 'Alimento' AND len(registros) > 0") == [(0,)]
    assert q("SELECT count(*) FROM M WHERE primeira > ultima") == [(0,)]
    keys = q("SELECT area_codigo, dossie, produto FROM M")
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


@needs_duckdb
def test_cosmeticos_por_empresa_on_the_fixtures(fake_dados, tmp_path):
    """One row per processo per company, numbered; the unregistered row of a pair is the one
    kept, the tipo labels are the site's."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["cosmeticos_por_empresa"]
    assert entry["fontes"] == ["cosmeticos"] and entry["sort"] == ["cnpj", "ordem"]
    assert [c["name"] for c in entry["columns"]] == list(
        derivar.COSMETICOS_POR_EMPRESA_TABELA.descriptions
    )
    path = out / entry["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM C", f"FROM '{path}'")).fetchall()  # noqa: E731
    # 34 records, 29 companies, two processos listed twice: 32 rows
    assert entry["rows"] == 32
    assert q("SELECT count(DISTINCT cnpj), count(*) = count(DISTINCT (cnpj, id)) FROM C") == [
        (29, True)
    ]
    assert q(
        "SELECT bool_and(ok) FROM (SELECT list_sort(list(ordem)) = range(1, count(*) + 1) AS ok "
        "FROM C GROUP BY cnpj)"
    ) == [(True,)]
    # the pairs: the st_registrado = false row stays, which carries ds_tipo_peticao
    assert q(
        "SELECT id, ordem, tipo, ativo, vencimento FROM C "
        "WHERE id IN ('250000004209949', '250000018199711') ORDER BY id"
    ) == [
        ("250000004209949", 1, "Isento de registro", True, date(2029, 5, 5)),
        ("250000018199711", 1, "Isento de registro", False, None),
    ]
    assert q("SELECT tipo, count(*) FROM C GROUP BY 1 ORDER BY 2 DESC, 1") == [
        ("Notificado", 17),
        ("Isento de registro", 7),
        ("Registrado", 7),
        ("Descartável", 1),
    ]
    types = {c["name"]: c["type"] for c in entry["columns"]}
    assert types["ordem"] == "INTEGER" and types["ativo"] == "BOOLEAN"
    assert types["vencimento"] == "DATE"


@needs_duckdb
def test_cosmeticos_por_empresa_order_and_cap(tmp_path, monkeypatch):
    """What the fixtures cannot show: active products come first, then name, then processo,
    and the list stops at the cap. Synthetic cosmeticos file, one company, cap lowered to 3."""
    import duckdb

    monkeypatch.setattr(derivar, "COSMETICOS_POR_EMPRESA", 3)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE c AS SELECT * FROM (VALUES "
        "('1', 'B', false, 'Notificado'), ('2', 'A', false, 'Notificado'), "
        "('3', 'Z', true, 'REGISTRO'), ('4', 'Y', true, 'DESCARTAVEL'), ('5', 'Y', true, NULL)) "
        "t(nu_processo, no_produto, st_situacao_produto, ds_tipo_peticao)"
    )
    con.execute(
        "ALTER TABLE c ADD COLUMN nu_cnpj_empresa VARCHAR DEFAULT '11111111000191'; "
        "ALTER TABLE c ADD COLUMN no_razao_social_empresa VARCHAR DEFAULT 'E'; "
        "ALTER TABLE c ADD COLUMN nu_registro VARCHAR; "
        "ALTER TABLE c ADD COLUMN st_registrado BOOLEAN DEFAULT false; "
        "ALTER TABLE c ADD COLUMN dt_vencimento TIMESTAMP"
    )
    src = tmp_path / "cosmeticos.parquet"
    con.execute(f"COPY c TO '{src}' (FORMAT parquet)")
    rows, _ = derivar.write(
        derivar.COSMETICOS_POR_EMPRESA_TABELA,
        {"cosmeticos": src},
        tmp_path / "cpe.parquet",
        tmp_path / "spill",
    )
    assert rows == 3
    got = con.execute(
        f"SELECT ordem, id, nome, ativo, tipo FROM '{tmp_path / 'cpe.parquet'}' ORDER BY ordem"
    ).fetchall()
    # active first (Y before Z by name, processo 4 before 5 on the same name), then the cap
    assert got == [
        (1, "4", "Y", True, "Descartável"),
        (2, "5", "Y", True, None),
        (3, "3", "Z", True, "Registrado"),
    ]


@needs_duckdb
def test_alimentos_por_produto_on_the_fixtures(fake_dados, tmp_path):
    """The alimentos rows of produtos, column for column, plus the petição's status, claim
    and three dates; nothing a consumer of produtos would see differently."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["alimentos_por_produto"]
    assert entry["fontes"] == ["alimentos", "alimentos_resultado"]
    assert entry["sort"] == ["cnpj", "id"] and entry["rows"] == 36
    names = [c["name"] for c in entry["columns"]]
    assert names == list(derivar.ALIMENTOS_POR_PRODUTO.descriptions)
    produtos = [c["name"] for c in manifest["tables"]["produtos"]["columns"]]
    assert names[: len(produtos)] == produtos  # the produtos columns first, in their order
    extras = names[len(produtos) :]
    assert extras == ["situacao_assunto", "alegacao", "publicacao", "situacao_em", "inicio_analise"]
    a = out / entry["path"]
    p = out / manifest["tables"]["produtos"]["path"]
    con = duckdb.connect()
    shared = ", ".join(produtos)
    for left, right in ((a, f"{p}' WHERE area = 'alimentos"), (f"{p}' WHERE area = 'alimentos", a)):
        gone = con.execute(
            f"SELECT count(*) FROM (SELECT {shared} FROM '{left}' "
            f"EXCEPT ALL SELECT {shared} FROM '{right}')"
        ).fetchone()
        assert gone == (0,)
    types = {c["name"]: c["type"] for c in entry["columns"]}
    assert types["publicacao"] == "TIMESTAMP" and types["inicio_analise"] == "TIMESTAMP"
    # the status is always there; the dates are not (publicacao is NULL on half the real file)
    assert con.execute(f"SELECT count(*) FROM '{a}' WHERE situacao_assunto IS NULL").fetchone() == (
        0,
    )
    # a product with two apresentações: publication is the latest, analysis start the earliest
    src = out / manifest["tables"]["alimentos"]["path"]
    (row,) = con.execute(
        f"SELECT CAST(co_seq_produto AS VARCHAR), max(dt_publicacao), min(dt_inicio_analise) "
        f"FROM '{src}' GROUP BY 1 HAVING count(*) > 1 ORDER BY 1 LIMIT 1"
    ).fetchall()
    assert con.execute(
        f"SELECT publicacao, inicio_analise FROM '{a}' WHERE id = '{row[0]}'"
    ).fetchone() == (row[1], row[2])


@needs_duckdb
def test_alimentos_fabricacao_on_the_fixtures(fake_dados, tmp_path):
    """One row per holder and manufacturer: Brazilian packers by CNPJ, foreign manufacturers by
    code; the holder is the product's, and apresentações without an alimentos row are dropped."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["alimentos_fabricacao"]
    assert entry["fontes"] == ["alimentos", "alimentos_resultado"]
    assert entry["sort"] == ["cnpj", "fabricante_cnpj", "fabricante_codigo", "fabricante"]
    assert entry["rows"] == 35
    assert [c["name"] for c in entry["columns"]] == list(derivar.ALIMENTOS_FABRICACAO.descriptions)
    path = out / entry["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM F", f"FROM '{path}'")).fetchall()  # noqa: E731
    assert dict(q("SELECT vinculo, count(*) FROM F GROUP BY 1")) == {
        "propria": 11,
        "grupo": 2,
        "terceira": 12,
        "exterior": 10,
    }
    detail = "fabricante_cnpj, fabricante, cidade, pais, vinculo, produtos, produtos_ativos, "
    detail += "apresentacoes FROM F WHERE cnpj = "
    assert q(f"SELECT {detail}'57357240000165'") == [
        ("05297500000150", "GSA ALIMENTOS LTDA", "OURO PRETO", "BRASIL", "terceira", 2, 2, 2),
        ("32098703000190", "GOMES SUPLEMENTOS ALIMENTARES LTDA", "POÇOS DE CALDAS", "BRASIL")
        + ("terceira", 2, 2, 2),
    ]
    nestle = q(
        "SELECT vinculo, fabricante_cnpj, fabricante_codigo, fabricante, pais FROM F "
        "WHERE cnpj = '60409075000152'"
    )
    assert Counter(r[0] for r in nestle) == {"grupo": 2, "exterior": 10}
    assert ("exterior", None, "D000093", "NESTLÉ CHILE S.A – CHILE (CANCURA)", "CHILE") in nestle
    assert all(r[1] is None and r[2] for r in nestle if r[0] == "exterior")
    # a self-packer whose only product is inactive
    assert q("SELECT vinculo, produtos, produtos_ativos FROM F WHERE cnpj = '41486715000183'") == [
        ("propria", 1, 0)
    ]
    # the four apresentações of alimentos_resultado without an alimentos row leave nothing:
    # Danone's 10055201 (two packers, twelve foreign manufacturers), Castrolanda's 2857441
    assert q(
        "SELECT count(*) FROM F WHERE fabricante_cnpj IN ('23643315014293', '76108349001428') "
        "OR fabricante IN ('MILUPA GMBH', 'BLÉDINA')"
    ) == [(0,)]
    holders = {r[0] for r in q("SELECT cnpj FROM F")}
    src = out / manifest["tables"]["alimentos"]["path"]
    assert holders <= {r[0] for r in con.execute(f"SELECT nu_cnpj_empresa FROM '{src}'").fetchall()}
    empresas = out / manifest["tables"]["empresas"]["path"]
    packers = {r[0] for r in q("SELECT fabricante_cnpj FROM F WHERE vinculo <> 'propria'")}
    known = {r[0] for r in con.execute(f"SELECT cnpj FROM '{empresas}'").fetchall()}
    assert packers - {None} <= known
    # within a holder: packers by CNPJ, then foreign manufacturers by code, then uncoded by name
    rows = q("SELECT cnpj, fabricante_cnpj, fabricante_codigo, fabricante FROM F")
    key = lambda r: (r[0], r[1] is None, r[1] or "", r[2] is None, r[2] or "", r[3])  # noqa: E731
    assert rows == sorted(rows, key=key)


@needs_duckdb
def test_medicamentos_por_produto_on_the_fixtures(fake_dados, tmp_path):
    """One row per product: the repeated LYSUGI rows collapse, a registro is the id, products
    without one get a stable hash id, and the produtos rows agree with this table."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["medicamentos_por_produto"]
    assert entry["fontes"] == ["medicamentos", "cmed"] and entry["sort"] == ["cnpj", "id"]
    assert [c["name"] for c in entry["columns"]] == list(
        derivar.MEDICAMENTOS_POR_PRODUTO.descriptions
    )
    path = out / entry["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM M", f"FROM '{path}'")).fetchall()  # noqa: E731
    assert entry["rows"] == 28 and q("SELECT count(DISTINCT id) FROM M") == [(28,)]
    (lysugi,) = q(
        "SELECT registro_produto, processo, cnpj, empresa, ativo, categoria_regulatoria, "
        "classe_terapeutica, principio_ativo, desde, vencimento, processos, linhas "
        "FROM M WHERE id = '151430058'"
    )
    assert lysugi == (
        "151430058",
        "25351686176201544",
        "03978166000175",
        "DR. REDDYS FARMACÊUTICA DO BRASIL LTDA",
        True,
        "Similar",
        "ANTICONVULSIVANTES",
        "pregabalina",
        date(2020, 9, 8),
        date(2030, 9, 1),
        1,
        3,
    )
    # without a registro: a hash id, stable across builds, from company, name and processo
    sem = q("SELECT id, nome, registro_produto, processos FROM M WHERE id LIKE 'sem-registro-%'")
    assert len(sem) == 8 and all(re.fullmatch(r"sem-registro-[0-9a-f]{12}", r[0]) for r in sem)
    assert all(r[2] is None for r in sem)
    assert {r[1] for r in sem} >= {"LABORHEX 2,0%", "KALOPÉ", "GÁS OXIGÊNIO MEDICINAL LUMIAR"}
    assert next(r for r in sem if r[1].startswith("GÁS"))[3] == 1  # the one with a processo
    # entities decoded, the source's spelling kept
    assert q("SELECT count(*) FROM M WHERE nome LIKE 'SOLUçãO%'") == [(1,)]
    assert q("SELECT count(*) FROM M WHERE categoria_regulatoria = 'FITOTER?PICO'") == [(1,)]
    # the CMED sample shares no registro with the medicamentos sample: every list is empty
    assert q("SELECT count(*) FROM M WHERE len(eans) > 0") == [(0,)]
    assert q("SELECT typeof(eans) FROM M LIMIT 1") == [("VARCHAR[]",)]
    # the same rows in produtos, with the shared columns
    prod = out / manifest["tables"]["produtos"]["path"]
    assert con.execute(
        f"SELECT count(*) FROM (SELECT id, nome, cnpj, ativo, vencimento FROM '{path}' "
        f"EXCEPT SELECT id, nome, cnpj, ativo, vencimento FROM '{prod}' "
        "WHERE area = 'medicamentos')"
    ).fetchone() == (0,)
    assert con.execute(
        f"SELECT tipo, categoria FROM '{prod}' WHERE area = 'medicamentos' AND id = '151430058'"
    ).fetchone() == ("Similar", "ANTICONVULSIVANTES")
    key = q("SELECT cnpj, id FROM M")
    assert key == sorted(key)


@needs_duckdb
def test_produtos_saude_por_produto_on_the_fixtures(fake_dados, tmp_path):
    """One row per registro with its fabricantes listed; VIGENTE and dated validades become
    `ativo` against the file's own date, never the build's."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["produtos_saude_por_produto"]
    assert entry["fontes"] == ["produtos_saude"] and entry["rows"] == 21
    assert [c["name"] for c in entry["columns"]] == list(
        derivar.PRODUTOS_SAUDE_POR_PRODUTO.descriptions
    )
    path = out / entry["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM P", f"FROM '{path}'")).fetchall()  # noqa: E731
    (sonda,) = q(
        "SELECT registro, nome, nome_tecnico, classe_risco, tipo, ativo, publicacao, vencimento, "
        "fabricantes, linhas FROM P WHERE id = '10150470123'"
    )
    assert sonda == (
        "10150470123",
        "SONDA DE FOLEY SILICONE RUSCH",
        "Sondas",
        "II",
        "Registrado",
        True,
        date(1999, 4, 16),
        None,  # VIGENTE: no term
        [
            "RUSCH INC (ESTADOS UNIDOS DA AMÉRICA)",
            "TELEFLEX MEDICAL SDN. BHD. (MALÁSIA)",
            "WILLY RUSCH GMBH (ALEMANHA)",
        ],
        3,
    )
    (preserv,) = q("SELECT ativo, vencimento, fabricantes FROM P WHERE id = '10163770014'")
    assert preserv == (True, date(2035, 5, 26), ["SURETEX LIMITED (TAILÂNDIA, REINO DA)"])
    assert q("SELECT tipo, count(*) FROM P GROUP BY 1 ORDER BY 1") == [
        ("Cadastrado", 3),
        ("Registrado", 18),
    ]
    # a row without fabricante adds nothing to the list (B. Braun's electrode has one such row)
    assert q("SELECT fabricantes FROM P WHERE id = '10008530286'") == [
        (["AESCULAP AG (ALEMANHA)"],)
    ]
    assert q("SELECT count(*) FROM P WHERE fabricantes IS NULL") == [(0,)]
    assert q("SELECT count(*) FROM P WHERE NOT ativo") == [(0,)]
    types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()}
    assert types["fabricantes"] == "VARCHAR[]" and types["vencimento"] == "DATE"
    assert types["fabricantes_chaves"] == types["fabricantes_paises"] == "VARCHAR[]"
    # the chave and país of each fabricante, index by index
    chaves = "SELECT fabricantes_chaves, fabricantes_paises FROM P WHERE id = "
    assert q(chaves + "'10150470123'") == [
        (
            ["ruschinc", "teleflexmedicalsdnbhd", "willyruschgmbh"],
            ["ESTADOS UNIDOS DA AMÉRICA", "MALÁSIA", "ALEMANHA"],
        )
    ]
    assert q(chaves + "'10008530286'") == [(["aesculapag"], ["ALEMANHA"])]
    # two spellings of Polar Fix: two fabricantes, one chave twice
    assert q(chaves + "'8003400137'") == [
        (["polarfixindustriaecomerciodeprodutoshospitalaresltda"] * 2, ["BRASIL"] * 2)
    ]
    (eth,) = q(
        "SELECT fabricantes, fabricantes_chaves, fabricantes_paises FROM P WHERE id = '10132590043'"
    )
    assert eth[2] == [
        "ESTADOS UNIDOS DA AMÉRICA",
        "ESTADOS UNIDOS DA AMÉRICA",
        "PORTO RICO (ESTADOS UNIDOS)",
    ]
    assert [f.endswith(f"({p})") for f, p in zip(eth[0], eth[2], strict=True)] == [True] * 3
    assert eth[1][0] == "ethiconendosurgeryincsadecvmexicoparaethiconendosurgeryinc"
    assert q(
        "SELECT count(*) FROM P WHERE len(fabricantes) <> len(fabricantes_chaves) "
        "OR len(fabricantes) <> len(fabricantes_paises) OR fabricantes_chaves IS NULL "
        "OR fabricantes_paises IS NULL"
    ) == [(0,)]
    prod = out / manifest["tables"]["produtos"]["path"]
    assert con.execute(
        f"SELECT tipo, categoria, desde FROM '{prod}' WHERE area = 'produtos_saude' "
        "AND id = '8003400137'"
    ).fetchone() == ("Cadastrado", "Classe I", date(2022, 1, 13))


@needs_duckdb
def test_produtos_saude_fabricacao_on_the_fixtures(fake_dados, tmp_path):
    """One row per holder, manufacturer key and country: two spellings of the holder's own name
    are one propria row, three Ethicon names are three exterior rows; every pair joins back to
    produtos_saude_por_produto's aligned lists."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["produtos_saude_fabricacao"]
    assert entry["fontes"] == ["produtos_saude"] and entry["sort"] == ["cnpj", "chave", "pais"]
    assert entry["rows"] == 22
    assert [c["name"] for c in entry["columns"]] == list(
        derivar.PRODUTOS_SAUDE_FABRICACAO.descriptions
    )
    path = out / entry["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM F", f"FROM '{path}'")).fetchall()  # noqa: E731
    assert dict(q("SELECT vinculo, count(*) FROM F GROUP BY 1")) == {"exterior": 14, "propria": 8}
    detail = "SELECT chave, pais, vinculo, registros, registros_ativos, linhas FROM F WHERE cnpj = "
    assert q(detail + "'51943645000107'") == [
        ("biomedicalequipamentoseprodutosmedicocirurgicosltda", "BRASIL", "propria", 4, 4, 4)
    ]
    # POLAR FIX ... and Polar Fix Indústria ..., once each: the tie goes to the first in order
    assert q("SELECT fabricante, registros, linhas FROM F WHERE cnpj = '02881877000164'") == [
        ("POLAR FIX INDUSTRIA E COMERCIO DE PRODUTOS HOSPITALARES LTDA", 1, 2)
    ]
    assert q("SELECT chave, pais, vinculo FROM F WHERE cnpj = '54516661000101'") == [
        ("ethiconendosurgeryinc", "ESTADOS UNIDOS DA AMÉRICA", "exterior"),
        (
            "ethiconendosurgeryincsadecvmexicoparaethiconendosurgeryinc",
            "ESTADOS UNIDOS DA AMÉRICA",
            "exterior",
        ),
        ("ethiconendosurgeryllc", "PORTO RICO (ESTADOS UNIDOS)", "exterior"),
    ]
    empresas = out / manifest["tables"]["empresas"]["path"]
    known = {r[0] for r in con.execute(f"SELECT cnpj FROM '{empresas}'").fetchall()}
    assert {r[0] for r in q("SELECT cnpj FROM F")} <= known
    # the pairs are exactly the (holder, chave, país) of the per-product lists
    pspp = out / manifest["tables"]["produtos_saude_por_produto"]["path"]
    pares = (
        f"SELECT DISTINCT cnpj, unnest(fabricantes_chaves) AS c, unnest(fabricantes_paises) AS p "
        f"FROM '{pspp}'"
    )
    assert con.execute(f"{pares} EXCEPT SELECT cnpj, chave, pais FROM '{path}'").fetchall() == []
    assert con.execute(f"SELECT cnpj, chave, pais FROM '{path}' EXCEPT {pares}").fetchall() == []
    # registro × manufacturer pairs: 19 registros with one (8003400137's under two spellings),
    # 10132590043 and 10150470123 with three
    assert q("SELECT sum(registros) FROM F") == [(25,)]
    key = q("SELECT cnpj, chave, pais FROM F")
    assert key == sorted(key)


@needs_duckdb
def test_certificados_on_the_fixtures(fake_dados, tmp_path):
    """One row per petição of both CBPF files: copies collapse, the medicines' lines become a
    list, vigente is read against each file's own load date, and the plant's vínculo comes from
    the CNPJ or the code."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["certificados"]
    assert entry["fontes"] == ["cbpf", "cbpf_medicamentos"] and entry["sort"] == ["cnpj", "id"]
    assert [c["name"] for c in entry["columns"]] == list(derivar.CERTIFICADOS.descriptions)
    assert entry["source"]["loaded_at"] == "2026-10-13T00:00:01"  # the later of the two fontes
    assert entry["rows"] == 37 + 22
    path = out / entry["path"]
    con = duckdb.connect()
    q = lambda sql: con.execute(sql.replace("FROM C", f"FROM '{path}'")).fetchall()  # noqa: E731
    assert q("SELECT count(*), count(DISTINCT id) FROM C") == [(59, 59)]
    geral = "FROM C WHERE area <> 'medicamentos'"
    assert dict(q(f"SELECT vinculo, count(*) {geral} GROUP BY 1")) == {
        "propria": 8,
        "grupo": 1,
        "terceira": 2,
        "exterior": 12,
        "desconhecida": 14,
    }
    assert dict(q("SELECT area, count(*) FROM C GROUP BY 1")) == {
        "produtos_saude": 25,
        "insumos": 8,
        "cosmeticos": 2,
        "saneantes": 1,
        "alimentos": 1,
        "medicamentos": 22,
    }
    assert q(f"SELECT list(id ORDER BY id) {geral} AND vigente") == [
        (
            sorted(
                [
                    33815111, 34129091, 32808049, 32266273, 32495090, 34618018, 34532485,
                    33746047, 33712136, 31501320, 30397355, 33116746, 32752131, 34064657,
                    32272678,
                ]
            ),
        )
    ]  # fmt: skip
    assert q(f"SELECT id {geral} AND cancelado") == [(35417747,)]
    assert q(f"SELECT list(id ORDER BY id) {geral} AND tipo = 'CBPDA'") == [
        ([30397355, 32165176, 33746047],)
    ]
    # the copies of 34532485 are one row; 30397355 expires on the load day and is vigente
    assert q("SELECT publicacao, validade, vigente FROM C WHERE id = 34532485") == [
        (date(2026, 6, 1), date(2028, 6, 3), True)
    ]
    assert q("SELECT validade, dados_de, vigente FROM C WHERE id = 30397355") == [
        (date(2026, 10, 13), date(2026, 10, 13), True)
    ]
    assert q(f"SELECT count(*) {geral} AND (len(linhas) > 0 OR dados_de <> DATE '2026-10-13')") == [
        (0,)
    ]
    # the medicines: their own load date, the lines as a sorted list
    med = "FROM C WHERE area = 'medicamentos'"
    assert q(f"SELECT DISTINCT dados_de {med}") == [(date(2025, 9, 8),)]
    assert dict(q(f"SELECT vinculo, count(*) {med} GROUP BY 1")) == {
        "propria": 9,
        "grupo": 1,
        "terceira": 1,
        "exterior": 9,
        "desconhecida": 2,
    }
    assert q(f"SELECT list(id ORDER BY id) {med} AND vigente") == [
        ([23716100, 30556708, 30640752, 30679096, 32448792, 32461648, 32937945],)
    ]
    # VALIDO for ANVISA on 2025-09-08 though its validade had passed on 2025-07-24
    assert q("SELECT validade, vigente FROM C WHERE id = 29603099") == [(date(2025, 7, 24), False)]
    assert q("SELECT linhas, situacao, vinculo FROM C WHERE id = 30679096") == [
        (
            [
                "Produtos estéreis (Embalagem secundária)",
                "Produtos estéreis (Granel): Pós Liofilizados",
                "Produtos estéreis: Soluções Parenterais de Pequeno Volume com Preparação "
                "Asséptica",
            ],
            "Publicado deferimento",
            "exterior",
        )
    ]
    assert q("SELECT len(linhas), situacao, vigente FROM C WHERE id = 26977292") == [
        (6, "Publicado indeferimento", False)
    ]
    assert q("SELECT len(linhas) FROM C WHERE id IN (13232768, 11551882) ORDER BY id") == [
        (0,),  # 11551882: an old petição without lines
        (1,),  # 13232768: two rows, one line
    ]
    assert q(f"SELECT list(id ORDER BY id) {med} AND cancelado") == [([27818097, 32945995],)]
    assert q("SELECT inspecionada, pais, vinculo FROM C WHERE id = 12458871") == [
        ("LEMERY, S.A. DE C.V.", "MÉXICO", "desconhecida")
    ]
    assert q("SELECT inspecionada_cnpj, vinculo FROM C WHERE id = 30640752") == [
        ("44734671000828", "grupo")
    ]
    # Cristália holds certificates in both files
    assert q(
        "SELECT area, count(*) FROM C WHERE cnpj = '44734671000151' GROUP BY 1 ORDER BY 1"
    ) == [
        ("insumos", 2),
        ("medicamentos", 4),
    ]
    empresas = out / manifest["tables"]["empresas"]["path"]
    known = {
        r[0]: r[1] for r in con.execute(f"SELECT cnpj, alimentos FROM '{empresas}'").fetchall()
    }
    assert {"54516661000101", "21551379000106", "44734671000151"} <= known.keys()
    assert known["60409075000152"] > 0  # Nestlé, an MDSAP certificate and foods
    key = q("SELECT cnpj, id FROM C")
    assert key == sorted(key)


@needs_duckdb
def test_derived_table_with_a_missing_source(fake_dados, tmp_path, monkeypatch):
    """`-d cosmeticos` with no published site has nothing to derive produtos from: skipped,
    said so. A full build that lacks a source is a bug, not a skip."""
    log: list[str] = []
    with fake_dados.client() as http:
        result = build(tmp_path / "one", http=http, datasets=(COSMETICOS,), now=NOW, log=log.append)
    # the derived table whose only fonte is cosmeticos is built even in this partial build
    assert list(result["tables"]) == ["cosmeticos", "cosmeticos_por_empresa"]
    assert any(
        m.startswith(
            "skipping produtos: alimentos, alimentos_resultado, saneantes, medicamentos, "
            "produtos_saude not in"
        )
        for m in log
    )
    broken = dataclasses.replace(derivar.PRODUTOS, fontes=("alimentos", "nope"))
    monkeypatch.setattr(derivar, "DERIVADAS", (broken,))
    with fake_dados.client() as http, pytest.raises(DadosError, match="produtos needs nope"):
        build(tmp_path / "two", http=http, now=NOW)


def folder_files(index: Path) -> dict[str, bytes]:
    """Every file in the search folder `index` sits in, by relative path."""
    folder = index.parent
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in files}


@needs_duckdb
def test_carry_over_rejects_a_changed_parquet(fake_dados, tmp_path):
    published(fake_dados, tmp_path)
    fake_dados.manifest["tables"]["saneantes"]["sha256"] = "0" * 64
    with fake_dados.client() as http, pytest.raises(DadosError, match="saneantes"):
        build(
            tmp_path / "second",
            http=http,
            datasets=(ALIMENTOS,),
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="other",  # not the published one, so the all-304 path does not short-circuit
        )
    assert not list((tmp_path / "second").rglob("saneantes.parquet*"))


@needs_duckdb
def test_carry_over_rejects_a_changed_search_index(fake_dados, tmp_path):
    published(fake_dados, tmp_path)
    fake_dados.manifest["tables"]["cosmeticos"]["busca"]["sha256"] = "0" * 64
    with fake_dados.client() as http, pytest.raises(DadosError, match="search index of cosmeticos"):
        build(
            tmp_path / "second",
            http=http,
            datasets=(ALIMENTOS,),
            skip_unchanged=FakeDados.MANIFEST_URL,
            commit="other",
        )
    assert not [p for p in (tmp_path / "second").rglob("cosmeticos/*") if p.is_file()]


@needs_duckdb
def test_build_checks_every_header_before_converting(fake_dados, tmp_path):
    headers, body = fake_dados.files[ALIMENTOS_RESULTADO.url]
    fake_dados.files[ALIMENTOS_RESULTADO.url] = (headers, body.replace(b"VALIDADE", b"PRAZO", 1))
    with fake_dados.client() as http, pytest.raises(SchemaDriftError, match="PRAZO"):
        build(tmp_path / "dist", http=http)
    assert not (tmp_path / "dist").exists()  # alimentos, listed first, was not converted


# --- cli ----------------------------------------------------------------------

runner = CliRunner()


@needs_duckdb
def test_cli_list_and_build(fake_dados, monkeypatch, tmp_path):
    result = runner.invoke(cli.app, ["-f", "json", "dados", "list"])
    assert result.exit_code == 0, result.output
    names = [d["name"] for d in json.loads(result.stdout)]
    assert names == PUBLISHED

    monkeypatch.setattr(cli, "dados_http", fake_dados.client)
    out = tmp_path / "dist"
    args = ["dados", "build", "--out", str(out), "--dataset", "alimentos_resultado"]
    result = runner.invoke(cli.app, args, env={"GITHUB_SHA": "abc123"})
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)  # progress went to stderr, stdout is pure JSON
    assert list(summary["tables"]) == ["alimentos_resultado"]
    assert summary["tables"]["alimentos_resultado"]["rows"] == 20
    assert "convert TA_CONSULTA_ALIMENTOS_RESULTADO.CSV" in result.stderr
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == "abc123"

    args = ["dados", "build", "-o", str(tmp_path / "again"), "-d", "alimentos_resultado"]
    args += ["--skip-unchanged", str(out / "manifest.json")]
    result = runner.invoke(cli.app, args, env={"GITHUB_SHA": "abc123"})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "skipped": True,
        "reason": "unchanged",
        "published_build_id": manifest["build_id"],
    }

    result = runner.invoke(cli.app, ["dados", "build", "-o", str(out), "-d", "nope"])
    assert result.exit_code == 1 and "unknown dataset" in result.stderr


@needs_duckdb
def test_cli_with_fontes_builds_the_dcb_from_the_copy(fake_dados, monkeypatch, tmp_path):
    """With --fontes the default build has every table, and the blocked host gets no request."""
    monkeypatch.setattr(cli, "dados_http", fake_dados.client)
    fake_dados.status[DCB.url] = 403
    args = ["dados", "build", "-o", str(tmp_path / "dist")]
    args += ["--fontes", str(copia_dcb(tmp_path))]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "dist" / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["tables"]) == PUBLISHED and "falhas" not in manifest
    assert not any(r.url.host == "bibliotecadigital.anvisa.gov.br" for r in fake_dados.requests)


@needs_duckdb
def test_cli_default_build_leaves_out_the_dcb(fake_dados, monkeypatch, tmp_path):
    """Without --fontes the Biblioteca Digital answering 403 cannot stop the default build: DCB
    is not asked for. `-d dcb` still asks for it, and fails."""
    monkeypatch.setattr(cli, "dados_http", fake_dados.client)
    fake_dados.status[DCB.url] = 403
    out = tmp_path / "dist"
    result = runner.invoke(cli.app, ["dados", "build", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "skipping substancias_dcb: dcb not in this build" in result.stderr
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["tables"]) == [
        name for name in PUBLISHED if name not in ("dcb", "substancias_dcb")
    ]
    assert "falhas" not in manifest

    result = runner.invoke(cli.app, ["dados", "build", "-o", str(tmp_path / "x"), "-d", "dcb"])
    assert result.exit_code == 1 and "HTTP 403" in result.stderr


@needs_duckdb
def test_medicamentos_eans_come_from_cmed(tmp_path):
    """eans is the sorted distinct EAN 1 of the CMED presentations with the product's registro;
    a product outside the CMED gets an empty list, and a CMED row without registro is ignored."""
    import duckdb

    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * FROM (VALUES "
        "('186200018', '25351000000000001', 'NOVALGINA', '02685377000157 - SANOFI', 'Ativo', "
        "'Novo', 'ANALGESICOS', 'dipirona', DATE '2000-01-01', DATE '2030-01-01'), "
        "('186200018', '25351000000000002', 'NOVALGINA', '02685377000157 - SANOFI', 'Inativo', "
        "'Novo', 'ANALGESICOS', 'dipirona', DATE '2001-01-01', DATE '2029-01-01'), "
        "('101810583', NULL, 'NORFLOXACINO', '00000000000191 - LAB', 'Ativo', "
        "'Genérico', NULL, 'norfloxacino', NULL, NULL)"
        ") t(numero_registro_produto, numero_processo, nome_produto, empresa_detentora_registro, "
        "situacao_registro, categoria_regulatoria, classe_terapeutica, principio_ativo, "
        "data_finalizacao_processo, data_vencimento_registro)) "
        f"TO '{tmp_path / 'medicamentos.parquet'}' (FORMAT PARQUET)"
    )
    con.execute(
        "COPY (SELECT * FROM (VALUES ('186200018', '7891058001407'), "
        "('186200018', '7891058001308'), ('186200018', '7891058001308'), "
        "(NULL, '7890000000000'), ('186200018', NULL)"
        ") t(registro_produto, ean_1)) "
        f"TO '{tmp_path / 'cmed.parquet'}' (FORMAT PARQUET)"
    )
    paths = {"medicamentos": tmp_path / "medicamentos.parquet", "cmed": tmp_path / "cmed.parquet"}
    rows, columns = derivar.write(
        derivar.MEDICAMENTOS_POR_PRODUTO, paths, tmp_path / "out.parquet", tmp_path / "spill"
    )
    assert rows == 2 and columns[-1] == ("eans", "VARCHAR[]")
    got = con.execute(f"SELECT id, eans FROM '{tmp_path / 'out.parquet'}' ORDER BY id").fetchall()
    assert got == [("101810583", []), ("186200018", ["7891058001308", "7891058001407"])]


@needs_duckdb
def test_substancias_dcb_on_the_fixtures(fake_dados, tmp_path):
    """One row per distinct CMED substância; each `;` part matched to the DCB by the chave
    rule. The DCB sample holds the DCBs of some of the CMED sample's substâncias, so the build
    has full matches, a partial association and misses."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["substancias_dcb"]
    assert entry["fontes"] == ["cmed", "dcb"] and entry["sort"] == ["substancia"]
    assert entry["source"]["name"] == "derivada de cmed, dcb"
    # the later of the two loads: the CMED files (12:43:03), not the DCB xlsx (12:31:12)
    assert entry["source"]["loaded_at"] == "2026-10-09T12:43:03"
    assert [c["name"] for c in entry["columns"]] == list(derivar.SUBSTANCIAS_DCB.descriptions)
    types = {c["name"]: c["type"] for c in entry["columns"]}
    assert types["partes"] == "VARCHAR[]" and types["dcb_numeros"] == "INTEGER[]"
    assert types["dcb_denominacoes"] == "VARCHAR[]" and types["todas_na_dcb"] == "BOOLEAN"
    assert types["apresentacoes"] == "INTEGER"
    path = out / entry["path"]
    con = duckdb.connect()
    cmed_path = out / manifest["tables"]["cmed"]["path"]
    distinct, total = con.execute(
        f"SELECT count(DISTINCT substancia), count(*) FROM '{cmed_path}'"
    ).fetchone()
    assert entry["rows"] == distinct == 20
    rows = {
        r[0]: r[1:]
        for r in con.execute(
            f"SELECT substancia, apresentacoes, partes, dcb_numeros, dcb_denominacoes, "
            f"todas_na_dcb FROM '{path}'"
        ).fetchall()
    }
    assert list(rows) == sorted(rows) and sum(r[0] for r in rows.values()) == total
    assert rows["ABATACEPTE"][1:] == (["ABATACEPTE"], [9580], ["abatacepte"], True)
    # Latin binomial, upper case in the CMED, as written in the DCB
    assert rows["ACTAEA RACEMOSA L."][2:] == ([10707], ["Actaea racemosa L."], True)
    # every part of an association, in the CMED's order, hyphenated hydrate included
    assert rows["ASCORBATO DE SÓDIO;COLECALCIFEROL;CITRATO DE ZINCO TRI-HIDRATADO"][1:] == (
        ["ASCORBATO DE SÓDIO", "COLECALCIFEROL", "CITRATO DE ZINCO TRI-HIDRATADO"],
        [107, 2569, 11215],
        ["ascorbato de sódio", "colecalciferol", "citrato de zinco tri-hidratado"],
        True,
    )
    # a part without DCB keeps its place as NULL
    assert rows["21-ACETATO DE DEXAMETASONA;CLOTRIMAZOL"][2:] == (
        [None, 2529],
        [None, "clotrimazol"],
        False,
    )
    assert rows["MONOFOSFATO DE RIBOFLAVINA"][2:] == ([None], [None], False)
    # 24 parts; "ACETATO DE SÓDIO TRI-HIDRATADO " is trimmed; only glicose monoidratada (4486)
    # is in the DCB sample
    (nutricao,) = [r for s, r in rows.items() if s.startswith("ACETATO DE POTÁSSIO;")]
    assert len(nutricao[1]) == len(nutricao[2]) == len(nutricao[3]) == 24
    assert "ACETATO DE SÓDIO TRI-HIDRATADO" in nutricao[1]
    assert [n for n in nutricao[2] if n is not None] == [4486]
    assert nutricao[1][nutricao[2].index(4486)] == "GLICOSE MONOIDRATADA"
    assert sorted(s for s, r in rows.items() if r[4]) == [
        "ABATACEPTE",
        "ACARBOSE",
        "ACTAEA RACEMOSA L.",
        "ALPRAZOLAM",
        "ASCORBATO DE SÓDIO;COLECALCIFEROL;CITRATO DE ZINCO TRI-HIDRATADO",
        "RISDIPLAM",
    ]
    docs = (out / "dicionario.html").read_text(encoding="utf-8")
    assert '<section id="substancias_dcb">' in docs and '<section id="dcb">' in docs


@needs_duckdb
def test_substancias_dcb_rule(tmp_path):
    """The chave rule on both sides (accents, case, hyphens, spaces), empty parts dropped, a
    substância with no part kept with empty lists, NULL substância left out, and two DCB names
    with one key resolved to the lowest number."""
    import duckdb

    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * FROM (VALUES ('AMOXICILINA TRIHIDRATADA'), ('AMOXICILINA TRIHIDRATADA'), "
        "('CAFEÍNA ANIDRA ; DIPIRONA;'), (';'), (NULL), ('X-1')"
        f") t(substancia)) TO '{tmp_path / 'cmed.parquet'}' (FORMAT PARQUET)"
    )
    con.execute(
        "COPY (SELECT * FROM (VALUES (736, 'amoxicilina tri-hidratada'), (1642, 'cafeína'), "
        "(3121, 'dipirona'), (20, 'x 1'), (10, 'X-1')"
        f") t(numero_dcb, denominacao)) TO '{tmp_path / 'dcb.parquet'}' (FORMAT PARQUET)"
    )
    paths = {"cmed": tmp_path / "cmed.parquet", "dcb": tmp_path / "dcb.parquet"}
    rows, columns = derivar.write(
        derivar.SUBSTANCIAS_DCB, paths, tmp_path / "out.parquet", tmp_path / "spill"
    )
    assert rows == 4 and [c[0] for c in columns] == list(derivar.SUBSTANCIAS_DCB.descriptions)
    got = con.execute(f"SELECT * FROM '{tmp_path / 'out.parquet'}'").fetchall()
    assert got == [
        (";", 1, [], [], [], False),
        (
            "AMOXICILINA TRIHIDRATADA",
            2,
            ["AMOXICILINA TRIHIDRATADA"],
            [736],
            ["amoxicilina tri-hidratada"],
            True,
        ),
        (
            "CAFEÍNA ANIDRA ; DIPIRONA;",
            1,
            ["CAFEÍNA ANIDRA", "DIPIRONA"],
            [None, 3121],
            [None, "dipirona"],
            False,
        ),
        ("X-1", 1, ["X-1"], [10], ["X-1"], True),
    ]


@needs_duckdb
def test_alimentos_fabricacao_parses_from_the_right(tmp_path):
    """City and country come from the right, so a name keeps its own " - "; the CNPJ is the 14
    digits at the end, and an entry without them is dropped; a packer listed twice in one
    apresentação counts once; a foreign entry with an empty code is keyed by its name; a
    product counts once, as active when any apresentação is; an apresentação without an
    alimentos row is dropped; NULLs sort last."""
    import duckdb

    con = duckdb.connect()
    castro = "CASTROLANDA - COOPERATIVA AGROINDUSTRIAL - CASTRO - BRASIL ; 22222222000122"
    con.execute(
        "COPY (SELECT * FROM (VALUES "
        "(1, 101, '11111111000111', 'HOLDER UM', 'Ativo'), "
        "(1, 102, '11111111000111', 'HOLDER UM', 'Inativo'), "
        "(2, 201, '11111111000111', 'HOLDER UM', 'Inativo'), "
        "(3, 301, '55555555000155', 'HOLDER DOIS', 'Ativo'), "
        "(3, 302, '55555555000155', 'HOLDER DOIS', 'Ativo')"
        ") t(co_seq_produto, co_seq_apresentacao_produto, nu_cnpj_empresa, "
        "no_razao_social_empresa, situacao_registro)) "
        f"TO '{tmp_path / 'alimentos.parquet'}' (FORMAT PARQUET)"
    )
    con.execute(
        "COPY (SELECT * FROM (VALUES "
        f"(101, '{castro} | FULANO LTDA - ME - CARATINGA - BRASIL ;33333333000133 | "
        f"SEM CNPJ - CIDADE - BRASIL ; 123 | {castro}', "
        "'ACME GMBH - ALEMANHA ; D000001 | NUTRELLA ALIMENTOS - BRASIL ; | "
        "FÁBRICA – SUL - CHILE ; A000002'), "
        "(102, 'HOLDER UM - SÃO PAULO - BRASIL ; 11111111000111 | "
        "FILIAL - RECIFE - BRASIL ; 11111111000299 | DOIS - BRASIL ; 66666666000166', NULL), "
        f"(201, '{castro}', NULL), "
        "(301, NULL, 'SO NOME ;'), "
        "(302, NULL, NULL), "
        "(999, 'ORFA - X - BRASIL ; 44444444000144', 'ORFA GMBH - ALEMANHA ; D000009')"
        ") t(co_seq_apresentacao_produto, empresas_envasadoras, empresas_internacionais)) "
        f"TO '{tmp_path / 'alimentos_resultado.parquet'}' (FORMAT PARQUET)"
    )
    paths = {name: tmp_path / f"{name}.parquet" for name in ("alimentos", "alimentos_resultado")}
    out = tmp_path / "out.parquet"
    rows, columns = derivar.write(derivar.ALIMENTOS_FABRICACAO, paths, out, tmp_path / "spill")
    assert columns == [
        ("cnpj", "VARCHAR"),
        ("empresa", "VARCHAR"),
        ("fabricante_cnpj", "VARCHAR"),
        ("fabricante_codigo", "VARCHAR"),
        ("fabricante", "VARCHAR"),
        ("cidade", "VARCHAR"),
        ("pais", "VARCHAR"),
        ("vinculo", "VARCHAR"),
        ("produtos", "INTEGER"),
        ("produtos_ativos", "INTEGER"),
        ("apresentacoes", "INTEGER"),
    ]
    got = con.execute(f"SELECT * EXCLUDE (empresa) FROM '{out}'").fetchall()
    h1, h2 = "11111111000111", "55555555000155"
    assert got == [
        (h1, h1, None, "HOLDER UM", "SÃO PAULO", "BRASIL", "propria", 1, 1, 1),
        (h1, "11111111000299", None, "FILIAL", "RECIFE", "BRASIL", "grupo", 1, 1, 1),
        (h1, "22222222000122", None, "CASTROLANDA - COOPERATIVA AGROINDUSTRIAL", "CASTRO")
        + ("BRASIL", "terceira", 2, 1, 2),
        (h1, "33333333000133", None, "FULANO LTDA - ME", "CARATINGA", "BRASIL", "terceira")
        + (1, 1, 1),
        (h1, "66666666000166", None, "DOIS", None, "BRASIL", "terceira", 1, 1, 1),
        (h1, None, "A000002", "FÁBRICA – SUL", None, "CHILE", "exterior", 1, 1, 1),
        (h1, None, "D000001", "ACME GMBH", None, "ALEMANHA", "exterior", 1, 1, 1),
        (h1, None, None, "NUTRELLA ALIMENTOS", None, "BRASIL", "exterior", 1, 1, 1),
        (h2, None, None, "SO NOME", None, None, "exterior", 1, 1, 1),
    ]
    assert rows == len(got)
    assert {r[0] for r in con.execute(f"SELECT empresa FROM '{out}'").fetchall()} == {
        "HOLDER UM",
        "HOLDER DOIS",
    }


@needs_duckdb
def test_certificados_rules(tmp_path):
    """vigente is the rule, not ANVISA's STATUS: a validade on the load day counts, one the day
    before does not even when STATUS says VALIDO, an empty STATUS valid by date counts, no
    validade, an indeferimento or a cancellation never does. area reads every family spelling
    and falls back to outros; vinculo covers its five values; a medicines petição's lines
    become one sorted list and its situação comes from STATUS."""
    import duckdb

    con = duckdb.connect()
    um, tres, quatro = "11111111000111", "33333333000133", "44444444000144"
    cbpf = [
        # id, cnpj, inspecionada cnpj, código, país, tipo, assunto, situação, validade, status
        (1, um, um, None, "BRASIL", "CBPF", "70246 - PRODUTOS PARA SAÚDE - X")
        + ("Publicado deferimento", "2026-10-13", "VENCIDO"),
        (1, um, um, None, "BRASIL", "CBPF", "70246 - PRODUTOS PARA SAÚDE - X")
        + ("Publicado deferimento", "2026-10-13", "VENCIDO"),  # an exact copy
        (2, um, "11111111000299", None, "BRASIL", "CBPF", "70530 - INSUMOS FARMACÊUTICOS – X")
        + ("Publicado deferimento", "2026-10-12", "VALIDO"),
        (3, um, "22222222000122", None, "BRASIL", "CBPDA", "80001 - MDSAP -PRODUTOS - X")
        + ("Revalidação automática", "2027-01-01", None),
        (4, um, None, "C000001", "ALEMANHA", "CBPF", "70100 - COSMÉTICOS, PERFUMES - X")
        + ("Publicado deferimento", None, None),
        (5, tres, None, None, None, "CBPF", "70200 - SANEANTES - X")
        + ("Publicado indeferimento", None, "INDEFERIDO"),
        (6, tres, None, None, None, "CBPF", "70300 - ALIMENTOS - X")
        + ("Publicado deferimento", "2030-01-01", "CANCELADO"),
        (7, tres, None, "B000007", "CHINA", "CBPF", "70400 - VETERINÁRIOS - X")
        + ("Certificado emitido", "2030-01-01", "VALIDO"),
        (8, tres, None, "A000008", "ÍNDIA", "CBPF", "70568 - INSUMOS FARMACÊUTICOS ATIVOS - X")
        + ("Renovação automática", "2027-01-01", "VALIDO"),
    ]
    values = ", ".join(
        f"({i}, '{c}', 'EMPRESA', {sql_str(ic)}, {sql_str(cod)}, {sql_str(cod or ic)}, "
        f"{sql_str(p)}, '{t}', '{a}', 'INSPEÇÃO', '{s}', TIMESTAMP '2024-01-01', "
        f"CAST({sql_str(v)} AS TIMESTAMP), {sql_str(st)}, '25351000000000001', "
        "TIMESTAMP '2026-10-13 00:00:01')"
        for i, c, ic, cod, p, t, a, s, v, st in cbpf
    )
    con.execute(
        f"COPY (SELECT * FROM (VALUES {values}) t(co_documento_peticao, nu_cnpj_solicitante, "
        "razaosocial_solicitante, nu_cnpj_empresa_inspecionada, codigounico_inspecionada, "
        "razaosocial_inspecionada, no_pais_inspecionada, no_tipo_certificado, assunto, "
        "ds_tp_emissao_certificado, situacao, dt_publicacao, dt_validade, status, nu_processo, "
        f"dt_carga_etl)) TO '{tmp_path / 'cbpf.parquet'}' (FORMAT PARQUET)"
    )
    medicamentos = [
        # id, inspecionada cnpj, código, validade, status, linha
        (100, None, "A000100", "2027-01-01", "VALIDO", "Sólidos não estéreis: B"),
        (100, None, "A000100", "2027-01-01", "VALIDO", "Produtos estéreis: A"),
        (101, quatro, None, None, "INDEFERIDO", None),
        (102, quatro, None, "2027-01-01", "CANCELADO", "L"),
    ]
    values = ", ".join(
        f"({i}, '{quatro}', 'QUATRO', {sql_str(ic)}, {sql_str(cod)}, 'PLANTA', "
        f"'{'BRASIL' if ic else 'SUÉCIA'}', 'CBPF', '70852 - PRODUTOS BIOLÓGICOS - X', 'INSPEÇÃO', "
        f"TIMESTAMP '2024-01-01', CAST({sql_str(v)} AS TIMESTAMP), '{st}', '25351000000000002', "
        f"{sql_str(linha)}, TIMESTAMP '2025-09-08 15:28:52')"
        for i, ic, cod, v, st, linha in medicamentos
    )
    con.execute(
        f"COPY (SELECT * FROM (VALUES {values}) t(co_documento_peticao, nu_cnpj_solicitante, "
        "empresasolicitante, nu_cnpj_empresa_inspecionada, codigounico_inspecionada, "
        "razaosocial, pais, no_tipo_certificado, assunto, ds_tp_emissao_certificado, "
        "dt_publicacao, dt_validade, status, nu_processo, linhacertificacao, dt_carga_etl)) "
        f"TO '{tmp_path / 'cbpf_medicamentos.parquet'}' (FORMAT PARQUET)"
    )
    paths = {name: tmp_path / f"{name}.parquet" for name in ("cbpf", "cbpf_medicamentos")}
    out = tmp_path / "out.parquet"
    rows, columns = derivar.write(derivar.CERTIFICADOS, paths, out, tmp_path / "spill")
    assert columns == [
        ("id", "INTEGER"),
        ("cnpj", "VARCHAR"),
        ("empresa", "VARCHAR"),
        ("inspecionada_cnpj", "VARCHAR"),
        ("inspecionada_codigo", "VARCHAR"),
        ("inspecionada", "VARCHAR"),
        ("pais", "VARCHAR"),
        ("tipo", "VARCHAR"),
        ("area", "VARCHAR"),
        ("assunto", "VARCHAR"),
        ("emissao", "VARCHAR"),
        ("situacao", "VARCHAR"),
        ("publicacao", "DATE"),
        ("validade", "DATE"),
        ("vigente", "BOOLEAN"),
        ("cancelado", "BOOLEAN"),
        ("vinculo", "VARCHAR"),
        ("processo", "VARCHAR"),
        ("linhas", "VARCHAR[]"),
        ("dados_de", "DATE"),
    ]
    got = con.execute(
        f"SELECT id, area, vinculo, situacao, vigente, cancelado, linhas, dados_de FROM '{out}'"
    ).fetchall()
    deferido, carga, med = "Publicado deferimento", date(2026, 10, 13), date(2025, 9, 8)
    assert got == [
        (1, "produtos_saude", "propria", deferido, True, False, [], carga),
        (2, "insumos", "grupo", deferido, False, False, [], carga),
        (3, "produtos_saude", "terceira", "Revalidação automática", True, False, [], carga),
        (4, "cosmeticos", "exterior", deferido, False, False, [], carga),
        (5, "saneantes", "desconhecida", "Publicado indeferimento", False, False, [], carga),
        (6, "alimentos", "desconhecida", deferido, False, True, [], carga),
        (7, "outros", "exterior", "Certificado emitido", True, False, [], carga),
        (8, "insumos", "exterior", "Renovação automática", True, False, [], carga),
        (100, "medicamentos", "exterior", deferido, True, False)
        + (["Produtos estéreis: A", "Sólidos não estéreis: B"], med),
        (101, "medicamentos", "propria", "Publicado indeferimento", False, False, [], med),
        (102, "medicamentos", "propria", deferido, False, True, ["L"], med),
    ]
    assert rows == len(got)


def sql_str(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def write_produtos_saude(path: Path) -> None:
    """A synthetic produtos_saude: holder A's own name (accents and punctuation aside) is one of
    its manufacturers; one chave in two countries; a spelling majority and a tie; a third-party
    Brazilian maker; two manufacturers on one registro; `ª`/`º`; a registro without any."""
    import duckdb

    a = ("11111111000111", "Fábrica São João Ltda.")
    b = ("22222222000122", "OUTRA EMPRESA LTDA")
    c = ("33333333000133", "SEM FABRICANTE LTDA")
    linhas = [
        ("10000000001", a, "FABRICA SAO JOAO LTDA", "BRASIL", "VIGENTE"),
        ("10000000002", a, "ACME GMBH", "ALEMANHA", "01/01/2020"),
        ("10000000002", a, "ACME GMBH", "ESTADOS UNIDOS DA AMÉRICA", "01/01/2020"),
        ("10000000003", a, "Acme GmbH", "ALEMANHA", "01/01/2030"),
        ("80000000004", b, "Acme GmbH", "ALEMANHA", "VIGENTE"),
        ("80000000005", b, "Fábrica São João Ltda.", "BRASIL", "VIGENTE"),
        ("80000000005", b, "GAMMA 3ª GERAÇÃO Nº1", "BRASIL", "VIGENTE"),
        ("80000000006", b, "Beta Sp. z o.o.", "POLÔNIA", "VIGENTE"),
        ("80000000006", b, "BETA SP Z OO", "POLÔNIA", "VIGENTE"),
        ("80000000007", c, None, None, "VIGENTE"),
    ]
    values = ", ".join(
        f"('{r}', '25351000000000001', 'NOME TECNICO', 'II', 'PRODUTO', '{h[0]}', '{h[1]}', "
        f"{sql_str(f)}, {sql_str(p)}, DATE '2020-01-01', '{v}', TIMESTAMP '2026-10-12 19:00:00')"
        for r, h, f, p, v in linhas
    )
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES {values}) t(numero_registro_cadastro, numero_processo, "
        "nome_tecnico, classe_risco, nome_comercial, cnpj_detentor_registro_cadastro, "
        "detentor_registro_cadastro, nome_fabricante, nome_pais_fabric, "
        "dt_pub_registro_cadastro, validade_registro_cadastro, dt_atualizacao_dado)) "
        f"TO '{path}' (FORMAT PARQUET)"
    )


@needs_duckdb
def test_produtos_saude_fabricacao_rules(tmp_path):
    """The holder is its own manufacturer when the names match once normalized; a chave never
    merges countries; the commonest spelling names the key, ties alphabetical; registros count
    distinct registros, the active ones by the file's own date; rows without a manufacturer
    leave nothing."""
    import duckdb

    write_produtos_saude(tmp_path / "ps.parquet")
    out = tmp_path / "out.parquet"
    rows, columns = derivar.write(
        derivar.PRODUTOS_SAUDE_FABRICACAO,
        {"produtos_saude": tmp_path / "ps.parquet"},
        out,
        tmp_path / "spill",
    )
    assert columns == [
        ("cnpj", "VARCHAR"),
        ("empresa", "VARCHAR"),
        ("chave", "VARCHAR"),
        ("fabricante", "VARCHAR"),
        ("pais", "VARCHAR"),
        ("vinculo", "VARCHAR"),
        ("registros", "INTEGER"),
        ("registros_ativos", "INTEGER"),
        ("linhas", "INTEGER"),
    ]
    got = duckdb.connect().execute(f"SELECT * EXCLUDE (empresa) FROM '{out}'").fetchall()
    a, b = "11111111000111", "22222222000122"
    assert got == [
        # 'Acme GmbH' twice beats 'ACME GMBH' once, though the latter sorts first; the 2020
        # registro is not active on 2026-10-12
        (a, "acmegmbh", "Acme GmbH", "ALEMANHA", "exterior", 2, 1, 2),
        (a, "acmegmbh", "ACME GMBH", "ESTADOS UNIDOS DA AMÉRICA", "exterior", 1, 0, 1),
        (a, "fabricasaojoaoltda", "FABRICA SAO JOAO LTDA", "BRASIL", "propria", 1, 1, 1),
        (b, "acmegmbh", "Acme GmbH", "ALEMANHA", "exterior", 1, 1, 1),
        (b, "betaspzoo", "BETA SP Z OO", "POLÔNIA", "exterior", 1, 1, 2),  # a tie, one registro
        (b, "fabricasaojoaoltda", "FABRICA SAO JOAO LTDA", "BRASIL", "terceira", 1, 1, 1),
        (b, "gamma3ageracaono1", "GAMMA 3ª GERAÇÃO Nº1", "BRASIL", "terceira", 1, 1, 1),
    ]
    assert rows == len(got)


@needs_duckdb
def test_produtos_saude_fabricantes_lists_align(tmp_path):
    """fabricantes, fabricantes_chaves and fabricantes_paises come from one distinct set in one
    order, so index i is the same manufacturer in all three; no manufacturer gives three empty
    lists, never NULL."""
    import duckdb

    write_produtos_saude(tmp_path / "ps.parquet")
    out = tmp_path / "out.parquet"
    derivar.write(
        derivar.PRODUTOS_SAUDE_POR_PRODUTO,
        {"produtos_saude": tmp_path / "ps.parquet"},
        out,
        tmp_path / "spill",
    )
    got = dict(
        (r[0], r[1:])
        for r in duckdb.connect()
        .execute(f"SELECT id, fabricantes, fabricantes_chaves, fabricantes_paises FROM '{out}'")
        .fetchall()
    )
    alemanha, eua = "ALEMANHA", "ESTADOS UNIDOS DA AMÉRICA"
    assert got["10000000002"] == (
        [f"ACME GMBH ({alemanha})", f"ACME GMBH ({eua})"],
        ["acmegmbh", "acmegmbh"],
        [alemanha, eua],
    )
    assert got["80000000005"] == (
        ["Fábrica São João Ltda. (BRASIL)", "GAMMA 3ª GERAÇÃO Nº1 (BRASIL)"],
        ["fabricasaojoaoltda", "gamma3ageracaono1"],
        ["BRASIL", "BRASIL"],
    )
    assert got["80000000006"] == (
        ["BETA SP Z OO (POLÔNIA)", "Beta Sp. z o.o. (POLÔNIA)"],
        ["betaspzoo", "betaspzoo"],
        ["POLÔNIA", "POLÔNIA"],
    )
    assert got["80000000007"] == ([], [], [])


def test_missing_duckdb_is_a_clear_error(monkeypatch, tmp_path):
    from anvisa.dados.convert import convert

    monkeypatch.setitem(sys.modules, "duckdb", None)  # makes `import duckdb` raise ImportError
    csv = tmp_path / ALIMENTOS.file
    csv.write_bytes(sample(ALIMENTOS).read_bytes())
    with pytest.raises(DadosError, match=re.escape("pip install 'anvisa[dados]'")):
        convert(ALIMENTOS, csv, tmp_path / "x.parquet")
    result = runner.invoke(cli.app, ["-f", "json", "dados", "list"])
    assert result.exit_code == 0 and len(json.loads(result.stdout)) == len(PUBLISHED)


def test_dataset_is_a_frozen_value():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ALIMENTOS.name = "x"


@pytest.mark.live
def test_live_dados_head():
    """No credentials: checks TLS to dados.anvisa.gov.br and the headers the build relies on."""
    with http_client(timeout=30) as http:
        response = http.head(ALIMENTOS.url)
    assert response.status_code == 200
    assert response.headers.get("accept-ranges") == "bytes"
    assert response.headers.get("etag") or response.headers.get("last-modified")


# The build of 2026-11-03: eight files unchanged since 2026-10-13 while the petições still moved
STALL = {
    "tables": {
        "alimentos": {"source": {"last_modified": "Tue, 13 Oct 2026 19:09:39 GMT", "files": []}},
        "peticoes_alimento": {"source": {"last_modified": "Mon, 02 Nov 2026 13:33:34 GMT"}},
        "cmed": {
            "source": {
                "last_modified": None,
                "files": [
                    {"last_modified": "Fri, 09 Oct 2026 15:43:03 GMT"},
                    {"last_modified": "Fri, 09 Oct 2026 15:43:26 GMT"},
                ],
            }
        },
        "cbpf_medicamentos": {"source": {"last_modified": "Fri, 15 May 2026 07:52:35 GMT"}},
        "dcb": {"source": {"last_modified": "Fri, 09 Oct 2026 15:31:12 GMT"}},
        "produtos": {"source": {"name": "derivar.py", "last_modified": None}},
    }
}


def test_source_ages_hold_each_table_to_its_own_limit():
    from anvisa.dados.build import source_ages

    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    ages = {age.table: age for age in source_ages(STALL, now)}
    # no limit (cbpf_medicamentos, dcb) or not a source (the derived produtos): left out
    assert sorted(ages) == ["alimentos", "cmed", "peticoes_alimento"]
    alimentos = ages["alimentos"]
    assert (alimentos.days, alimentos.limit, alimentos.stale) == (20, 7, True)
    assert not ages["peticoes_alimento"].stale
    # the newer of the two CMED files counts, against the monthly limit
    assert ages["cmed"].last_modified.isoformat() == "2026-10-09T15:43:26+00:00"
    assert (ages["cmed"].limit, ages["cmed"].stale) == (45, False)
    assert source_ages({}, now) == []
    assert source_ages(STALL, now, {"alimentos": 30})[0].stale is False


def test_dados_idade_names_each_stale_table(tmp_path):
    from typer.testing import CliRunner

    from anvisa.cli import app

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(STALL), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["dados", "idade", str(path)])
    assert result.exit_code == 1
    assert "::error::alimentos: ANVISA's file unchanged" in result.output
    assert "::error::peticoes_alimento" not in result.output
    assert runner.invoke(app, ["dados", "idade", str(path), "--max-dias", "100000"]).exit_code == 0
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"tables": {"produtos": {"source": {}}}}), encoding="utf-8")
    assert runner.invoke(app, ["dados", "idade", str(empty)]).exit_code == 1
