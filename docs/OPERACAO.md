# Operação do Oráculo

Este guia descreve a implementação do checkout. A instalação e os cenários reais de integração precisam de evidência conforme [VALIDACAO_ORACULO.md](VALIDACAO_ORACULO.md); não estão comprovados apenas pela existência deste documento.

## Configuração e instalação

Use macOS, Python 3.12, `uv`, Git e um T3 acessível por MCP. O LaunchAgent funciona na sessão do usuário enquanto o Mac estiver ligado e acordado. O launcher instalado usa o Python absoluto de uma release verificada em
`~/.local/share/oraculo/releases`, selecionada por `current`. Siga
[Instalação e manutenção](INSTALACAO.md) para construir o bundle, instalar,
ativar e recuperar; o daemon instalado não depende de manter o checkout.

```bash
uv sync --locked --python 3.12
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" init
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" doctor
```

`init` cria a configuração quando ausente e inicializa estado e vault. Leia [config/oraculo.example.toml](../config/oraculo.example.toml). `--config` fica **antes** do subcomando em todos os comandos CLI. Os exemplos seguintes usam o caminho padrão; IDs em maiúsculas são valores a substituir pelo resultado real.

Os padrões são:

| Configuração | Valor |
|---|---|
| Projetos | `~/projects/work` |
| Vault | `~/Obsidian/Oraculo` |
| Estado operacional | `~/Library/Application Support/Oraculo` |
| Executor de trabalho | 1 |
| Consulta ativa / ociosa | 15 / 60 segundos |
| Descoberta de melhorias | 1.800 segundos |
| Gasto adicional | Proibido |

A URL `https://t3.example.invalid/mcp` é um placeholder sem serviço associado; substitua-a pelo endereço MCP da sua instância T3 antes do login. O serviço descobre os projetos cadastrados no T3 cujo caminho real está dentro de `projects_root`; criar somente uma pasta não a cadastra no T3.

Cada bloco `providers` começa com `billing_verified = false` e `models = []`. Confira o catálogo vivo do T3 e suas assinaturas antes de preencher os IDs reais e habilitar o bloco. `billing_verified` é uma declaração de configuração, não uma consulta ao faturamento. Modelos customizados, opções rápidas e APIs não devem ser habilitados se causarem gasto adicional. Os três provedores precisam ser comprovados no aceite; habilitar apenas um não satisfaz esse requisito. Em cota indisponível, o serviço considera alternativas já elegíveis ou aguarda.

Cada operador deve confirmar as assinaturas e desativar extras pagos para o seu ambiente. O arquivo de exemplo começa com todos os provedores desabilitados. O login OAuth real e as integrações devem ser validados na instalação de destino.

Perfis padrão: implementação/testes preferem Codex, arquitetura prefere Claude e pesquisa/descoberta prefere Grok. Memória prefere Codex. São prioridades configuráveis de roteamento, não execução triplicada da tarefa.

## Login e ciclo do serviço

O Oráculo registra seu próprio cliente OAuth, guarda credenciais no Keychain e usa callback local na porta 8769. Pare o serviço antes de login ou atualização do ambiente. Não copie tokens do T3 ou do plugin para arquivos de configuração.

```bash
./scripts/install-service.sh --dry-run
./scripts/install-service.sh --install
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" auth login
./scripts/install-service.sh --start
./scripts/install-service.sh --status
```

Sem argumentos, o script faz somente dry-run de instalação. `--install` sincroniza o lockfile, inicializa dados ausentes e gera `~/Library/LaunchAgents/local.oraculo.plist`; não inicia nem autentica. `--start` executa o diagnóstico local e carrega o LaunchAgent. Esse diagnóstico não comprova login nem cota. O serviço começa a descobrir e executar trabalho quando há provedores elegíveis e conexão disponível.

```bash
./scripts/install-service.sh --stop
./scripts/install-service.sh --uninstall --dry-run
```

