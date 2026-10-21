# Tarefas geridas por projeto

O adapter `oraculo task` mantém o pedido ligado à conversa de origem, mesmo quando
a execução ocorre em outro contexto. A origem recebe título, projeto, estado,
link direto `t3-thread://v1/<identidade retornada pelo host>`, branch/worktree,
bloqueios e recibo do PR. `task list` e `task status` consultam o serviço ou o ledger offline sem
exigir IDs de ferramentas T3, credenciais ou uma sessão de modelo ativa.

## Contrato de transporte

A escolha é automática:

- **Filho nativo**: projeto resolvido coincide com o da origem, cwd corresponde
  ao workspace realmente vinculado, base é o HEAD local e capabilities anuncia
  filhos. Usa `delegate_task(isolation="worktree")`, contexto novo e chave
  idempotente persistida. T3 notifica o pai quando o resultado está disponível;
  turnos com revisores/filhos pendentes não são trabalho concluído.
- **Conversa gerida**: projeto diferente, base diferente, ausência de capacidade
  de filhos ou pedido `independent=true`. Usa `t3_thread_launch` com projeto e
  estratégia de worktree explícitos. É uma conversa top-level, não um filho
  nativo. O vínculo de negócio com a origem está no ledger do Oráculo.

Nenhum modo cria um executor no projeto errado para obter um callback. A UI do
host pode omitir filhos da lista comum de Threads; o link direto e o status na
origem permitem abrir o detalhe. Este pacote não modifica a UI do T3.

## Entrada operacional e cobertura

```sh
oraculo --config /path/to/config.toml task start --file request.json
oraculo --config /path/to/config.toml task list
oraculo --config /path/to/config.toml task status --file task.json
oraculo --config /path/to/config.toml task resume --file task.json
```

`request.json` (diretórios, nomes e identidades abaixo são exemplos):

```json
{
  "kind": "work",
  "key": "stable-user-message-key",
  "title": "Adicionar exportação",
  "text": "Implementar a exportação, validar, registrar memória e abrir PR.",
  "cwd": "/workspace/projects/example-project",
  "project": "example-project",
  "base": "HEAD"
}
```

`task.json` contém `{"task_id":"<id devolvido por start/list>"}`. `project` é
opcional: cwd/worktree resolve a identidade canônica. Intenção explícita pode
escolher outro projeto autorizado; título T3 ou basename de remote nunca concede
escopo. Os aliases são IDs, nomes de diretório sem ambiguidade e caminhos reais.
Symlinks e worktrees externas são canonicalizados por metadados Git. A allowlist
é revalidada nos retries. Não se lê o conteúdo de projetos vizinhos.

Os comandos que executam trabalho precisam de um **bridge vinculado à conversa
chamadora**. O transporte padrão usa `T3_ACP_MCP_NODE` e o subcomando suportado
`acp-mcp-call`. Se o harness não expõe esse transporte, pode fornecer
`--transport-command bridge-argv.json`: um array JSON com executável/argumentos.
O executável recebe `tool_name` e `arguments_json` como argumentos finais e retorna
um objeto JSON/MCP. Ele deve chamar as ferramentas autenticadas do host no escopo
real da conversa; não pode substituir a identidade por um ID informado pelo
usuário. O adapter compara `orchestrator_capabilities.parentThreadId` com leitura
do thread/run ativo. Um cliente HTTP de daemon sem identidade chamadora não pode
se passar pelo pai.

Sem bridge, `start` bloqueia explicitamente; `list/status` continuam disponíveis.
Não existe hook global T3 de admissão nesta entrega. O prompt
`prompts/managed-origin.md` fornece o contrato cooperativo para conversas novas
ou existentes; ele não instala interceptação global. É necessário integrar esse
ponto de entrada no host/adapters e ativar a versão do serviço que contém os
ciclos de memória e esta reconciliação. Não há edição automática de configurações
globais, instalação ou reinício de serviço nesta implementação.

Perguntas, status, mensagens de controle e tarefas de memória não abrem tarefas:
`start` aceita apenas `kind="work"`. A classificação semântica continua sendo
responsabilidade do adapter da conversa. `continue` recebe `task_id`, `key` e
`text`, encaminhando a mensagem ao executor existente; não cria outra tarefa.
Novas revisões de trabalho já terminal exigem um pedido novo explícito.

