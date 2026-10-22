"""anvisa.dados: parsing ANVISA's CSV dialect, downloading, converting to Parquet, building."""

from __future__ import annotations

import dataclasses
import hashlib
import html
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import headers_only
from typer.testing import CliRunner

from anvisa import cli
from anvisa.dados import CATALOG, DERIVADAS, Unchanged, build, busca, cmed, derivar, parse, select
from anvisa.dados.catalog import (
    ALIMENTOS,
    ALIMENTOS_RESULTADO,
    CMED,
    COSMETICOS,
    PETICOES_ALIMENTO,
    PETICOES_ALIMENTO_ANDAMENTO,
    PRODUTOS_IRREGULARES,
    SANEANTES,
    TYPES,
)
from anvisa.dados.descricoes import DESCRICOES
from anvisa.dados.fetch import download, http_client
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
}
HEADERS = {
    ALIMENTOS.name: "headers_alimentos.txt",
    ALIMENTOS_RESULTADO.name: "headers_alimentos_resultado.txt",
    SANEANTES.name: "headers_saneantes.txt",
    PETICOES_ALIMENTO.name: "headers_peticoes_alimento.txt",
    PETICOES_ALIMENTO_ANDAMENTO.name: "headers_peticoes_alimento_andamento.txt",
    PRODUTOS_IRREGULARES.name: "headers_produtos_irregulares.txt",
    COSMETICOS.name: "headers_cosmeticos.txt",
}
# the CMED pair is read by cmed.py, not the Windows-1252 parser: its samples are separate
CMED_SAMPLES = {"pmc": "cmed_pmc_head.csv", "pmvg": "cmed_pmvg_head.csv"}
CMED_HEADERS = {"pmc": "headers_cmed_pmc.txt", "pmvg": "headers_cmed_pmvg.txt"}
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
    return list(records(decode(sample(ds).read_bytes()), len(ds.columns)))


def write_dialect(path: Path, rows: list[list[str]]) -> Path:
    """Write rows the way ANVISA does: every field quoted, nothing escaped, Windows-1252."""
    path.write_bytes(
        "".join(";".join(f'"{v}"' for v in row) + "\n" for row in rows).encode("cp1252")
    )
    return path


class FakeDados:
    """Serves the fixture samples at their catalog URLs with the recorded headers, honours
    If-None-Match, and serves `manifest` (when set) at MANIFEST_URL."""

    SITE_URL = "https://example.github.io/anvisa-api/"
    MANIFEST_URL = SITE_URL + "manifest.json"

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.manifest: dict | None = None
        self.manifest_status = 200
        self.site: Path | None = None  # a previous build's `out`, served under SITE_URL
        self.files = {}
        for ds in CSV_DATASETS:
            headers, _ = headers_only(DADOS / HEADERS[ds.name])
            headers.pop("Content-Length")  # recorded for the full file; httpx sets the sample's
            self.files[ds.url] = (headers, sample(ds).read_bytes())
        for kind, file in cmed.FILES.items():
            headers, _ = headers_only(DADOS / CMED_HEADERS[kind])
            headers.pop("Content-Length")
            self.files[catalog_url(file)] = (headers, (DADOS / CMED_SAMPLES[kind]).read_bytes())

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == self.MANIFEST_URL:
            if self.manifest is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(self.manifest_status, json=self.manifest)
        if url.startswith(self.SITE_URL):
            file = self.site / url.removeprefix(self.SITE_URL) if self.site else None
            if file is None or not file.is_file():
                return httpx.Response(404, text="not found")
            return httpx.Response(200, content=file.read_bytes())
        if url not in self.files:
            return httpx.Response(404, text="not found")
        headers, body = self.files[url]
        if request.headers.get("If-None-Match") == headers["ETag"]:
            return httpx.Response(304, headers={"ETag": headers["ETag"]})
        return httpx.Response(200, headers=headers, content=body)

    def client(self) -> httpx.Client:
        return http_client(transport=httpx.MockTransport(self.handler))

    def data_requests(self) -> list[httpx.Request]:
        """The CSV downloads: not the manifest, not a carried-over Parquet."""
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
        assert ds.url.startswith("https://dados.anvisa.gov.br/")
        assert ds.kind == "cmed" or ds.url.endswith(ds.file)
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
    assert select(["medicamentos"]) == (CMED,) and select(["cmed"]) == (CMED,)
    assert CMED.url == catalog_url("TA_PRECO_MEDICAMENTO.csv") and CMED.local_file == "cmed.csv"
    assert (
        CMED.source_name("pf_17_5_alc") == "PF 17,5 % ALC" and CMED.source_name("tarja") == "TARJA"
    )
    with pytest.raises(ValueError, match="unknown kind"):
        dataclasses.replace(ALIMENTOS, kind="xlsx")
    assert select(["cosmeticos"]) == (COSMETICOS,)


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