`--stop` descarrega o LaunchAgent e seu reinício automático. Não equivale ao cancelamento dos agentes remotos. `--uninstall` remove somente o LaunchAgent, preservando vault, estado, configuração, credenciais, ambiente e logs. Para configuração em outro caminho, use a variável `ORACULO_CONFIG` com caminho absoluto ao instalar/iniciar; mudar esse caminho exige reinstalação com serviço parado.

Para diagnóstico em primeiro plano, com o LaunchAgent parado:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" serve
```

## Objetivos e status

O T3 é a interface de conversa, inclusive no iPhone quando seu acesso remoto estiver configurado. O serviço usa coordenadores T3 e worktrees para execução; a CLI oferece o controle local. Use o ID de projeto do T3 retornado em `status`, não apenas o nome da pasta.

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" status
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" status --project PROJECT_ID
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" submit PROJECT_ID "Corrigir a falha reproduzida no teste de importação" --kind implementation
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" cancel PROJECT_ID OBJECTIVE_ID
```

Tipos padrão: `implementation`, `tests`, `architecture`, `research`, `discovery`, `memory`. Preserve os IDs retornados. O cancelamento é reconciliado com o T3: a solicitação não prova que os descendentes pararam nem desfaz um merge/deploy já realizado. Trabalho de controle não conta como um segundo executor; conversas manuais no T3 não entram no limite do Oráculo. Agentes com acesso completo precisam respeitar o protocolo: ele não é uma sandbox contra comandos arbitrários.

## Memória com Obsidian fechado

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" memory ingest PROJECT_ID --source /caminho/absoluto/documento.md
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" memory query PROJECT_ID --query "Quais decisões continuam válidas?"
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" memory lint PROJECT_ID
```

Esses comandos enfileiram objetivos de memória processados por agentes T3. A resposta inicial não é a resposta final da consulta: acompanhe o objetivo e sua conversa no T3. A fonte deve estar disponível no Mac do serviço.

O serviço publica fontes imutáveis em `projects/<project_key>/raw/` e páginas derivadas em `projects/<project_key>/wiki/`. IDs compatíveis com nomes de páginas mantêm seu nome; IDs T3 com `:`, como `mcp:...`, recebem a chave estável `p-<sha256>`. Os comandos e a proveniência continuam usando o `project_id` original; o bundle informa `project_key` e `corpus_path`. `shared/` contém somente aprendizados generalizados; contexto de cliente permanece no projeto. `index.md`, schema e log pertencem ao publicador. Não edite diretamente a memória canônica com o plugin.

`memory propose` destina-se ao executor autorizado, com bundle estruturado e grant ativo; não é necessário para a revisão cotidiana:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" memory propose PROJECT_ID --file /caminho/bundle.json --attempt-id ATTEMPT_ID --generation 1 --thread-id THREAD_ID --run-id RUN_ID
```

O bundle é copiado para staging privado e identificado por hash. Ingestão não significa armazenar automaticamente conversas completas ou todos os documentos de clientes.

### Contexto grande e leitura autorizada

O despacho de memória elimina cópias duplicadas do bundle e da evidência de execução. Todo novo despacho de memória preserva `payload` e `context` integrais em snapshot JSON canônico privado e envia apenas a referência com SHA-256, inclusive para fontes pequenas e operações query/lint. Assim o coordenador não precisa retranscrever fontes literais. Despachos antigos já persistidos são mantidos para garantir identidade e retry; a atualização não os reescreve. A mensagem final, incluindo as instruções, deve caber em 120000 unidades UTF-16. O snapshot aceita até 64 MiB; conteúdo acima desse limite é rejeitado explicitamente, sem truncamento silencioso.

O snapshot conserva o `base_commit`, os hashes e as fontes do momento da emissão. Não é substituído pela versão atual do vault durante retry ou restart. Seu manifesto permite apenas o projeto autorizado e `shared`; a referência não permite abrir caminhos arbitrários do filesystem. O coordenador encaminha a referência sem expandir novamente seu conteúdo no brief do filho.

