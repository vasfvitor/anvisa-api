# anvisa

Python client and CLI for ANVISA's official **Consultas Externas** API
(fila de análise, listas, UDI de dispositivos médicos, termos GMDN, nomes técnicos, assuntos
de peticionamento).

```bash
uv tool install anvisa   # or: pipx install anvisa
```

Credentials come from https://api.anvisa.gov.br/ (login Gov.br → Client ID / Client Secret).
Write them to `~/.config/anvisa/credentials.env` (`CLIENT_ID=...`, `CLIENT_SECRET=...`, chmod 600)
or export `ANVISA_CLIENT_ID` / `ANVISA_CLIENT_SECRET`.

```bash
anvisa fila areas
anvisa fila consulta 167
anvisa udi search --nome cateter
anvisa --format json udi get 377
anvisa lista consulta 2141
anvisa nome-tecnico search --size 50
anvisa assunto lista --busca bioequival
```

```python
from anvisa import Client

with Client.from_env() as anvisa:
    queue = anvisa.fila.consulta(167)
    page = anvisa.udi.search(nomeComercial="cateter", size=50)
```

What the library handles for you, because ANVISA's spec doesn't say so: a User-Agent that
Cloudflare accepts, token caching and renewal (29-minute tokens, no refresh token), a mirror
of the gateway's rate limit (burst 25, 1 request/s) so loops never hit 429, 1-based request
pages against 0-based responses, required filter keys, and typed exceptions for the
validation errors ANVISA reports as HTTP 500.

Full write-up, spec overlay and recorded fixtures: https://github.com/vasfvitor/anvisa-api
