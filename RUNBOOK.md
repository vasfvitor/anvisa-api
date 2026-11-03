# RUNBOOK do anvisa-api

O que fazer quando um workflow deste repo fica vermelho. Comece pelo
[RUNBOOK do anvisa-hub](https://github.com/vasfvitor/anvisa-hub/blob/main/RUNBOOK.md): lá estão o mapa
dos seis repos, a rotina e o que vale para todos (rodar de novo, run cinza, lockfile, Pages).

As mensagens aparecem aqui como o log mostra, em inglês. Procure com Ctrl+F um trecho da mensagem que
você viu. `<tabela>`, `N` e afins marcam a parte que muda.

Quem cria ou muda um alarme ou uma mensagem de erro atualiza este arquivo no mesmo commit.

## Índice

| Workflow | Trecho da mensagem | Entrada |
|---|---|---|
| dados | `ANVISA's file unchanged for` | [Fonte parada na ANVISA](#fonte-parada-na-anvisa) |
| dados | `no source Last-Modified in` | [Manifest publicado ilegível](#manifest-publicado-ilegível) |
| dados | `(kept the previous build)`, `nothing changed, but` | [Download falhou, tabela anterior mantida](#download-falhou-tabela-anterior-mantida) |
| dados | `error: HTTP 403 for`, no passo Build | [Tabela nova não baixou](#tabela-nova-não-baixou) |
| dados | `header changed`, `unparseable CSV`, `wrong number of fields`, `do not parse as` | [A ANVISA mudou o formato de um arquivo](#a-anvisa-mudou-o-formato-de-um-arquivo) |
| dados | `update anvisa/dados/cmed.py`, `update anvisa/dados/dcb.py`, `cannot read the DCB copy` | [CMED mudou ou a cópia da DCB está errada](#cmed-mudou-ou-a-cópia-da-dcb-está-errada) |
| dados | `rows against`, `refusing it` | [Tabela encolheu](#tabela-encolheu) |
| dados | `does not match its manifest entry` | [Arquivo publicado não confere](#arquivo-publicado-não-confere) |
| dados | `Traceback`, `which this build lacks`, `needs DuckDB` | [Erro de código no build](#erro-de-código-no-build) |
| dados | `uv sync`, Pages, timeout, deploy | [Instalação, Pages, timeout e deploy](#instalação-pages-timeout-e-deploy) |
| drift | `git diff --exit-code --stat -- spec/` | [A ANVISA mudou a documentação da API](#a-anvisa-mudou-a-documentação-da-api) |
| drift | `Traceback` no passo Snapshot | [O snapshot da documentação falhou](#o-snapshot-da-documentação-falhou) |
| ci | ruff, pytest, `spec-drift` | [Lint, formato ou testes](#lint-formato-ou-testes) |
| release | `tag v... != version`, PyPI | [Release falhou](#release-falhou) |
| keepalive | qualquer | [keepalive](#keepalive) |

## dados

Roda todo dia às 21:00 UTC (18:00 em Brasília). Baixa os CSVs de dados.anvisa.gov.br, converte para
Parquet e publica no Pages com o `manifest.json`. Quando nada mudou, não publica nada e o site anterior
continua no ar.

A ordem dos passos importa: Build, Upload, Deploy, idade das fontes, downloads que falharam. Os dois
últimos rodam **depois** do deploy. Então um vermelho neles não quer dizer que nada foi publicado. E
quando o Build falha, o passo da idade não roda: o alarme de fonte parada some naquele dia.

### Fonte parada na ANVISA

- **Onde aparece:** passo "Check the age of each ANVISA source".
- **Mensagem:** `::error::<tabela>: ANVISA's file unchanged for N days (since AAAA-MM-DD, limit L)`
- **O que quer dizer:** o arquivo da ANVISA dessa tabela não muda há mais dias que o limite dela. O
  limite é 7 dias para os arquivos que a ANVISA reescreve nos dias úteis e 45 para a lista da CMED.
- **O site foi afetado?** Não quebrou. Continua com o último dado que a ANVISA publicou, só que velho.
- **Agir ou esperar:** esperar. A causa está na ANVISA.
- **O que fazer:**
  1. Ver a idade de cada tabela:
     `cd packages/python && uv run anvisa dados idade https://vasfvitor.github.io/anvisa-api/manifest.json`
  2. Se quiser, avisar a ANVISA pelo Fala.BR.
  3. O run fica vermelho todo dia até a ANVISA voltar a publicar.
- **Pedir ajuda quando:** a ANVISA voltou a publicar (a data no comando acima mudou) e o alarme
  continua. Ou o alarme aparece todo mês para uma tabela que a ANVISA só atualiza uma vez por mês (ver
  [Problemas conhecidos](#problemas-conhecidos)).

### Manifest publicado ilegível

- **Onde aparece:** passo "Check the age of each ANVISA source".
- **Mensagem:** `::error::no source Last-Modified in <manifest>`
- **O que quer dizer:** o passo não conseguiu ler o `manifest.json` publicado, ou ele não tem datas.
- **O site foi afetado?** Talvez. Abra
  `https://vasfvitor.github.io/anvisa-api/manifest.json` no navegador.
- **Agir ou esperar:** rodar de novo uma vez.
- **Pedir ajuda quando:** repetir, ou o manifest não abrir no navegador.

### Download falhou, tabela anterior mantida

- **Onde aparece:** passo "Report sources that failed to download". Ou no passo Build, quando nada mais
  mudou.
- **Mensagens:**
  - `::error::<tabela>: <erro> (kept the previous build)`
  - `error: nothing changed, but <tabelas> could not be downloaded (<erros>); build <id> stays live`
- O `<erro>` costuma ser um destes:
  - `HTTP 403 for <url>` ou `HTTP 5xx for <url>`;
  - `<url>: got N of M bytes (truncated)`;
  - `<url>: <erro> (after 3 attempts)`.
- **O que quer dizer:** a ANVISA não entregou o arquivo dessa tabela, mesmo com três tentativas. A tabela
  ficou com o arquivo do build anterior, byte por byte, e aparece em `falhas` no manifest.
- **O site foi afetado?** As outras tabelas saíram normais. A que falhou continua com o dado anterior.
  Com `nothing changed, but`, nada foi publicado e o site anterior segue igual.
- **Agir ou esperar:** esperar. O próximo run tenta de novo.
- **O que fazer:** ver se o erro é 5xx ou timeout (servidor instável, passa sozinho) ou 403 (bloqueio).
  Um 403 que se repete na mesma URL quer dizer que a ANVISA bloqueou o acesso automático. **Não contornar
  o bloqueio.**
- **Pedir ajuda quando:** o mesmo erro passa de uma semana, ou é 403 todo dia.

### Tabela nova não baixou

- **Onde aparece:** passo "Build (skipped when nothing changed)".
- **Mensagem:** `error: HTTP 403 for <url>` (ou outro erro de download), sem `kept the previous build`.
- **O que quer dizer:** é uma tabela que nunca foi publicada. Como não há versão anterior para manter, o
  build para.
- **O site foi afetado?** Nada foi publicado. O site anterior continua, com todas as tabelas.
- **Exemplo real:** 29 a 31/10/2026, a Biblioteca Digital da ANVISA respondeu 403 para a DCB. A solução
  foi ler uma cópia baixada à mão, em `fontes/dcb/`.
- **Agir ou esperar:** pedir ajuda. Só acontece quando alguém acabou de acrescentar uma tabela.

### A ANVISA mudou o formato de um arquivo

- **Onde aparece:** passo "Build (skipped when nothing changed)".
- **Mensagens:**
  - ``error: <ARQUIVO>: header changed (added [...], missing [...], first difference at column N); update the `<nome>` entry in anvisa/dados/catalog.py``
  - `error: unparseable CSV at character N: ...`
  - `error: <ARQUIVO>: N of M records have the wrong number of fields (first: record K)`
  - `error: <ARQUIVO>: N of M values in <COLUNA> do not parse as <TIPO>; did ANVISA change the format?`
- **O que quer dizer:** um arquivo chegou diferente do que o código espera. Pode ser uma coluna nova ou
  sumida, aspas quebradas, campos a mais, ou datas e números num formato novo. O build para antes de
  gravar qualquer Parquet.
- **O site foi afetado?** Nada foi publicado. O site anterior continua com todas as tabelas. Mas,
  enquanto isso durar, **nenhuma** tabela atualiza, nem as que estão certas.
- **Agir ou esperar:** agir. Precisa mudar o código.
- **O que fazer:**
  1. Rodar de novo uma vez. Se o servidor devolveu uma página de erro no lugar do CSV, isso aparece como
     `header changed` e passa no run seguinte.
  2. Se repetir, o roteiro está no `CONTRIBUTING.md`, "Adding an open-data dataset", passos 2 a 5:
     perfilar o arquivo novo, atualizar a entrada em `catalog.py`, `descricoes.py` e a fixture.
- **Pedir ajuda quando:** repetir no segundo run.

### CMED mudou ou a cópia da DCB está errada

- **Onde aparece:** passo "Report sources that failed to download".
- **Mensagens:**
  - `::error::cmed: <ARQUIVO>: price column '<coluna>' (<nome>) is not in COLUMNS; add the rate to ALIQUOTAS in anvisa/dados/cmed.py (kept the previous build)`
  - `...: unknown column '<coluna>'; update anvisa/dados/cmed.py`
  - `...: missing columns [...]; update anvisa/dados/cmed.py`
  - `::error::dcb: dcb.xlsx: header is [...], expected [...]; update anvisa/dados/dcb.py (kept the previous build)`
  - `::error::dcb: <pasta>/dcb: cannot read the DCB copy: ...`
- **O que quer dizer:** a lista da CMED mudou de colunas (por exemplo, uma alíquota nova de ICMS quando
  um estado muda a lei), ou a cópia da DCB trocada à mão tem outro formato.
- **O site foi afetado?** A tabela fica como estava; as outras saíram normais. O anvisa-feeds e o portal
  continuam com o mês anterior da CMED.
- **Agir ou esperar:** agir.
- **O que fazer:**
  - **CMED:** mudar `cmed.py`, lançar uma versão nova da biblioteca no PyPI e subir a versão no
    anvisa-feeds, que usa o mesmo leitor no job `cmed`. Pedir ajuda.
  - **DCB:** conferir o arquivo trocado em `fontes/dcb/` (ver
    [Nova versão da DCB](#nova-versão-da-dcb)). O CI já fica vermelho no push que trocou a cópia, no
    teste `test_the_kept_copy_of_the_dcb_is_coherent`.

### Tabela encolheu

- **Onde aparece:** passo "Build (skipped when nothing changed)".
- **Mensagem:** `error: <tabela>: N rows against M in build <id>; refusing it (rerun with a lower --min-rows-ratio if the source really shrank)`
- **O que quer dizer:** o arquivo novo tem menos de 90% das linhas da tabela publicada. O build recusa,
  para não publicar um arquivo cortado.
- **O site foi afetado?** Nada foi publicado. O site anterior continua, mas nenhuma tabela atualiza
  enquanto isso durar.
- **Agir ou esperar:** esperar um run. Se a ANVISA publicou um arquivo incompleto, o problema some
  quando ela corrigir.
- **Pedir ajuda quando:** repetir. Se a tabela encolheu de verdade, o limite precisa baixar por um
  build. A mensagem cita a opção `--min-rows-ratio`, mas o workflow não deixa passar essa opção: é
  preciso mudar o código.

### Arquivo publicado não confere

- **Onde aparece:** passo "Build (skipped when nothing changed)".
- **Mensagens:**
  - `error: published table <nome> does not match its manifest entry`
  - `error: published table <nome> lacks path/sha256/bytes; rebuild it`
  - `error: published search index of <nome> does not match its manifest entry`
  - `error: published search file <nome>/<arquivo> is not N bytes`
- **O que quer dizer:** o build reaproveita os arquivos já publicados das tabelas que não mudaram e
  confere cada um com o manifest. Um deles veio diferente. Isso costuma acontecer por cache do Pages
  (até 10 minutos) ou porque outro deploy estava no meio.
- **O site foi afetado?** Nada foi publicado. O site anterior continua.
- **O que fazer:** esperar uns 10 minutos e rodar de novo.
- **Pedir ajuda quando:** repetir.

### Erro de código no build

- **Onde aparece:** passo "Build (skipped when nothing changed)".
- **Mensagens:** um `Traceback` do Python, `error: <tabela> needs <fontes>, which this build lacks`,
  `building Parquet needs DuckDB`, `busca needs the columns`.
- **O que quer dizer:** um bug, ou uma mudança da ANVISA que o código não previu. Por exemplo, um
  `UnicodeDecodeError` quer dizer que o arquivo mudou de codificação.
- **O site foi afetado?** Nada foi publicado. O site anterior continua.
- **Agir ou esperar:** pedir ajuda.

### Instalação, Pages, timeout e deploy

- **"Sync from the lockfile" falhou:** ver o RUNBOOK do hub, "Lockfile desatualizado". Se for erro de
  rede do PyPI, rodar de novo.
- **"Read the Pages URL" falhou:** o Pages deste repo não está configurado. Settings → Pages → Source:
  GitHub Actions.
- **Passou de 20 minutos:** rodar de novo. Se repetir, o servidor da ANVISA está lento.
- **Upload ou Deploy falhou:** rodar de novo. O site anterior continua. Nesse caso, o passo da idade não
  roda, mas o de downloads que falharam roda.

## drift

Roda nos dias 1 e 15, às 09:17 UTC. Baixa de novo a descrição da API da ANVISA e as páginas de
documentação do portal, e fica vermelho se algo mudou em relação ao que está em `spec/`. Não publica
nada.

### A ANVISA mudou a documentação da API

- **Onde aparece:** passo "Fail if ANVISA changed anything".
- **Mensagem:** linhas como ` spec/<arquivo> | N +++---`, depois `Process completed with exit code 1`.
- **O que quer dizer:** a ANVISA mudou a descrição da API ou uma página de documentação.
- **O site foi afetado?** Não. A biblioteca e os dados seguem funcionando.
- **O que fazer:** depende do arquivo.
  - **Só `spec/portal/sngpc.openapi.json` (ou outro de SNGPC/SNCR):** essas APIs estão fora do escopo da
    biblioteca, então a mudança não afeta nada. Foi o que aconteceu em 1º/10, 15/10 e 1º/11/2026. Para o
    próximo run ficar verde:
    1. na raiz do repo, `make snapshot` e depois `make spec models`;
    2. `git diff --stat` deve mostrar só esse arquivo;
    3. commit.
  - **Páginas do portal (`spec/portal/*-doc.json`, tutoriais):** ler o diff. Costuma ser texto. Mesmo
    roteiro: `make snapshot`, `make spec models`, commit.
  - **`spec/consultas-externas.openapi.json`:** a própria API mudou. Se apareceram endpoints novos,
    `test_spec.py` falha até eles serem tratados. Pedir ajuda.

### O snapshot da documentação falhou

- **Onde aparece:** passo "Snapshot the spec and the portal pages", com um `Traceback`.
- **O que quer dizer:** um download da documentação falhou. Não há nova tentativa.
- **O que fazer:** rodar de novo mais tarde. O run seguinte também tenta.

## ci

Roda em push no `main` e em pull request. Não publica nada.

### Lint, formato ou testes

- **Mensagens:** a saída do `ruff`, `Would reformat: <arquivo>`, falhas do `pytest`, ou o
  `git diff --exit-code` do job `spec-drift`.
- **O que fazer**, na máquina:

  ```bash
  cd packages/python && uv sync --extra dados
  uv run ruff check . && uv run ruff format .
  uv run pytest
  cd ../.. && make spec models   # commitar o diff, se houver
  ```

- **Pedir ajuda quando:** um teste falha sem que ninguém tenha mudado o código. Pode ser um teste preso a
  uma data (ver [Problemas conhecidos](#problemas-conhecidos)).

## release

Roda quando uma tag `v*` é enviada. Publica a biblioteca no PyPI. Não mexe no site.

### Release falhou

- **`tag vX.Y.Z != version A.B.C`:** a tag não bate com `__version__` em
  `packages/python/src/anvisa/__init__.py`. Corrigir a versão e mover a tag, só se o PyPI ainda não tiver
  essa versão.
- **Passo Test falhou:** o mesmo que no [ci](#lint-formato-ou-testes). Em 03/11/2026, o 0.8.0 falhou aqui
  e a tag foi movida depois da correção.
- **Passo "Publish to PyPI" falhou:**
  - se a versão já existe no PyPI: o PyPI nunca aceita a mesma versão duas vezes. É preciso uma versão
    nova;
  - se for erro de autorização: conferir no pypi.org o trusted publisher do projeto (owner `vasfvitor`,
    repositório `anvisa-api`, workflow `release.yml`, environment `pypi`).

## keepalive

Roda no dia 1 de cada mês, às 07:41 UTC. Reativa os workflows deste repo, para o GitHub não desligar os
crons depois de 60 dias sem atividade.

- **Vermelho:** uma chamada à API do GitHub falhou. Rodar de novo.
- **Verde:** abrir o log e conferir que há linhas `enabled .github/workflows/...`. Se não houver nenhuma,
  ele não fez nada (ver [Problemas conhecidos](#problemas-conhecidos)).

## Procedimento manual

### Nova versão da DCB

A Biblioteca Digital da ANVISA bloqueia acesso automático, então a lista DCB vem de uma cópia em
`fontes/dcb/`, trocada à mão. O passo a passo está no `CONTRIBUTING.md`, "Updating the DCB list". Em
resumo:

1. baixar o xlsx no navegador;
2. trocar `fontes/dcb/dcb.xlsx` e atualizar `dcb.json` e `LICENSE.md`;
3. commit. O CI confere a cópia no push.

Nenhum alarme avisa de versão nova. Vale olhar a página da coleção de vez em quando.

## Problemas conhecidos

Achados em 2026-11-03, ainda sem correção. Cada um diz o efeito e o que fazer enquanto isso.

- **Teste preso a uma data (a partir de 10/11/2026, 13:33 UTC).** `test_dados_idade_names_each_stale_table`
  usa um `Last-Modified` fixo de 02/11/2026 e compara com o relógio de verdade. A partir dessa hora, `ci`
  e `release` ficam vermelhos com `AssertionError` nesse teste. O site não é afetado, porque o `dados`
  não roda testes. Enquanto isso, não dá para lançar versão. Correção prevista na fase 3.
- **medicamentos pode dar alarme falso todo mês.** O catálogo diz que a ANVISA atualiza esse arquivo uma
  vez por mês, mas o limite dele é de 7 dias. A confirmar quando a ANVISA voltar a publicar. Fase 2.
- **keepalive verde sem ter feito nada.** Se a primeira chamada à API falhar, o passo sai verde mesmo
  assim. Por isso vale conferir as linhas `enabled` no log. Fase 2.
- **DCB sem alarme de versão nova.** Ver [Nova versão da DCB](#nova-versão-da-dcb).
- **Perda de linhas aos poucos.** O limite de 90% compara só com o build anterior. Várias quedas
  pequenas seguidas passam. Fase 3.
- **Coluna de preço da CMED inteira vazia passa sem alarme.** Fase 3.
- **`falhas` antigo no manifest.** Depois de uma falha, se no dia seguinte nada mudar, o run fica verde,
  mas o manifest publicado ainda lista a falha antiga. Fase 2.
- **Página nova do portal quase não aparece.** O `git diff` do `drift` não vê arquivos novos. Uma página
  nova só se nota pela mudança em `spec/portal/menus.json`. Fase 2.
- **Idade lida só do cabeçalho HTTP.** Se a ANVISA publicar de novo um arquivo antigo, ele parece novo.
  Fase 3.