Após adquirir o grant, o executor usa a CLI absoluta indicada no despacho, inclusive quando está em outro checkout. Exemplo equivalente na raiz deste checkout:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" memory read PROJECT_ID --artifact ARTIFACT_SHA256 --attempt-id ATTEMPT_ID --generation 1 --thread-id THREAD_ID --run-id RUN_ID --file /caminho/novo/contexto.json
```

O protocolo `memory.read` exige o executor do grant vigente, mesmo projeto/job e artefato efetivamente emitido. Cada página de até 4096 caracteres Unicode revalida identidade, geração, cancelamento e integridade; repetir `request_id` não dispensa essas verificações. A CLI confere o hash integral, recusa sobrescrever arquivo existente e remove download incompleto. Só usar o JSON depois de `ok: true`. Examinar o arquivo em partes não autoriza resumir ou descartar a fonte antes de validar sua integridade.

Para compartilhar um aprendizado, envie a fonte ao projeto de origem:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" memory promote PROJECT_ID --source /caminho/aprendizado.md
```

`promote` enfileira um objetivo de memória com `memory_scope = shared`. O agente deve remover dados de clientes, identificadores e referências privadas antes de propor o aprendizado generalizado. Não é uma cópia direta da fonte privada para `shared/`. A publicação compartilhada exige esse escopo no job e identidade/grant do executor; um job comum não pode habilitá-la apenas mudando o bundle.

O executor dessa tarefa usa uma fonte sanitizada e um bundle cujo `project_id` interno é `shared`, mantendo o projeto de origem no comando:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" memory propose PROJECT_ID --shared-source /caminho/sanitized.md --file /caminho/proposal_project_shared.json --attempt-id ATTEMPT_ID --generation 1 --thread-id THREAD_ID --run-id RUN_ID
```

O bundle deve referenciar o hash da fonte sanitizada. A sanitização semântica é responsabilidade do agente; as verificações estruturais não comprovam por si só a remoção de todos os dados sensíveis.

## Revisão no mesmo vault

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" review open PROJECT_ID
```

Também é possível revisar somente a memória compartilhada, sem cadastrar um projeto T3 artificial chamado `shared`:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" review open shared
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" review close shared --session SESSION_ID
```

Guarde `session_id`. A revisão fica em `~/Obsidian/Oraculo/reviews/<session_id>/wiki/`, com fontes copiadas e mapping de versão. Contém o projeto escolhido e aprendizados compartilhados; o serviço não a atualiza enquanto aberta. Duas revisões são versões independentes, não visualizações sincronizadas.

Nas novas revisões, as fontes em `reviews/<session_id>/raw/` são exibidas como texto literal, com o SHA-256 do conteúdo original. Links, embeds e aliases contidos na fonte não são ativados: eles podem pertencer a uma revisão antiga ou a outro destino. As páginas da wiki continuam apontando para a fonte da própria revisão. O raw canônico conserva os bytes originais; a cópia de exibição também é protegida contra edição no import.

A correção de isolamento não reescreve revisões já abertas. Preserve e encerre o trabalho da sessão antiga, tratando eventuais conflitos, e abra uma nova revisão para receber a projeção corrigida. No plugin, selecione a nova Pasta Wiki pela interface e reinicie o Obsidian conforme a orientação dessa configuração.

Abra o vault `~/Obsidian/Oraculo` no Obsidian. No plugin Karpathy LLM Wiki 1.28.0, selecione manualmente **Wiki Folder** como `reviews/<session_id>/wiki` pela interface suportada. Configure o login Codex OAuth próprio do plugin usando assinatura existente; não copie tokens do T3 nem habilite APIs pagas. O Oráculo não reescreve `.obsidian` durante a execução. A resolução real de links, aliases e fontes deve ser validada no plugin conforme o roteiro de aceite.

Finalize as edições e aguarde os comandos do plugin terminarem antes de fechar:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" review close PROJECT_ID --session SESSION_ID
```

O importador compara base, canônico atual e proposta. Mudança sem conflito é incorporada; divergência preserva as versões e impede publicação parcial do lote. Páginas canônicas `reviewed: true` não recebem alteração automática de conteúdo. Índice e log de revisão não substituem os canônicos. Em conflito, preserve a sessão e consulte os achados retornados; não force cópia sobre o canônico. Alterações feitas depois do fechamento não fazem parte do import já concluído.

