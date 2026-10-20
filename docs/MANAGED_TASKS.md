# Tarefas geridas por projeto

O adapter `oraculo task` mantém o pedido ligado à conversa de origem, mesmo quando
a execução ocorre em outro contexto. A origem recebe título, projeto, estado,
link direto `t3-thread://v1/<identidade retornada pelo host>`, branch/worktree,
bloqueios e recibo do PR. `task list` e `task status` consultam o ledger local sem
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
run cancelado, perguntas ou continuações pendentes impedem publicação. Um run
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

A intenção de emissão é gravada imediatamente antes de `gh pr create`. Falha em
precheck permite correção/retry. Depois de emissão incerta, o modo
`reconcile_only` nunca cria outro PR. Um PR ainda invisível exige observação ou
resolução explícita; não autoriza retry cego. O recibo é reconsultado mesmo após
falha de linking/memória, e vinculado tanto ao executor quanto à origem no T3.
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

`merge` exige política, memória anterior confirmada, trabalho/descendentes sem
pendências e HEAD/base/repo exatos. Usa `gh pr merge --match-head-commit` e método
permitido, sem bypass, auto-merge ou remoção de branch. Checks antigos não valem
para um HEAD novo; uma revisão nova precisa validar/publicar a nova versão.
Conflitos, revisão pendente, checks falhos/pendentes ou efeitos incertos bloqueiam.
Após intenção emitida, retries são apenas observação. Somente estado `merged` com
SHA de merge confirmado muda a tarefa para `merged`.

Um ciclo canônico adicional salva o resultado final e o SHA de merge. Falha dessa
memória mantém `merged=true`, `settled=false` e pode ser retomada por `settle`.
O encerramento reaproveita os gates 004: thread/run/projeto originais, estado
terminal, perguntas/filhos ausentes e revisão do thread compatível. A chamada de
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
