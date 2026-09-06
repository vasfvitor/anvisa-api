# Deep-dive: APIs oficiais da ANVISA (Portal de APIs + SNGPC)

*Leitura direta das especificações OpenAPI + **teste real com credenciais** em 06/09/2026. Versão local; a cópia no projeto "oss health" (`claude/deep-dive-anvisa-apis-2026.md`) ainda tem a versão pré-teste e precisa ser atualizada a partir de uma sessão web.* Respostas brutas em `notes/probe-2026-09-06/`.

---

## 0. Resultado do teste ao vivo (06/09/2026, 9 requisições)

**Credenciais**: o portal deu HTTP 500 na primeira tentativa de criar o client (`portal-apis/api/v1/keycloak/clients/create`), mas funcionou em nova tentativa no mesmo dia. Client ID é numérico (~11 dígitos); o secret foi rotacionado após a criação.

**Cloudflare bloqueia o User-Agent padrão do curl** (403 "Attention Required") — até no `api-docs` público. Com UA de navegador, tudo passa. Um cliente precisa mandar UA próprio.

**Token** (`POST https://acesso.prd.apps.anvisa.gov.br/auth/realms/externo/protocol/openid-connect/token`, `grant_type=client_credentials`): 200, `expires_in: 1740` (29 min), sem refresh token. JWT: `aud: ["consultas-externas-service","account"]`, papel `consultas-externas-service.roles = ["CONSULTASEXTERNAS_LEITURA"]`, `preferred_username: service-account-<clientId>`. Papel único de leitura; sem sinal de escopos por API.

**Rate limit — descoberto pelos headers** (Spring Cloud Gateway, token bucket, por client): `X-RateLimit-Burst-Capacity: 25`, `X-RateLimit-Replenish-Rate: 1` (1 req/s sustentado), `X-RateLimit-Remaining` em cada resposta. Sem docs, mas totalmente observável — um cliente pode se auto-regular.

| Endpoint | Corpo | Resultado |
|---|---|---|
| `GET /api/v1/fila/areafila` | — | **200**, 11 áreas (Medicamento=1, Cosmético=2, Saneantes=3, Alimento=6, Empresas=7, Dispositivos Médicos=8, Toxicologia=9, Tabaco=10, PAF=11, IVD=12, Insumo Farmacêutico=15). 3 s na primeira chamada (cold start), <0,5 s depois |
| `GET /api/v1/lista/arealista` | — | **200**, 4 áreas (Empresas, Insumo, Medicamento, Toxicologia) |
| `GET /api/v1/nomeTecnico/categorias` | — | **200**, 2 categorias (Equipamento ou Material=8, Diagnóstico in vitro=12) |
| `GET /api/v1/assunto/assuntos` | — | **200, 303 KB** — lista completa de códigos de assunto de peticionamento, sem paginação |
| `POST /api/v1/udi` | `{"page":0,"size":3}` | **500** `"mensagem":"mensagens.MSG-062"` — chave i18n não resolvida, `mensagem_detalhada: "{}"` |
| `POST /api/v1/udi` | `sorting: []` | **500** com stack Jackson: `sorting` é `Map<String, Sort.Direction>`; classe `br.gov.anvisa.module.consultas.util.PaginationBuilder` |
| `POST /api/v1/udi` | `sorting:{}, filter:{}` | **500** MSG-062 de novo (causa desconhecida — talvez página 1-based ou filtro obrigatório) |
| `POST /api/v1/nomeTecnico` | `page:0, sorting:{}, filter:{}` | **500** `"Page index must not be less than zero!"` → **paginação é 1-based** (o serviço faz `page-1`) |
| `POST /api/v1/fila/consulta` | `filter:{"areaFila":8}` | **500** `"Filtro 'subfila' não informado."` → o filtro exige a chave `subfila` (id vindo de `/fila/{codigoGrupo}/subfila`) |

**Leituras**: (1) toda validação de entrada volta como **HTTP 500** com envelope `{status, mensagem, data_hora:[y,m,d,h,m,s,ns], mensagem_detalhada}` — um cliente decente teria que mapear isso para erros tipados a partir do texto. (2) O `PaginationBuilder` real: `sorting: {coluna: "ASC"|"DESC"}`, `page` (1-based), `size`, `column`/`order` (alternativa), `filter: {chave: valor}` com chaves obrigatórias por endpoint, não documentadas. (3) Os GETs de catálogo e a lista de assuntos funcionam de primeira e são dados úteis por si.