## Entrega e protocolo de agentes

O comando `request` envia um envelope JSON v1 pelo socket local, com `request_id`, `kind`, `project_id`, `origin` e `payload`. O mesmo ID identifica a mesma solicitação; um retry não deve inventar outro ID para um efeito incerto. Arquivo ou stdin são aceitos:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" request --file /caminho/request.json
```

Envelope de consulta, sem efeito externo:

```json
{
  "schema_version": 1,
  "request_id": "consulta-status-001",
  "kind": "status.get",
  "project_id": "*",
  "origin": {},
  "payload": {}
}
```

Para `operation.prepare`, o executor precisa de identidade/grant válidos e informa `operation_id`, `attempt_id`, `generation`, `kind`, `expected_state`, `cwd`, `checks_argv` e `verifier_argv`. `cwd` precisa pertencer ao mesmo repositório Git do projeto, inclusive quando é um worktree. `checks_argv` é uma lista de comandos, cada comando uma lista de argumentos; `verifier_argv` também é uma lista de argumentos. Não são strings de shell. Os checks precisam passar antes da autorização de efeito único.

Merge exige commit e destino esperados; deploy exige também o artefato. O verificador é específico do projeto e deve consultar o destino de forma somente leitura, produzir JSON em stdout e retornar sucesso. Sua saída deve conter `commit`, `target`, `healthy` e, para deploy, `artifact`, com valores observados no destino. Não use um comando que apenas repita o estado esperado: isso não comprova a entrega. Não existe comando universal de deploy embutido.

Um replay de `operation.prepare` retorna `effect_authorized_once: false` e `receipt_replay: true`: recuperar o recibo não autoriza repetir o efeito. Em perda da resposta original, consulte e reconcilie o destino antes de agir.

Depois do efeito, `operation.report` identifica `operation_id` e `attempt_id`; o serviço executa o verificador registrado. O agente não pode atestar a entrega apenas enviando `verified: true` ou um recibo escrito por ele. Sem evidência correspondente, a operação permanece incerta e deve ser reconciliada antes de nova tentativa. Os comandos de verificação têm limite de execução e saída; use verificadores curtos e sem efeitos colaterais.

## Falhas, reinício e dados

| Sintoma | Conduta |
|---|---|
| `auth_required` | Pare o serviço, execute `auth login`, reinicie. O daemon não fica abrindo o navegador. |
| `disconnected` | Verifique T3, URL MCP e rede; preserve estado enquanto a conexão se recupera. |
| Nenhum provedor elegível | Confira IDs/modelos, cobertura dos planos e configuração; não habilite API paga como fallback. |
| Cota esgotada | Aguarde ou use outra alternativa já elegível no plano existente. |
| `uncertain` | Inspecione conversa/operação e destino; não reenvie o efeito nem apague o banco para liberar fila. |
| Cancelamento pendente | Aguarde confirmação de parada dos runs; parar o daemon não cancela execução remota. |
| Conflito de revisão | Preserve proposta e base; não sobrescreva a wiki canônica. |
| Socket ausente | Verifique `--status`, logs, caminho de configuração e se `serve` está ativo. |
| `dispatch_identity_unknown` | Despacho antigo sem recibo de run: confira a conversa e o job antes de pedir ao coordenador que retome o mesmo dispatch. Não apague outbox nem invente um recibo. |
| `dispatch_attempts_exhausted` | O coordenador terminou sem adquirir grant em todas as tentativas permitidas; resolva o impedimento mostrado na conversa e retome o mesmo job pelo coordenador. |
| `legacy_dispatch_oversize_requires_reconciliation` | Outbox persistida antes da correção contém mensagem acima do limite. Preserve conteúdo e chave; confira execução remota e reconcilie pelo protocolo antes de nova tentativa. Não reescreva a outbox/SQLite nem presuma que o envio anterior falhou. |

Se a delegação comprovadamente não começou, o coordenador pai pode enviar `execution.report` com `status = not_started`, identidade de origem válida e evidência definitiva de ausência de execução. Timeout ou resposta perdida não são essa prova e não liberam o executor. Se o cancelamento chegar antes do claim, o vínculo do filho pode ser persistido somente para reconciliação, sem autorizar efeitos; o serviço precisa localizá-lo e confirmar a parada.

No reinício, o serviço usa o estado persistido para retomar a reconciliação. Intenção registrada não prova que uma chamada externa ocorreu; ausência de resposta não prova que falhou. Não libere manualmente o executor apenas porque passou tempo.

Despachos novos registram o run retornado pelo T3. Se esse run terminar sem adquirir grant, sem execução ou pedidos pendentes, o serviço pode reenviar a mesma mensagem com nova chave de controle, até `max_dispatch_attempts`. O run pode estar no histórico, desde que sua terminalidade seja comprovada e o estado atual do coordenador também esteja ocioso e terminal. Isso não relaxa a identidade exigida para concluir um executor. Resposta perdida conserva chave e conteúdo originais. Recibos legados sem run identificado ficam bloqueados para recuperação explícita; outros jobs podem avançar. Uma outbox legada acima do limite de transporte conserva a mensagem original, porque mudar conteúdo sob a mesma chave poderia contradizer uma aceitação remota anterior. Essa repetição de controle não autoriza repetir merge/deploy.

Para validar continuidade com interfaces fechadas, siga [TESTES_AT020.md](TESTES_AT020.md). Inicie e observe a tarefa antes de fechar a janela T3, mantenha desktop/backend vivos e registre a transição dentro do intervalo fechado. Encerrar o aplicativo inteiro interrompe os runs e é um cenário diferente de fechar sua janela. O reinício do daemon Oráculo também é distinto do reinício do T3: uma thread retomada pelo T3 pode receber outro `run_id`, que não herda autorização do grant anterior.

Logs ficam em `~/Library/Logs/Oraculo`: `service.jsonl` com rotação, além de `stdout.log` e `stderr.log` do LaunchAgent. Estado SQLite, staging e recibos ficam fora do vault em `~/Library/Application Support/Oraculo`; o socket é local ao usuário. Não publique esse diretório como memória nem copie credenciais para logs.

O Git local do vault versiona o conteúdo publicado e ignora configurações do Obsidian e revisões. Não use `git reset --hard` ou `git add -A` para reparar uma publicação. Git local e cópias no mesmo disco não protegem contra perda do disco. Para backup consistente do banco ativo, use a API de backup do SQLite; uma cópia isolada de `state.sqlite3` pode omitir transações no WAL. Pare o serviço antes de manutenção manual e preserve as revisões ainda não importadas. Nenhum backup remoto é configurado automaticamente.


### Admissão explícita de memória (ORA-CONT-002)

`submit PROJECT TEXT --kind memory` sem contrato de memória é rejeitado antes de
criar objetivo/job. O texto livre não decide silenciosamente entre consulta e
publicação. Use `memory ingest PROJECT --source FILE`, `memory query PROJECT
--query TEXT` ou `memory lint PROJECT`. Em envelope `objective.submit`, declare
`memory_operation: ingest|query|lint`; query exige pergunta não vazia e ingest
exige `source_path` ou `execution_evidence` não vazio. O conteúdo informado deve
ser integral e verificável; apontar para caminhos no texto não os anexa.

`source_path` precisa pertencer à raiz registrada do projeto. Um worktree irmão
não fica automaticamente autorizado como fonte de ingestão: envie uma cópia
identificada dentro da raiz, ou um envelope estruturado com `execution_evidence`
contendo fontes integrais, caminhos, commits e hashes. Não amplie o escopo para
contornar essa regra. A validação é aplicada na CLI e na API de admissão; jobs
legados já persistidos não são reescritos.

## Planos executáveis (ORA-CONT-003)

Para uma entrega com várias etapas, envie um JSON de plano usando:

```sh
oraculo plan PROJETO_T3 --file plano.json --key intencao-estavel
oraculo status --project PROJETO_T3
oraculo retry PROJETO_T3 OBJETIVO_ID TAREFA_ID
oraculo cancel PROJETO_T3 OBJETIVO_ID
```

O arquivo contém `text`, `max_followups` (0–4) e `tasks` (1–32). Cada tarefa tem
`id`, `kind` (perfil habilitado), `stage`, `owner`, `brief`, `sources`,
`write_paths`, `depends_on` (IDs do mesmo plano) e `criteria` com `id`/`description`.
Exemplo exercitável: [plano de exemplo](tasks/ORA-CONT-003-example-plan.json).
Submissão é atômica; repetir a chave com o mesmo conteúdo recupera o plano,
conteúdo diferente é rejeitado. Ciclos, dependências desconhecidas e IDs repetidos
são rejeitados antes de enfileirar. A CLI `submit` legada continua disponível.

O serviço despacha dependências prontas após reconciliar execução e efeitos;
não exige “continue”. Cada tarefa usa um filho T3 próprio e preserva
`taskId`/`threadId`/`runId`, com link no status. Revisão adicional é outra tarefa
e outra delegação. Planos usam checkout compartilhado com propriedade declarada;
contexto T3 separado não exige worktree separado. Worktrees legados continuam
partindo do commit do pai e não recebem alterações não commitadas.

`max_executors=1` continua sendo o padrão. Valores 2–4 permitem concorrência
limitada apenas entre escopos independentes e destinos elegíveis na assinatura.
`write_paths` são prefixos relativos: ausência ou `['*']` bloqueia todo o projeto,
lista vazia é somente leitura. Declare artefatos de testes e todas as escritas;
commits/índice Git compartilhado exigem `'*'`. Aliases de roots, symlinks,
maiúsculas/minúsculas e normalização Unicode recebem exclusão conservadora;
hardlinks explicitamente declarados usam identidade de arquivo. Não são enumerados
hardlinks internos a árvores. Os agentes confiáveis precisam respeitar o escopo:
leases não constituem sandbox. Memória mantém exclusão global do vault.

`status.plans` informa tarefas prontas/ativas, responsável, dependências,
critérios, provas, tentativas, links e bloqueios com motivo/ação. Os estágios
implementation, validation, review, research, memory, publication, deployment e
acceptance são independentes. `not_requested` não comprova que publicação/deploy
ou aceite são dispensáveis. `delivery_contract_satisfied` comprova apenas os
critérios declarados; um checkpoint de memória não encerra trabalho pendente.

Todo relatório planejado de sucesso exige `evidence.criteria` com recibo único
por critério: `id`, `status:'passed'` e `reference` verificável. Publicação/deploy
exigem operação confirmada e o estágio correspondente autoriza o canal de
operações. Falha pode receber até `max_correction_rounds` novas tentativas;
`evidence.blocker` interrompe correção automática. Estado remoto incerto conserva
o grant e impede duplicidade. Cancelamento impede sucessores e só termina depois
de reconciliar todos os executores/efeitos. `task.retry` não contorna leases,
operações incertas ou limites.

`plan.extend` só é aceito de executor ativo, depende da tarefa de origem, mantém
escopo contido e orçamento persistido. Não cria publicação, deploy, memória ou
aceite automaticamente. Executor planejado não pode abrir objetivo/plano avulso
para fugir desse limite.

Use fontes por referência e recibos concisos. Um despacho novo que excede o
limite T3 fica `blocked` com `task_dispatch_too_large`, sem envio/grant; cancele e
reformule o plano. Uma outbox antiga incerta conserva os bytes/ID originais e exige
reconciliação. Memória preserva o transporte integral por artefato da ORA-CONT-002.

## Ciclo completo ligado à conversa de origem

O fluxo `oraculo task` resolve o projeto, prepara memória e isolamento e mantém
o vínculo com a origem até PR, checks, merge e encerramento. Consulte
[tarefas geridas](MANAGED_TASKS.md) para o contrato de entrada, o bridge T3,
comandos, política de publicação e limites de cobertura. O adapter é cooperativo;
instalar o pacote não intercepta automaticamente todas as conversas do host.

Jobs geridos recebem consulta versionada antes da admissão e gravação terminal
com recibo canônico. O estado da memória é separado do resultado da execução;
falha de gravação não deve ser apresentada como entrega encerrada. Conversas
diretas usam o contrato de `memory-cycle`, também por adoção explícita do adapter.

`oraculo capacity` mostra a capacidade efetiva. Alterações operacionais e limites
por plano não ultrapassam `max_executors`; reduzir a capacidade preserva trabalhos
já admitidos. Escritas conflitantes continuam serializadas.

O [encerramento verificado](ENCERRAMENTO.md) distingue PR aberto, merge observado,
memória confirmada e conversa fechada. Merge não comprova implantação.

## Instalação, ativação e rollback

Código validado e release instalada são evidências distintas da versão ativa.
`runtime install`/`upgrade` preparam arquivos; `runtime activate` seleciona a
release. Uma primeira seleção não inicia o serviço. Use `service start` quando
configuração e credenciais estiverem prontas. Operações sobre serviço já ativo
exigem quiescência e fecham admissão durante a troca.

```sh
oraculo status
oraculo capacity
oraculo runtime status
oraculo service status
oraculo runtime upgrade --bundle /caminho/bundle --dry-run
oraculo runtime upgrade --bundle /caminho/bundle
oraculo runtime activate RELEASE --dry-run
oraculo runtime activate RELEASE
oraculo service status
oraculo capacity
```

`status.runtime` identifica PID, release, caminho/hash da configuração carregada
e protocolo. Alterar o TOML não muda a configuração já carregada: divergência
com o arquivo bloqueia confirmação de identidade. `service status` exige PID e
socket correspondentes; loaded não equivale a online. `doctor` aceita `--root`,
`--bin-dir`, `--label`, `--plist-dir`, `--provider` e `--skill-root` para identificar
os alvos de ensaios isolados. A configuração é sempre opção global `--config`.

`maintenance.begin` fecha admissão e devolve operation_id/token somente após
provar quiescência. Trabalho ativo/incerto, operação de memória ou sessão gerida
em andamento bloqueia a manutenção. `maintenance.end` exige a mesma operação e
token. O caminho offline oficial adquire o lock do daemon e do ledger; nunca
abra outro escritor sobre um serviço vivo. `capacity --set 1` reduz concorrência,
mas não substitui manutenção nem fecha toda a admissão.

Sem socket disponível, `task list/status/history` abre o ledger existente em
modo somente leitura e identifica `source=local_ledger`. Essa consulta não cria
banco nem executa migrações de schema, mesmo que outro processo detenha a escrita.
Ledger ausente, incompatível ou ilegível produz erro explícito.

A parada aguarda remoção de launchd, término do PID anterior e desaparecimento
do socket com prazo finito. Timeout exige recuperação; não use sleep fixo como
prova, nem apague socket/recibos para forçar bootstrap. Journals de runtime e
serviço ficam privados no root de instalação. Preserve-os após falha.

Para rollback, use `runtime rollback --dry-run` e `runtime rollback` conforme o
procedimento de recuperação em [INSTALACAO.md](INSTALACAO.md). O código anterior
precisa aceitar o schema atual. Não restaure SQLite antigo sobre execuções ou
efeitos posteriores; capacidade, IDs e recibos devem permanecer preservados.
Instalações legadas sem manifesto precisam da migração explícita documentada;
não ganham integridade verificável só por receber um manifesto novo.

Admissão compartilhada cobre scheduler e tarefas que entram pelo adapter. A
capacidade do daemon não prova contagem de filhos lançados diretamente no host.
Sem recibos dessa contagem, conserve execução serial e trate a expansão paralela
como bloqueada; não aumente a capacidade nem alegue enforcement global. Skill e
templates são cooperativos, e suporte por provedor depende da matriz de provas
em [SKILLS.md](SKILLS.md).