## Continuidade, validação e memória

O ledger privado `managed-tasks.sqlite3` guarda origem/run original, pedido,
projeto, intenção, recibo de lançamento, workspace, relatório e outbox.
Transições incluem `received`, `project_resolved`, `memory_read`, `launch_pending`,
`workspace_ready`, `executing`, `validating`, `memory_pending`, `pr_pending`,
`pr_open` e `blocked`. Cada evento contém prova; bloqueio não apaga o estágio e
as identidades anteriores. `pr_open` não significa merge ou settlement.

O ciclo 006 consulta **antes** do lançamento. Conteúdo indisponível bloqueia; uma
consulta vazia confirmada permite prosseguir. O contexto integral fica em um
artefato privado de hash SHA-256, fora do repositório, evitando truncamento em
prompts. O executor recebe referência, digest e instrução de leitura integral.
O recibo atesta transporte da memória, não prova cognitiva de que o modelo a usou.
O contexto contém somente o corpus do projeto e shared já sanitizado pelo
publicador canônico. Nenhum método deste adapter escreve diretamente no vault.

O executor envia progresso com `task progress` (`task_id`, `message`) e um
relatório com `task finish`:

```json
{
  "task_id": "<id>",
  "report": {
    "summary": "Exportação implementada",
    "checks": [{"command": "test command", "status": "passed"}],
    "commit": "<SHA validado>",
    "decisions": [],
    "limitations": []
  }
}
```

O próprio executor só submete esse relatório: não pode certificar o término do
seu turno ainda ativo. No modo nativo, a notificação T3 acorda a origem, cujo
adapter chama `resume`. No top-level, o daemon reconcilia o run original e os
filhos e finaliza o relatório submetido. Ausência de relatório, validação falha,
run cancelado, perguntas ou continuações pendentes impedem publicação. Antes de
certificar sucesso e salvar a memória de conclusão, o adapter verifica o run
original, o estado atual e os descendentes. Falha, cancelamento ou interrupção
de um descendente impede essa certificação; resultados falhos do próprio executor
continuam podendo ser registrados como falha ou parada. Um run
posterior nunca substitui sucesso do run original. `finish` confirma SHA do
checkout limpo, mantém trabalho separado da gravação e só libera PR após recibo
canônico `confirmed`/commit. Falhas de memória ficam visíveis e são retomáveis.

O daemon usa thread de trabalho e conexão SQLite própria para Git/publicação;
chamadas T3 são encaminhadas ao loop de transporte. Não adiciona polling de modelo.
A reconciliação determinística aproveita o tick existente. A outbox usa IDs de
envio estáveis; perder a resposta não duplica um aviso no host que honra essa chave.

## Publicação e base pública separada

`task publish` recebe `task_id` e `publication` com `cwd`, `publication_root`,
`remote`, `repository`, `base`, `branch`, `head_sha`, `title`, `body`,
`source_project_id` e `source_commit`. Os dois últimos devem corresponder ao
projeto e ao commit validado. `publication_root` é um checkout explicitamente
autorizado em `projects_root`; `cwd` pode ser sua worktree externa. Isso permite
um checkout privado sem remote e outro checkout público com história segura.
O vínculo é explícito; o adapter não copia alterações nem declara que revisou o
delta. A preparação/revisão do delta e o push autorizado devem preceder o PR.

O adapter GitHub verifica remote real, identidade do host, SHA da branch remota,
base e checkout limpo. Não faz push, fork automático, merge ou settlement. Consulta
PRs por repo/head/base antes de criar e novamente depois, inclusive fechados.
Outros hosts retornam bloqueio `publication_host_unsupported`; Azure não é uma
integração implementada por este módulo.

