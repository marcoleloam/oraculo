# F6 noturno bloqueado — investigação e correção

Base obrigatória: `bdf5cd5e87966f339f75d5b0be3039280ff08d59`.
Branch: `fix/f6-nightly-blocked`. Tentativa: `60fc06916717452f9334273288e2eda6`, geração 69.

## Fatos demonstrados

- `brain compile` sem `--apply`, com o payload congelado do job `7cede7482fdf481899c6e34cb9122960`, reproduziu `rejected`: proposta de **2.284.525 bytes**, limite **2.097.152**, uma página, 44 achados `not_promoted` e um `proposal_too_large`. Exit code da CLI: 0 (transporte bem-sucedido não implica compilação aprovada).
- A página acumulada, recuperada por `brain source` com hash conferido, tem **1.926.427 bytes**, **1.192 claims** e **120 fontes** registradas. O compilador unia as claims e fontes antigas às 20 selecionadas e tentava serializar tudo em uma única proposta.
- `_parse_report` gera um `not_promoted` por campo não vazio (`checks`, `next_steps`, `artifacts` etc.). Não conta candidatas nem impede publicação. A rejeição vinha do limite da proposta inteira. `_bind_preview` usava o primeiro código de achado e ocultava a causa impeditiva atrás de `not_promoted`.
- `pending_sources` só considera consumido raw citado por conceitos. A rejeição não cria essas citações; por isso mantém a coorte pendente. A ordenação por caminho/hash é determinística, mas novos relatórios raw podem entrar antes dos anteriores e mudar a composição das primeiras 20 fontes.
- Replay local, somente dry-run, sobre **141 arquivos** recuperados por CLI/artefato do mesmo projeto: a correção propõe lote de **283.943 bytes**, com as **20 fontes**, **162 claims**, sem adiamentos e com a página antiga intacta. Os mesmos 44 `not_promoted` permanecem informativos. Nenhum dado real foi publicado.
- O publicador recusava qualquer ocorrência textual de `reviews/` em todo o conteúdo, inclusive texto e quote de claim. Uma fonte sintética com uma menção literal reproduziu essa recusa e passa após a correção; referências de navegação a reviews continuam proibidas.
- O caminho F6 de `memory.propose` chamava diretamente o publicador e não registrava ciclo. Agora cria intenção durável e grava `saved` apenas com recibo confirmado; teste de perda de resposta verifica recuperação sem commit duplicado.

## Inferências e lacunas

O crescimento 42 → 44 não comprova crescimento do número de fontes: são contagens de campos nas fontes da seleção corrente. O código e o replay demonstram que não há promoção dessas fontes após rejeição e que novas fontes podem mudar a seleção. Não foi reconstruída a composição histórica das duas primeiras noites; a atribuição exata de quais campos entraram permanece não comprovada.

`review_reference_in_canonical` pertence à mesma interface compilador/publicador, mas tem causa distinta do excesso de tamanho. Não foi lido conteúdo de outro projeto nem provado que o falso positivo textual causou suas recusas de 07–09/11. O sucesso no release bdf5cd5e isoladamente não prova correção desse caso.

## Mudanças

- Rollover apenas para o conceito noturno padrão e não revisado. Identidades derivadas do manifesto; redução por fontes inteiras e adiamentos explícitos. Conceitos explícitos mantêm a recusa de tamanho. Páginas revisadas não são contornadas.
- Pré-seleção pura evita que mais de 20 fontes individualmente grandes escondam fontes elegíveis posteriores. Adiamentos não viram fontes consumidas nem sucesso de corpus.
- Recibo conserva seleção congelada completa, declara subconjunto compilado e adiamentos, e mostra o motivo impeditivo real. Prompt e contrato descrevem `deferred` e o conceito do lote.
- Ciclo F6 persistido antes da publicação e reconciliável pelo journal, sem criar relatório raw recursivo. Somente publicações futuras são rastreadas.
- CAS de página/fontes, base global nula, escopo privado, proteção reviewed, ausência de supported automático e `corpus_acceptance: false` preservados.

## Verificação

Artefatos privados ficam em `.oraculo-attempt-60fc/`, fora do commit.

- Testes novos contra a base: `pytest -q -p no:cacheprovider tests/test_nightly_overflow.py tests/test_nightly_compile_preflight.py` → **8 failed, 4 passed**, exit **1** (antes de adicionar o teste integrado de rollover).
- Mesmos testes com a correção inicial → **12 passed**, exit **0**.
- Suíte relevante completa de memória/conhecimento (19 módulos) → **211 passed em 43,46 s**, exit **0**, antes da última ampliação de cobertura integrada. Resultado final registrado no relatório de execução.
- Suíte final completa dos módulos `memory`, `knowledge`, `nightly`, `managed` e `settlement`: **495 passed em 166,74 s**, exit **0**. Comando e stdout preservados em `check-receipt.json` e `tests-complete.log`.
- Lint Ruff dos arquivos alterados e `git diff --check`: **pass**, exit **0**.
- Ambiente de testes: Python/pytest existentes em `/Users/marcoleloam/projects/work/oraculo/.venv/bin/python`, com `PYTHONPATH=src`, `PYTHONDONTWRITEBYTECODE=1`; não houve instalação de dependências, API paga ou troca de modelo.

## Ativação pelo operador

Nenhum push, merge, deploy, ativação ou restart foi executado. Revisar o commit desta branch sobre bdf5cd5e, preservando os commits de release ausentes de main. Não aplicar sobre main sem integrar explicitamente a base correta.

Após revisão, o operador deve construir e selar um bundle com o SHA integral do commit aprovado como `release-id` e `source-commit`, usando o procedimento de `docs/INSTALACAO.md`. Conferir manifesto e wheels locais. Preparar com `oraculo runtime upgrade --bundle CAMINHO --dry-run`, depois o mesmo comando sem dry-run. Avaliar `oraculo runtime activate SHA_APROVADO --dry-run`; somente o operador decide executar a ativação real, respeitando manutenção, quiescência e protocolos de carry existentes. Conferir `runtime status` e `service status` após ativar.

Validar apenas um novo ciclo noturno admitido: preview/lookup coincidentes, receipt confirmado, ciclo saved ligado à tentativa e seleção seguinte sem as fontes publicadas. Não republicar nem reexecutar os ingests históricos falhos.

## Riscos residuais

Lotes tornam o conhecimento padrão distribuído entre páginas em vez de perpetuar um único documento sem teto. O seletor faz extração local adicional para verificar admissão por fonte, com custo proporcional às fontes pendentes inspecionadas. Fontes que não cabem sozinhas exigem revisão; permanecem raw íntegros, com adiamento explícito. Não há migração ou backfill de ciclos passados e nenhuma prova de comportamento do daemon ativo com o novo código, pois ativação ficou fora do escopo.