def test_download_http_errors_are_dados_errors(tmp_path):
    def handler(request):
        if "RESULTADO" in str(request.url):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(503, text="down")

    with http_client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DadosError, match="HTTP 503"):
            download(http, ALIMENTOS, tmp_path)
        with pytest.raises(DadosError, match="boom"):
            download(http, ALIMENTOS_RESULTADO, tmp_path)
    assert list(tmp_path.iterdir()) == []


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


# --- busca: the search files --------------------------------------------------


def test_words_matches_the_cases_shared_with_the_site():
    """fixtures/dados/tokens.json is the contract with anvisa-dash: the two tokenizers must
    split every case the same way, or searches miss products without any error."""
    cases = json.loads((DADOS / "tokens.json").read_text(encoding="utf-8"))
    assert cases["versao"] == busca.VERSION and len(cases["casos"]) == 40
    assert busca.words(None) == []
    for case in cases["casos"]:
        assert busca.words(case["entrada"]) == case["palavras"], case["entrada"]


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
        "data": "dados abertos da ANVISA (dados.anvisa.gov.br)",
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
    # the unchanged files answered 304 first, then were fetched again for a complete build
    statuses = [r.headers.get("If-None-Match") for r in fake_dados.data_requests()]
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
    assert entry["fontes"] == ["alimentos", "alimentos_resultado", "saneantes"]
    assert entry["sort"] == ["cnpj", "area", "id"]
    assert entry["source"]["name"] == "derivada de alimentos, alimentos_resultado, saneantes"
    assert entry["source"]["url"].endswith(
        "/blob/abc123/packages/python/src/anvisa/dados/derivar.py"
    )
    # the latest load among the sources: saneantes' (its fixture has two records of a later file)
    assert entry["source"]["loaded_at"] == "2026-10-06T00:00:00"
    assert entry["nulls_added"] == {} and entry["rejected_records"] == 0
    assert [c["name"] for c in entry["columns"]] == list(derivar.PRODUTOS.descriptions)
    path = out / entry["path"]
    assert entry["bytes"] == path.stat().st_size and entry["rows"] == 36 + 24
    con = duckdb.connect()
    rows = con.execute(f"SELECT area, count(*) FROM '{path}' GROUP BY 1 ORDER BY 1").fetchall()
    # 38 apresentações of 36 products; 25 saneante records, one expediente listed twice
    assert rows == [("alimentos", 36), ("saneantes", 24)]
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
    assert con.execute(f"SELECT DISTINCT tipo FROM '{path}' ORDER BY 1").fetchall() == [
        ("Notificado",),
        ("Registrado",),
    ]
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
    paths = {}
    for name, table in (("alimentos", "a"), ("alimentos_resultado", "r"), ("saneantes", "s")):
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
    """One row per 14-digit CNPJ across the three product tables and the measures' targets;
    counts are products, and only a real CNPJ makes a company."""
    import duckdb

    out = tmp_path / "dist"
    with fake_dados.client() as http:
        manifest = build(out, http=http, now=NOW, commit="abc123")
    entry = manifest["tables"]["empresas"]
    assert entry["fontes"] == ["alimentos", "saneantes", "cosmeticos", "produtos_irregulares"]
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
        "SELECT bool_and(produtos = alimentos + saneantes + cosmeticos AND ativos <= produtos) "
        "FROM E"
    )
    assert sums == (True,)
    assert q("SELECT count(*) FROM E WHERE nome IS NULL AND produtos > 0") == [(0,)]


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
def test_derived_table_with_a_missing_source(fake_dados, tmp_path, monkeypatch):
    """`-d cosmeticos` with no published site has nothing to derive produtos from: skipped,
    said so. A full build that lacks a source is a bug, not a skip."""
    log: list[str] = []
    with fake_dados.client() as http:
        result = build(tmp_path / "one", http=http, datasets=(COSMETICOS,), now=NOW, log=log.append)
    # the derived table whose only fonte is cosmeticos is built even in this partial build
    assert list(result["tables"]) == ["cosmeticos", "cosmeticos_por_empresa"]
    assert any(
        m.startswith("skipping produtos: alimentos, alimentos_resultado, saneantes not in")
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