Antes de uma nova emissão, a execução é verificada novamente: sucesso salvo
anteriormente não basta se surgiu trabalho pendente ou falho. A intenção de
emissão é gravada imediatamente antes de `gh pr create`. Falha em
precheck permite correção/retry. Depois de emissão incerta, o modo
`reconcile_only` nunca cria outro PR. Um PR ainda invisível exige observação ou
resolução explícita; não autoriza retry cego. O recibo é reconsultado mesmo após
falha de linking/memória. Após uma emissão incerta, a consulta pode preservar
o recibo de um PR existente mesmo se a execução mudou, sem emitir novamente;
a tarefa permanece bloqueada e não certifica publicação pronta com prova antiga.
Com execução válida, o PR é vinculado tanto ao executor quanto à origem no T3.
Uma atualização idempotente da memória registra URL/SHA/base/estado em outro ciclo
imutável, preservando o relatório e recibo anteriores.

`t3_thread_launch` não possui idempotency key. Sua intenção é persistida antes da
chamada; depois de timeout/restart, o adapter/daemon só consulta list/read e
verifica marcador único, projeto, branch, worktree e base. Zero ou múltiplos
candidatos bloqueiam sem abrir outra conversa. `delegate_task` reutiliza a chave
original, após revalidar escopo e binding/base da origem.

## Validação e limites de implantação

Testes usam Git/worktrees, SQLite, socket e publicador de memória reais em
ambientes temporários. Transporte T3 e GitHub é simulado nos testes automatizados;
isso não comprova uma chamada externa, merge, deploy ou ativação. Ensaios reais
precisam de recibos privados separados e devem dizer qual camada foi real.

O serviço operacional anterior pode não reconhecer `memory.cycle.*`. Uma rejeição
de contrato é lacuna de implantação, não leitura/gravação confirmada. Nenhum
histórico privado, recibo pessoal, dump de ferramenta, ID real de conversa ou
conteúdo de cliente deve entrar no pacote público. Conversas históricas podem
ser referenciadas como fontes; não são reparentadas nem reexecutadas.

## Checks, revisão, merge e encerramento

O orquestrador mantém responsabilidade depois do PR aberto. Os comandos
`task pr-check`, `task merge` e `task settle` estendem o ciclo. A primeira operação
registra `policy` proveniente da conversa de origem, com autorização explícita,
fonte da política do projeto, repo/base/HEAD, checks obrigatórios, exigência de
revisão e método de merge. Uma lista vazia de checks exige `allow_no_checks=true`
explícito. O adapter não infere ausência de proteção quando não recebeu regras.
A descoberta/revisão das regras remotas cabe ao adapter da origem; a integração
GitHub mantém enforcement do servidor e nunca usa `--admin`.

`pr-check` observa o HEAD atual, checks e revisão. Se aguardando, registra
`watch_pull_request` na origem e devolve `pr_waiting`; o host notifica mudanças.
Não há loop de modelo nem poller próprio de PR. Ao evento, o orquestrador repete
`merge`, que revalida todas as provas; um evento de watch não é autorização de
merge. Antes da emissão também fica registrado o watch para recuperar uma
resposta perdida. Se watch não estiver disponível ou a origem for um subagente
incapaz de observar PR, o bloqueio é explícito: a operação pertence à origem
raiz. `task unwatch` encerra o acompanhamento quando houver devolução ao usuário.

`merge` exige política, memória anterior confirmada, execução original concluída
com sucesso, estado atual compatível, descendentes verificados e HEAD/base/repo
exatos. Uma execução posterior falha, cancelada ou interrompida, ou um descendente
nesses estados, bloqueia antes da emissão do merge. Usa `gh pr merge --match-head-commit` e método
permitido, sem bypass, auto-merge ou remoção de branch. Checks antigos não valem
para um HEAD novo; uma revisão nova precisa validar/publicar a nova versão.
Conflitos, revisão pendente, checks falhos/pendentes ou efeitos incertos bloqueiam.
Após intenção emitida, retries são apenas observação. Somente estado `merged` com
SHA de merge confirmado muda a tarefa para `merged`.

Um ciclo canônico adicional salva o resultado final e o SHA de merge. Falha dessa
memória mantém `merged=true`, `settled=false` e pode ser retomada por `settle`.
O encerramento reaproveita os gates 004: thread/run/projeto originais, estado
terminal, perguntas/filhos ausentes e revisão do thread compatível. Também confere
o conjunto completo de PRs vinculados ao executor: deve corresponder ao PR
registrado, com merge confirmado e base esperada. PR adicional, desconhecido ou
ainda aberto bloqueia o encerramento, inclusive ao recuperar uma resposta perdida.
A chamada de
settlement é intenção, não prova: o estado precisa ser relido. ACK perdido nunca
dispara segundo settlement; observa-se o resultado. A origem permanente não é
settled; somente a conversa de execução. A outbox devolve conclusão à origem e o
watch é removido. Merge não comprova deploy; o recibo registra implantação como
não observada.

