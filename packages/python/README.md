# `anvisa`

Python client and command-line tool for ANVISA's official **Consultas Externas** API
(fila de análise, listas, UDI de dispositivos médicos, termos GMDN, nomes técnicos, assuntos
de peticionamento, produtos para saúde, certificados de boas práticas e tabaco: all 59
endpoints of the spec).

```bash
uv tool install anvisa   # or: pipx install anvisa
```

Credentials come from https://api.anvisa.gov.br/ (login Gov.br → Client ID / Client Secret).
Write them to `~/.config/anvisa/credentials.env` (`CLIENT_ID=...`, `CLIENT_SECRET=...`, chmod 600)
or export `ANVISA_CLIENT_ID` / `ANVISA_CLIENT_SECRET`.

```bash
anvisa fila areas
anvisa fila query 167
anvisa udi search --nome cateter
anvisa --format json udi get 377
anvisa lista query 2141
anvisa nome-tecnico search --size 50
anvisa assunto list --busca bioequival
anvisa saude search --nome 1000VINYL
anvisa certificado search --nacional --internacional --status 0
anvisa tabaco get 33007111
```

```python
from anvisa import Client

with Client() as anvisa:  # credentials from the environment or ~/.config/anvisa/credentials.env
    queue = anvisa.fila.query(167)
    page = anvisa.udi.search(nomeComercial="cateter", size=50)
```

What the library handles for you, because ANVISA's spec doesn't say so: a User-Agent that
Cloudflare accepts, token caching and renewal (29-minute tokens, no refresh token), a mirror
of the gateway's rate limit (burst 25, 1 request/s) so loops never hit 429, 1-based request
pages against 0-based responses, required filter keys, and typed exceptions for the
validation errors ANVISA reports as HTTP 500.

Open data, no credentials: `pip install 'anvisa[dados]'` adds `anvisa dados build`, which turns
ANVISA's bulk CSVs from dados.anvisa.gov.br (alimentos, saneantes, cosméticos, petições,
produtos irregulares; Windows-1252, unescaped quotes) into typed, sorted Parquet plus eight
derived tables (`produtos`, `empresas`, `medidas`, `cosmeticos_por_empresa`,
`alimentos_por_produto`, `medicamentos_por_produto`, `produtos_saude_por_produto`,
`alimentos_fabricacao`), a `manifest.json` with Portuguese column descriptions
and a data dictionary page. The daily result is live at
https://vasfvitor.github.io/anvisa-api/ (dictionary: `dicionario.html`).

Full write-up, spec overlay and recorded fixtures: https://github.com/vasfvitor/anvisa-api
