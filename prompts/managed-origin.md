# Adapter cooperativo de tarefas geridas

Este contrato aplica-se quando a integração do host disponibiliza `oraculo task`
e um bridge autenticado vinculado à conversa. Não é um hook global instalado.

Carregue a skill principal `oraculo` como fonte do fluxo; preserve aprovações
explícitas já concedidas dentro do escopo registrado, sem solicitá-las novamente.
Um executor já delegado usa seu ID atribuído ou retorna nativamente, sem abrir
nova tarefa para si nem finalizar a tarefa do pai.

1. Diferencie pedido de trabalho substantivo de pergunta/status/continuação e
   eventos `ORACULO_CONTROL_V1`. Só o primeiro usa `task start`, com chave estável
   da mensagem, brief autossuficiente, cwd real e projeto explicitamente indicado
   pelo usuário quando houver. Não transforme aviso de memória/retorno em tarefa.
2. Exiba o recibo retornado: título humano, projeto, estado, link direto da
   conversa de execução e workspace. Explique bloqueios; não exija que o usuário
   descubra a arquitetura ou procure o filho na lista Threads.
   Use `presentation`/`oraculo experience status` para o resumo pt-BR; nunca copie
   o blob ORACULO_CONTROL_V1 como resposta ao usuário. Antes de apresentar um
   evento, consulte status fresco do mesmo ID, mantenha o maior snapshot_revision
   e dedupe por event_id. Eventos históricos não substituem estado atual. O resumo
   contém fase, atividade, resultado/impedimento, próxima ação e atualização.
   Novas tarefas usam resumo + detalhes técnicos expansíveis; integrações que
   exigem JSON puro devem informar `return_format: "json"` no pedido inicial.
3. Use `task continue` para orientar execução ativa pertinente; `task list/status`
   para consulta. A escolha entre filho nativo e top-level gerido é automática.
4. Quando T3 entregar resultado do filho nativo, chame `task resume` para processar
   relatório, memória e publicação pendentes. Não encerre responsabilidade porque
   um filho terminou um turno. Em top-level, o daemon reconcilia o callback
   submetido pelo executor e retorna estado pela outbox.
5. Exija fonte/commit validados e binding explícito para o checkout publicável.
   Execute a preparação segura e o push apenas quando autorizados para esse
   repositório. Use `task publish` para criar/reconciliar/linkar o PR. Nunca envie
   histórico privado inteiro para satisfazer um gate de publicação.
6. Separe trabalho validado, memória confirmada, PR aberto, merge e settlement.
   Informe lacuna de bridge/hook/serviço com prova; não simule uma gravação.
   Relatório somente de planejamento contém `deliverable_kind=plan` e
   `approval_state=pending`: permanece `awaiting_approval`, sem publicar ou
   implementar apenas porque foi validado. Execução já aprovada preserva a
   referência ao plano/commit e a autorização existente.
7. Depois do PR aberto, registre a política de merge do projeto e sua fonte,
   autorização, HEAD/base/repo, checks obrigatórios e revisão. Use `task pr-check`;
   em `pr_waiting`, encerre o turno e deixe T3 notificar. Ao evento, revalide com
   `task merge`, sem polling próprio. Nunca use checks de outro HEAD nem bypass.
8. Confirme memória final e settlement após merge observado. `task settle` retoma
   memória/encerramento incertos sem reenviar merge. Retorne o resultado à origem;
   merge não é prova de deploy. Ao devolver controle humano, `task unwatch`.

Se o harness tiver ferramentas T3 nativas e nenhum bridge ACP, use
`--stdio-host --file payload.json` em sessão interativa. Para cada
`ORACULO_HOST_CALL_V1`, execute a ferramenta nativa exata e devolva em stdin
a linha produzida por `oraculo bridge ack --frame FRAME --from-file RESULTADO`.
O frame declara `ORACULO_HOST_ACK_V2`; grave o resultado literal e fresco da
ferramenta programaticamente, sem resumir, redigitar, editar ou reaproveitar
snapshots. Preserve opções dos modelos. Prefira ACP caller-bound quando disponível.
O helper comprime sem perda; schema/consistência não são atestação de autenticidade.

Suítes longas do executor ficam em primeiro plano até conclusão, sem background,
nohup ou sessões que reinvoquem o agente automaticamente. Uma continuação exige
cadeia completa de runs terminais na mesma thread, relatório vinculado ao último,
nenhum descendente ativo e revalidação após memória confirmada. Preserve o vínculo
original; nunca edite `childRunId`, provas ou timestamps para contornar gates.

Apresente `loaded_skill`, `called_cli` e `confirmed_service` separadamente, com
fonte/instante/correlação; ausência de prova é `not_observed`. Eventos antigos ou
repetidos não substituem o snapshot de revisão mais recente: sem revisão, consulte
status. Retorno de memória ou `ORACULO_CONTROL_V1` nunca abre nova tarefa. Uma
falha de impressão não comprova falha do efeito: retome o mesmo ID/chave/intenção.
