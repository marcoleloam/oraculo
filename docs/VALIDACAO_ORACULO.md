# Validação do Oráculo

Gerado por: Codex / gpt-6-astra · Build · 2026-10-18.

Este runbook descreve os 24 critérios de aceitação do Oráculo e seus procedimentos de regressão. A V1 foi aceita em 19/10/2026 com **27/27 critérios aprovados** (24 critérios originais e três regressões complementares). O [resumo público da V1](VALIDACAO_V1.md) explica os resultados e os limites. As evidências brutas de desenvolvimento, conversas e recibos do ambiente local não são distribuídos neste repositório.

Escopos do aceite: entrega real em destino local descartável, proatividade limitada ao início autônomo e à deduplicação demonstrados, e iPhone conforme teste e declaração humana. O AT-020 combina a transição medida com interfaces fechadas e o ensaio separado de reinício preservando identidades. Novos ambientes e versões precisam de sua própria avaliação.

## Preparação e limites

Use Python 3.12 e `uv sync --frozen`. A suíte usa SQLite, vaults, sockets e repositórios temporários, sem login, API de modelo ou deploy externo. `tests/test_lifecycle.py` inicia subprocessos reais do serviço, substituindo somente o loop de rede; verifica exclusão mútua, permissões, SIGTERM e recuperação de objetivo persistido. `tests/test_service.py` exercita CLI request → Unix socket → handlers com T3 simulado.

```sh
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q
uv run --frozen pytest -q tests/test_service.py tests/test_lifecycle.py
```

Para os testes humanos, crie projeto/repositório e destino descartáveis, com dados sintéticos e credenciais já existentes. Cadastre o projeto de teste no T3, mantenha o Mac acordado e use configuração dedicada com vault/estado fora da árvore de projetos. Configure modelos somente após verificar elegibilidade pelo plano existente, sem excedentes pagos; catálogo disponível não prova elegibilidade. Não habilite projetos de clientes reais nesta avaliação.

Inicialização local e login independente (pare o serviço antes do login para respeitar o lock):

```sh
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml init
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml doctor
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml auth login
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml serve
```

Em outro terminal, após cadastro pelo daemon:

```sh
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml status
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml submit PROJECT_ID 'Objetivo descartável com checks'
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml memory ingest PROJECT_ID --source /caminho/projeto/decisao.md
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml memory query PROJECT_ID --query 'Qual decisão foi registrada?'
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml memory lint PROJECT_ID
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml review open PROJECT_ID
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml review close PROJECT_ID --session SESSION_ID
uv run --frozen oraculo --config /caminho/absoluto/config-teste.toml cancel PROJECT_ID OBJECTIVE_ID
```

Substitua caminhos/IDs pelos valores reais. O plugin original 1.28.0 deve usar o `wiki_folder` retornado pela abertura da revisão. Finalize edição/comandos do plugin antes de fechar a revisão. Conflitos preservam ambos os conteúdos; não copie arquivos sobre o canônico para forçar sucesso. Consulte `docs/OPERACAO.md` para operação/configuração.

## Registro de evidências

Guarde evidências sanitizadas por eval em diretório de relatório: data/hora, versão/commit do Oráculo/T3/plugin, configuração sem credenciais, projeto/thread/task/run, modelo, estado esperado/observado, saída de comandos e artefatos/hashes. Não registre tokens, cookies, conversas de clientes ou arquivos privados. Cada teste humano exige atestação de Marco com essas evidências; catálogo, mocks e ausência de erro não bastam. Mantenha falhas e pendências explícitas no relatório; `/eval ORACULO` é a fase independente que produz o recibo de aceitação.

## Cenários do contrato

### projects · AT-001

Descoberta paginada e escopo real de projetos.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_config.py::test_projects_scope_and_registration
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### providers · AT-002

Três provedores em tarefas distintas e planos existentes.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

No ambiente de teste, verificar autenticação por plano existente e excedentes desativados em cada provedor. Executar 3 tarefas diferentes sequenciais, uma em Claude, Codex e Grok. Registrar IDs de projeto/thread/run/task, modelo escolhido e evidência de resultado; não aceitar catálogo disponível como execução bem-sucedida nem usar API paga.

### worktree · AT-003

Checkout e commit corretos preservando raiz com alterações locais.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

Em repositório de teste cadastrado, deixar alteração não commitada na raiz, lançar tarefa em worktree via T3 e conferir projeto, base commit, branch e path. Provar que a alteração local permaneceu intacta e não foi presumida no filho. Guardar diff/status e IDs.

### restart · AT-004

Retomada não duplica tarefa viva.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_reconcile.py::test_restart_with_live_child
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### launch_unknown · AT-005

Launch com resposta perdida reconcilia antes de repetir.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_reconcile.py::test_launch_lost_response
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### memory_session · AT-006

Nova sessão recupera decisão, tarefa e aprendizado com fontes.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

Persistir uma decisão, tarefa e aprendizado em corpus de teste. Abrir nova sessão T3 sem histórico anterior e consultar o serviço. Conferir resposta contra as 3 fontes, IDs e timestamps; tarefa histórica deve ser distinguida de status T3 atual. Registrar respostas e referências sem segredos.

### protected · AT-007