### Bridge nativo sem executável ACP

Em harnesses que expõem ferramentas T3 nativas mas não `T3_ACP_MCP_NODE`, existe
entrada cooperativa incluída no pacote:

```sh
oraculo --config /path/to/config.toml task start --file request.json --stdio-host
```

O agente da origem executa esse comando em sessão interativa. Para cada linha
`ORACULO_HOST_CALL_V1`, chama a ferramenta T3 nativa indicada com os argumentos
exatos, depois escreve em stdin uma linha JSON
`{"request_id":"<recebido>","result":<resultado real da ferramenta>}`. O processo
continua e entrega o estado final. Replays/erros usam as intenções do mesmo ledger;
nenhum resultado pode ser inventado para avançar. Correlação incorreta ou falta de
resposta bloqueia. Não é polling de tarefas: são as chamadas e respostas sequenciais
do adapter. O limite de confiança é o agente/usuário local que já pode operar o
socket; isso não equivale a atestado criptográfico do host.

Esse bridge evita depender de um helper externo inexistente, mas continua
cooperativo. Sua presença não instala um hook em todas as conversas. O prompt de
origem deve encaminhar pedidos e eventos a essa entrada quando o transporte ACP
não estiver disponível.

## Protocolo de serviço e compatibilidade

As mutações `task` agora negociam `managed.capabilities` versão 1 e são executadas
pelo daemon. O CLI conduz somente argumentos, apresentação e chamadas ao host.
O socket emite `ORACULO_SERVICE_HOST_CALL_V1` com operação e request_id; o CLI
executa a ferramenta no escopo real da conversa e devolve o ACK correlacionado.
Não há credencial nem identidade inventada no Markdown. A confiança continua no
operador local; não se trata de atestado criptográfico contra esse operador.

O daemon conserva o ledger e adquire seu lock exclusivo. Um adapter antigo não
pode mutar esse ledger simultaneamente. Um daemon anterior não reconhece o
protocolo: o CLI novo retorna `managed_service_upgrade_required`, sem tentar a
mesma mutação localmente. Ausência do socket bloqueia mutações. `list`, `status`
e `history` podem consultar o ledger offline e identificam `source=local_ledger`;
essa resposta não comprova serviço online. A migração é aditiva (State schema 4),
e rollback exige runtime cujo intervalo de schemas inclua a versão gravada.

## Planejamento e aprovação vinculada

O pedido de planejamento fixa `deliverable_kind=plan`; finish não pode reclassificá-lo.
O relatório de plano usa `deliverable_kind=plan`, `approval_state=pending` e o
commit do documento. Após término real e memória confirmada, o estado passa a
`awaiting_approval`. `publish`, merge e settlement não aceitam essa entrega.
A aprovação é registrada pela origem, com referência verificável ao pedido:

```json
{"task_id":"<plan-task>","commit":"<plan-commit>","evidence":"Referência à aprovação explícita e seu escopo"}
```

Envie esse payload a `task approve`. O estado passa a `plan_approved`; o relatório
original permanece imutável. Uma execução autorizada usa uma nova chave e
`approved_plan={"task_id":"<plan-task>","commit":"<plan-commit>"}` em `task start`.
O serviço confere origem, commit e aprovação. O adapter não consegue provar a
intenção humana independentemente do operador; não infere aprovação por sucesso.

## Recibos, histórico e recuperação

Notificações v2 contêm sequência por tarefa, event_id, occurred_at e revisão do
snapshot. A primeira tentativa de entrega congela delivered_at e snapshot junto
à chave de envio; retry reenvia bytes idênticos, inclusive após ACK perdido.
O consumidor conserva a maior revisão, marca eventos anteriores como históricos
e consulta status antes de apresentar estado atual. Novos eventos carregam
`refresh_required=true`: o snapshot é uma observação na primeira tentativa,
não uma garantia de frescor no recebimento. Essa regra também vale para entregas
legadas já congeladas com false. `replay_semantics=immutable_delivery_may_repeat`
explicita que os mesmos bytes podem chegar novamente; `replayed=false` descreve
a primeira emissão, não certifica entrega única. Uma notificação antiga não prova
um bloqueio atual. Eventos e entregas não são apagados.

