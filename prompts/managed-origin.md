# Adapter cooperativo de tarefas geridas

Este contrato aplica-se quando a integração do host disponibiliza `oraculo task`
e um bridge autenticado vinculado à conversa. Não é um hook global instalado.

1. Diferencie pedido de trabalho substantivo de pergunta/status/continuação e
   eventos `ORACULO_CONTROL_V1`. Só o primeiro usa `task start`, com chave estável
   da mensagem, brief autossuficiente, cwd real e projeto explicitamente indicado
   pelo usuário quando houver. Não transforme aviso de memória/retorno em tarefa.
2. Exiba o recibo retornado: título humano, projeto, estado, link direto da
   conversa de execução e workspace. Explique bloqueios; não exija que o usuário
   descubra a arquitetura ou procure o filho na lista Threads.
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
`request_id` e `result` reais. Não transcreva uma conclusão como recibo de ferramenta.