Fontes e páginas protegidas permanecem intactas.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_memory_schema.py::test_raw_and_reviewed_protection
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### scope · AT-008

Links, aliases e shared não vazam contexto de outro cliente.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_memory_scope.py::test_homonyms_do_not_cross_projects
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### delivery · AT-009

Entrega autônoma de teste com checks, merge e deploy verificáveis.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

Executar objetivo em repositório/destino descartável configurado, com checks obrigatórios e credenciais existentes. Observar implementação, checks aprovados, merge e deploy sem nova aprovação por etapa; conferir commit, artefato e saúde do destino. Repetir caso com check falho para comprovar impedimento. Anexar recibos e IDs.

### unavailable · AT-010

Provedor indisponível não é tratado como pronto.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_routing.py::test_unavailable_provider_no_silent_fallback
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### single_executor · AT-011

Dois coordenadores e descendentes não obtêm grants concorrentes.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_state.py::test_global_grant_includes_descendants
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### cancel · AT-012

Cancelamento persiste e aguarda confirmação da árvore.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_reconcile.py::test_cancel_survives_restart_and_pending_children
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### external_effect · AT-013

Efeito remoto incerto não dispara deploy duplicado.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_operations.py::test_lost_deploy_response
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### failed_checks · AT-014

Checks reprovados e deploy falho não viram entrega.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_operations.py::test_failed_checks_block_delivery
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### review_conflict · AT-015

Revisão isolada preserva contribuições, conflitos e snapshots.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_review.py::test_concurrent_review_and_publish_conflict
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### git_memory · AT-016

Revisão Git rastreável e restauração em cópia.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_memory_git.py::test_commit_and_restore_copy
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### disconnect · AT-017

Desconexão limitada e reconciliação sem duplicação.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_reconcile.py::test_disconnect_and_reconnect
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### compiled_memory · AT-018

Ingest publica fonte, referências, índice e log corretamente.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_memory_publish.py::test_ingest_index_and_append_only_log
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### run_identity · AT-019

Resultado de run anterior não encerra o atual.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_t3_contracts.py::test_old_run_does_not_complete_current
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### continuous · AT-020

Continua para etapa seguinte com interface fechada.

Bateria detalhada, comando automatizado e formulário do ensaio real: [TESTES_AT020.md](TESTES_AT020.md). Os resultados correntes ficam no [resumo público da V1](VALIDACAO_V1.md); os estados históricos deste roteiro não substituem o recibo de avaliação.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

Com daemon pareado por OAuth próprio e um objetivo de duas etapas, fechar interface T3 e Obsidian mantendo Mac acordado. Confirmar nos registros que a primeira etapa termina e a próxima começa sem mensagem humana. Reiniciar daemon e conferir reconciliação dos mesmos IDs. Não usar bearer token desta sessão como credencial do daemon.

### obsidian · AT-021

Headless ingest/query/lint e plugin na revisão do mesmo vault.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

Com Obsidian fechado, executar ingest, query e lint reais em corpus de teste pelos planos existentes. Manter o canônico e duas projeções versionadas do mesmo projeto no mesmo vault com conteúdos distintos; apontar wikiFolder do plugin 1.28.0 para uma revisão e verificar que links, aliases, sources e source_note resolvem apenas naquela sessão, inclusive após edição pelo plugin. Verificar consulta por autenticação de assinatura e revisão, tradução dos links no import e ausência de referências reviews no canônico. Encerrar edição/comandos do plugin e importar alteração sem conflito. Testar reviewed:true e conflito contra mudança canônica; nenhum arquivo da revisão aberta pode ser atualizado pelo serviço. Registrar versões, hashes e capturas.

### proactive · AT-022

Detecta e inicia melhoria sem nova ordem e sem duplicar objetivo.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

Preparar evidência de melhoria em projeto de teste e ativar ciclo proativo sem enviar objetivo humano. Confirmar objetivo com origem autonomous, evidência, seleção de agente e início de trabalho. Reavaliar a mesma evidência e confirmar ausência de duplicata. Registrar ciclo, objetivo e IDs.

### no_paid_fallback · AT-023

Cota não habilita destino pago nem plano desconhecido.

Execução determinística local:

```sh
uv run --frozen pytest -q tests/test_routing.py::test_quota_never_enables_paid_destination
```

Registrar código de saída e saída do teste; o cenário não comprova integrações externas reais.

### iphone · AT-024

Comandos, estado e interrupção pelo iPhone.

**V1 aprovada por Marco em 19/10/2026, no escopo registrado no [resumo público](VALIDACAO_V1.md).**

Com Mac acordado e T3 pareado no iPhone, enviar objetivo pela thread coordenadora, consultar andamento e solicitar interrupção. Conferir as mesmas identidades e estados no Mac; filhos ainda não parados devem aparecer como cancelamento pendente. Guardar capturas e recibo de interrupção.

## Critério de conclusão

Todos os 24 evals são obrigatórios (16 determinísticos e 8 humanos). A suíte completa inclui regressões adicionais e deve passar além dos nós mínimos. Um humano pendente impede alegar aceitação final; resultados locais aprovados continuam úteis e devem ser registrados. Não gerar recibo PASS manual nem alterar o digest. Encaminhe para `/eval ORACULO` depois de reunir evidências; `/ship` exige recibo de aceitação.