Status inclui os últimos 100 eventos e `history_truncated/event_count`.
`task history` recebe `task_id` e `offset` (inicial 0), devolve até 50 eventos e
`next_offset/total`. Outbox pendente é mostrada separadamente do resultado.

O writer stdio aceita writes parciais e EAGAIN, espera disponibilidade com prazo
finito e restaura flags. O leitor evita `connect_read_pipe` e preserva os flags do
TTY. Tanto frames de host quanto JSON final usam escrita exata. Limites: saída
stdio 16 MiB, ACK stdio 4 MiB, envelope/socket 1 MiB. Fragmentos não são chamadas.
Falha após emissão deixa resultado incerto: retome a mesma tarefa/chave/intenção;
não lance outro filho porque a impressão final falhou. Até migrar uma instalação
antiga, stdout em arquivo privado e stdin em PTY continua um workaround observado.

## Admissão e manutenção

Reservas persistentes de tarefas geridas e grants do scheduler compartilham o
mesmo State e limite de provedor. A reserva precede lançamento, sobrevive a crash
e resposta perdida e só é liberada por término/descendentes confirmados. Estado
incerto mantém ocupação. `executor_slots` pode reservar um orçamento maior para
uma tarefa; não aumenta o teto configurado. Consultas não reservam executor.

Cobertura é cooperativa: filhos/revisores adicionais precisam ingressar pelo
adapter. Chamadas diretas de delegação no T3 não são interceptadas. Portanto o
valor `capacity.occupied` não é inventário global de todas as conversas T3. Sem
admissão cruzada comprovada, o operador deve manter execução conservadora; nenhum
hook ou alteração do host é instalado por este pacote.

`maintenance.begin/status/end` são operações locais pelo socket, com operation_id
e token. Begin exige quiescência e fecha admissão persistentemente; end confere
ambos e reabre somente com estado ainda quiescente. Sem daemon, a interface oficial
`oraculo.maintenance.maintenance_guard` toma os locks de daemon/ledger, valida o
estado e mantém o bloqueio após sair do contexto. `end_maintenance` o encerra
explicitamente. Desconhecido, processo ativo e schema futuro bloqueiam a troca.

A reconciliação em segundo plano usa a mesma autoridade do serviço, serializada
com sessões caller-bound. Ela finaliza threads independentes e reenvia retornos
persistidos. Filhos nativos continuam exigindo `task_status` no contexto real da
origem após o callback; o daemon não se passa pelo pai. Falhas/cancelamentos
terminais com descendentes parados liberam a reserva e salvam o resultado de
falha. Isso não satisfaz os gates de sucesso, publicação ou settlement.

Ao abrir um ledger legado, o serviço identifica execuções ainda não terminais
sem reserva correspondente. `capacity.available` fica zero e lista
`untracked_managed_tasks`; scheduler e novos pedidos ficam bloqueados por
`managed_legacy_execution_requires_reconciliation`. Não se inventa ocupação
observada. A origem deve reconciliar esses IDs com prova terminal real; só então
a admissão reabre. Reiniciar não apaga esse bloqueio.

Respostas do serviço mostram `admission` consultada na tabela de reservas atual;
`admission_at_launch` conserva o recibo histórico da admissão. `not_reserved`
significa ausência de reserva atual, não sucesso do executor. No ledger offline,
a observação armazenada não substitui uma consulta de capacidade ao serviço.

## Parecer negativo e recuperação anterior à migração