### Rodada 2 (7 requisições; total do dia: 16, bucket nunca abaixo de 20/25)

| Endpoint | Corpo | Resultado |
|---|---|---|
| `POST /api/v1/udi` | `page:1, filter:{}` | **500** MSG-062 de novo |
| `POST /api/v1/udi` | `page:1, filter:{"nomeComercial":"cateter"}` | **200** — 69 resultados, 35 páginas; `UdiDTO` com `udiDi` (GTIN-14), fabricante legal, detentora + CNPJ, termo GMDN, `estagio: CONSOLIDADO`, `dtPublicacao` em epoch-ms. **MSG-062 = "pelo menos um filtro é obrigatório"** |
| `POST /api/v1/nomeTecnico` | `page:1, filter:{}` | **200** — 2.678 nomes técnicos em 1.339 páginas; resposta diz `pageNumber: 0` → confirma **request 1-based, response 0-based** |
| `GET /api/v1/fila/8/fila` | — | **200** — 8 grupos para Dispositivos Médicos (Alterações=285, Registros=281, Revalidações=287, Transferências=661…) |
| `GET /api/v1/fila/285/subfila` | — | **200** — 13 subfilas (ex.: "Alterações de Notificações de Equipamentos Classe I"=167, reenquadramentos RDC 751) |
| `POST /api/v1/fila/consulta` | `page:1, size:3, filter:{"subfila":167}` | **200** — devolve **array** (não `Page`) com **40 itens ignorando `size`**: `nuOrdem`, `dtEntrada`, `nuExpediente`, `codAssunto`, `nuProcesso`, `numeroProcessoFormatado` (25351.216322/2025-86), `dtGeracaoFila` (fila gerada diariamente) |
| `GET /api/v1/assunto/10013` | — | **200** — detalhe completo: tipo de solicitação, sistema (SOLICITA + link), serviço gov.br, formulário `.docx` (conteúdo `null`), 5 documentos de checklist, **6 faixas de taxa por porte**, fundamentação legal em texto |

**Leituras adicionais**: (4) datas sempre em epoch-ms. (5) `fila/consulta` não pagina — é a lista inteira da subfila; `page/size` são ignorados. (6) o `descricao` do assunto vem `null` no detalhe (está em `assunto`), e `tipoProduto.id` vem `null` — DTOs preenchidos pela metade. (7) A fila de análise é o dado mais "vivo" e o mais valioso comercialmente: posição diária de cada processo por subfila, algo que hoje se acompanha na mão no portal.

**Ainda por testar**: `GET /udi/{id}` (o `DetalheDispositivoDTO` rico), `POST /udi/termoGmdn`, `POST /lista/consulta` (chaves de filtro?), `POST /assunto/` (filtros?), e o formato dos `…/download`.

## 1. O que a "Consultas Externas API" realmente é

Spec: `https://api-gateway.prd.apps.anvisa.gov.br/consultas-externas-api/v3/api-docs` (definição única). Servidor: `https://api-gateway.prd.apps.anvisa.gov.br/consultas-externas-api`. Spring Boot; `info.title` com placeholder Maven não resolvido (`@project.artifactId@`). Cópia local: `notes/consultas-externas-openapi-2026-09-06.json`.

**Auth**: OAuth2 client credentials via Keycloak (realm `externo`), Client ID/Secret emitidos após login Gov.br.

**32 endpoints em 5 tags** — nenhum é registro de medicamentos, CMED ou bulário:

| Tag | O que cobre | Endpoints-chave |
|---|---|---|
| Consulta UDI | UDI-DI de dispositivos médicos, detalhe rico (`DispositivoDTO`), histórico | `POST /api/v1/udi`, `GET /api/v1/udi/{id}`, `GET /api/v1/udi/{idDispositivo}/{idHistorico}`, `GET /api/v1/udi/historico` |
| Termos GMDN | Nomenclatura global de dispositivos | `POST /api/v1/udi/termoGmdn`, `GET /api/v1/udi/termoGmdn/{codigo}` |
| Nomes Técnicos | Nomes técnicos de produtos para saúde, classe de risco | `POST /api/v1/nomeTecnico`, `GET /api/v1/nomeTecnico/categorias` |
| Consulta Fila | Fila de análise de processos por área/grupo/subfila | `GET /api/v1/fila/areafila`, `GET /api/v1/fila/{areaFila}/fila`, `GET /api/v1/fila/{codigoGrupo}/subfila`, `POST /api/v1/fila/consulta` |
| Consulta Lista | "Listas calculadas" por área/grupo/sublista | `GET /api/v1/lista/arealista`, `POST /api/v1/lista/consulta` |
| Consulta Assuntos | Códigos de assunto: tipo de solicitação, formulários, documentação, fundamentação legal, taxas por porte | `GET /api/v1/assunto/assuntos`, `POST /api/v1/assunto/`, `GET /api/v1/assunto/{codigoAssunto}` |

Paginação: `POST` com `PaginationBuilder` (`sorting: map<col, ASC|DESC>`, `page` 1-based, `size`, `column`, `order`, `filter: map<string, object>`). Respostas no formato Spring `Page*`. Endpoints `…/download` sem formato especificado.

**Descasamento com o menu do portal**: "Certificados", "Funcionamento de Empresa", "Empresas Internacionais", "Alimentos" não aparecem nesta spec — serviços separados ou páginas descritivas; não verificado.

## 2. API SNGPC

Spec: `https://sngpc-api.anvisa.gov.br/swagger/v1/swagger.json`. Lançada em 05/09/2025; web service antigo sendo descontinuado; fornecedores de software de farmácia devem implementar. Quatro endpoints: `GET /api/Auth/ObterTokenKeycloak?code=`, `POST /v1/Authentication/GetToken` (`{username,password}` → JWT), `POST /v1/FileXml/EnviarArquivoXmlSNGPC` (XML em base64), `GET /v1/FileXml/ConsultaDadosArquivoXml/{email}/{cnpj}/{hash}`. Não testada (exige credenciais de farmácia).

## 3. Quem já está nesse espaço

Nada aponta para as APIs oficiais novas. Bulário: `iuryLandin/bulario-api` (101★). Preço/CMED: `yagoluiz/meuremedio-extracao`. Empresa: `LibreCodeCoop/consulta-empresa-anvisa-cli`. SNGPC: `endersonmaia/sngpc-go` (Go, MIT, ativo ago/2026, parser só — não chama a API). Sinal de mercado: Infosimples vende automação paga de Bulário e Funcionamento de Empresa.

## 4. Reavaliação pós-teste

**(a) Cliente para Consultas Externas** — **confirmado**: todos os 5 domínios (UDI, nomes técnicos, fila, assuntos, listas-catálogo) responderam com dados reais. E é o tipo de API onde um cliente agrega valor de verdade, porque o comportamento útil não está na spec: UA obrigatório (Cloudflare), token de 29 min sem refresh, request 1-based/response 0-based, `filter` com chaves obrigatórias por endpoint (`udi` exige ≥1 filtro, `fila/consulta` exige `subfila`), `fila/consulta` que devolve array inteiro ignorando paginação, validação devolvida como 500 com mensagem em português, datas em epoch-ms, rate limit só nos headers. Cliente pequeno, tipado a partir da spec, com auto-throttle pelos headers e erros tipados por mensagem — mais um CLI `anvisa fila <subfila>` seria o primeiro uso concreto. Risco residual: spec pode mudar sem aviso (placeholders, DTOs meio preenchidos).

**(b) SDK SNGPC** — inalterado: urgente para o mercado, mas sem credenciais de farmácia não dá para verificar ponta a ponta. `sngpc-go` é o lar natural.

**(c) Medicamentos / CMED / bulário** — fora das APIs oficiais; scraping.

**Próximo passo**: decidir escopo mínimo do cliente (auth + throttle + `fila` + `udi` + `assunto`) e linguagem; gerar tipos a partir do JSON da spec salvo em `notes/`; usar as respostas brutas de `notes/probe-2026-09-06/` como fixtures de teste para não gastar requisições reais.