`completed` no host significa que o agente terminou; não significa aprovação.
Um relatório com checks válidos `failed`, `verdict: "FAIL"` ou resumo iniciado
por `FAIL` registra `work_status=failed`. O ciclo de memória conserva a falha e
seu recibo confirmado; a resposta permanece `blocked/work_not_successful` e não
permite publicação, merge ou settlement. Sucesso exige todos os checks `passed`,
execução original e atual válidas, descendentes concluídos e commit/worktree
validados. Checks precisam ser uma lista não vazia de objetos com `command`
textual não vazio e `status` exatamente `passed` ou `failed`; os campos opcionais
são `result`, `proof` e `layer`. Metadados de revisão existentes são aceitos:
`reviewed_public_head`, `public_base`, `pull_request`, `task_id`,
`linked_implementation_task`, `origin`, `memory`, `findings`, `prior_findings`
e `documentation_observations`. Esses campos preservam proveniência no relatório;
não concedem aprovação nem ampliam o schema da memória. Outros campos desconhecidos
no relatório/check ou verdict fora de `PASS`/`FAIL` bloqueiam. O relatório submetido
e o ciclo são imutáveis.

Uma instalação anterior pode ter um relatório negativo submetido, memória
`failed/saved` já confirmada pelo serviço e ledger ainda `pending`, impedindo
`migrate-legacy`. Para esse caso existe uma operação estreita, executada pela
origem com o **código candidato revisado**, antes de parar o serviço antigo:

```sh
# arquivo privado de entrada, contendo somente o ID original
printf '%s\n' '{"task_id":"task-ID-ORIGINAL"}' > /caminho/privado/reconcile.json
/caminho/candidato/venv/bin/python -m oraculo --config /caminho/config.toml \
  task reconcile-legacy-failure --file /caminho/privado/reconcile.json \
  --stdio-host > /caminho/privado/reconcile.out
```

O agente da origem atende cada frame `ORACULO_HOST_CALL_V1` com a ferramenta
nativa correspondente e devolve seu resultado real pelo ACK correlacionado em
stdin. `--transport-command` também aceita a ponte caller-bound já configurada.
Não executar a ponte com identidades inventadas. O comando usa o thread atual da
origem e a identidade original do ciclo; não exige que o run atual seja o antigo.

A operação exige serviço legado vivo, lock do daemon ocupado, rejeição específica
do protocolo gerido novo e resposta válida de `memory.cycle.status`. Exige banco
principal schema 3 e ledger gerido schema 2, ambos existentes. Socket indisponível,
timeout, resposta ambígua, schema diferente ou daemon novo bloqueiam. O lock
`managed-tasks.lock` permanece exclusivo durante as verificações e atualização;
não há abertura por `State`, migração de schema, novo ciclo ou escrita no vault.
As operações geridas normais continuam exigindo `managed_service_upgrade_required`
contra serviço antigo; não há adapter offline geral.

Somente a origem/projeto originais podem reconciliar. O ciclo precisa corresponder
à chave derivada da tarefa e identidade original, estar `failed/saved` e conter
recibo `confirmed` correlacionado. O digest do relatório canônico do serviço é
comparado à reconstrução com o relatório submetido e os metadados consultados.
Não se aceitam arquivo de recibo, novo relatório, status escolhido pelo operador,
resultado positivo ou tarefa com publicação tentada. Como o serviço antigo não
preservava `verdict`, o conteúdo canônico retido deve comprovar a reprovação por
check `failed` ou resumo iniciado por `FAIL`. Um `verdict: "FAIL"` isolado não
autoriza recuperação; relatório e metadados originais permanecem intactos.
Se a memória não estiver confirmada, a origem precisa resolver o ciclo original
pela interface legada antes; este comando não o reescreve.

O binding/worktree, run original, estado atual e descendentes são consultados
novamente imediatamente antes da atualização condicional. A cobertura de filhos
é a visibilidade do projeto no host, sem lock atômico remoto contra novas entradas;
manter a janela cooperativa sem novas execuções. A gravação altera somente a tarefa
e acrescenta seu evento/outbox. Relatórios, eventos anteriores, memória e schemas
são preservados. O JSON final `reconciliation: "confirmed"` indica registro da
falha; **exit 1 e `work_not_successful` são esperados**, não aprovação do candidato.
`pending_reconciliation_returns` indica ACK ainda pendente. Repetir exatamente o
mesmo comando revalida as fontes e reenvia o mesmo evento sem duplicá-lo. Após
schema 4, esse comando deixa de ser aplicável. Só então continuar o procedimento
de quiescência e migração; reconciliar uma tarefa não certifica quiescência global.
