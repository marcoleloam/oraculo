# Bateria de continuidade — AT-020

Gerado por: Codex / gpt-6-astra · 2026-10-19.

Objetivo: comprovar que o serviço termina uma etapa e inicia a próxima autorizada sem nova mensagem humana, preservando identidades após restart do daemon. A bateria local usa transporte T3 simulado; o ensaio final exige T3 real, registros dos processos e horários de fechamento/reabertura das interfaces. Não altera o contrato congelado nem aprova automaticamente o cenário humano.

## Execução local reproduzível

Na raiz do projeto, com o ambiente sincronizado:

```sh
uv run --frozen python scripts/test_at020.py
```

O runner cria um diretório novo em `.claude/sdd/reports/AT020/<UTC>/`, com `result.json`, `junit.xml` e `pytest.txt`. Propaga o exit code do pytest. Não exige login, não usa provedores reais e não toca no estado/vault do daemon instalado. Não marca defeitos conhecidos como xfail e não converte falha em sucesso.

## Matriz automatizada

| Caso | Verificação | Critério |
|---|---|---|
| C01 | Etapa → memória após restart | Mesmos attempt/generation/thread/run/task; só uma memória; despacho efetivamente enviado sem nova submissão humana. |
| C02 | Filho terminal com descendente ativo | Grant retido e memória não enfileirada até toda a árvore terminar. |
| C03 | Restart real do processo | Socket/permissões e exclusão mútua; estado persistido recuperado após SIGTERM. |
| C04 | Resposta perdida e replay de memória | Mesmo payload e chave persistidos, inclusive depois de restart; nenhum novo objetivo. |
| C05 | Retry do despacho atual | Máximo de três tentativas; payload estável; próxima tarefa continua elegível após esgotamento. |
| C06 | Despacho histórico terminal, coordenador mais recente ocioso | Retry permitido dentro do orçamento, com e sem restart; sem relaxar a conclusão de executores. |
| C07 | Terminalidade não comprovada | Run ativo, pedido pendente, descendente vivo, run desconhecido ou recibo legado não autorizam retry. |
| C08 | Corrida durante leitura remota | Claim, cancelamento ou tentativa concluída impedem despacho indevido. |
| C09 | Memória com evidência pequena/grande/Unicode | Despacho ≤120000 unidades UTF-16, incluindo instruções; snapshot integral recuperável e raw preservado, sem truncamento. |
| C10 | Desconexão/reconexão | Estado incerto mantém grant; reconciliação só libera após identidade e relatório válidos. |
| C11 | Cancelamento com restart | Intenção persiste; descendentes interrompidos; vaga liberada só após confirmação. |
| C12 | Relatório repetido ou run antigo | Relatório imutável/idempotente; run antigo não conclui execução atual. |
| C13 | Leitura do snapshot por `memory.read` | Grant, geração, projeto/job e artefato emitido conferidos em cada página; hash integral validado pela CLI, inclusive após restart. |
| C14 | Snapshot inválido ou acesso fora de escopo | Cancelamento, identidade incorreta, tamper, traversal, symlink, offset inválido ou outro job impedem leitura; download parcial removido. |
| C15 | Outbox legada excessiva | Mensagem/chave originais preservadas; sinalização explícita de reconciliação, sem presumir rejeição remota. |
| C16 | Run original cancelado e thread retomada pelo T3 | Reconciliação só pode liberar o grant como parada após provar terminalidade do run original e de toda a árvore atual; não aceitar o novo run como sucesso da tentativa original. |

O runner inclui `tests/test_continuity.py`, `tests/test_dispatch_history.py`, `tests/test_memory_transport.py`, `tests/test_reconcile.py`, `tests/test_reconcile_cancelled.py` e regressões selecionadas de serviço, contratos e lifecycle. A validação da V1 passou **88 casos nesta bateria e 180 na suíte completa**. A bateria é um subconjunto da suíte: as contagens não devem ser somadas.

C16 cobre uma tentativa original cancelada cuja thread foi retomada em outro run. O grant só é liberado como parado após comprovar a terminalidade original e da árvore atual. Liberar uma tentativa como parada não a converte em sucesso. Nenhuma falha deve ser ignorada para concluir a bateria.

## Ensaio real com interfaces fechadas

**Preparação pelo executor:** conferir fila vazia e lease nula; confirmar daemon com OAuth próprio e T3 em execução; usar somente o projeto/vault descartáveis. Manter discovery desabilitado para não introduzir tarefas extras. Preservar configuração e evidências anteriores. Renovar prevenção temporária de repouso (`caffeinate -i -t 900`) imediatamente antes do ensaio; não presumir que o processo de uma rodada anterior continua ativo.

1. Submeter **nova** tarefa descartável de duas etapas: implementação/teste simples, com memória automática como etapa seguinte. Incluir uma espera curta e limitada na primeira etapa para permitir fechar as interfaces. **Antes de pedir o fechamento, confirmar o grant claimed e observar o filho efetivamente em espera**, com timestamp de início. Só então iniciar o intervalo de interfaces fechadas. Não reutilizar objetivos cancelados nem alterar outbox/SQLite manualmente. Exercitar fontes pequenas e grandes em rodadas identificadas separadamente.
2. Registrar antes do fechamento: UTC e BRT, objective_id, job_id, attempt_id, generation, task_id, thread_id, run_id, PID do daemon, PIDs do T3 desktop e backend, commit/worktree da aplicação e identidade do projeto. Guardar a submissão inicial e os checks esperados. Registrar processos vivos antes e imediatamente depois de fechar a janela; manter observações durante o intervalo e ao reabrir.
3. Encerrar atividade do plugin e fechar Obsidian. Fechar somente a **janela** T3 no Mac pelo botão de fechar (`AXCloseButton` quando automatizado) e a interface no iPhone. Não usar Encerrar, `Cmd+Q`, `kill` ou outra ação que finalize o processo T3. Manter o Mac acordado. Registrar horários de cada interface e método de observação; a confirmação humana cobre o iPhone quando não houver instrumento direto. O início do intervalo comum é o último desses fechamentos. A frase “fechei o app” isolada não comprova preservação do processo.
4. Manter as interfaces fechadas por **pelo menos dois minutos**. A janela precisa incluir a conclusão da primeira etapa e o início da segunda; dois minutos isolados não garantem isso, pois a duração do provedor varia. Não enviar mensagens, cliques de continuação ou novos objetivos nesse intervalo.
5. Registrar os horários de reabertura. Comparar recibos/timeline T3, observações de processos e estado do daemon: primeira etapa concluída dentro do intervalo; segunda admitida e efetivamente iniciada dentro dele; mesmas relações de objetivo/proveniência; uma única memória e nenhuma submissão humana intermediária. Confirmar que desktop/backend T3 permaneceram vivos. Se houve encerramento/restart do T3, transição fora do intervalo ou faltam horários, resultado **inconclusivo para interfaces fechadas**, nunca aprovado por ausência de mensagens. Preservar a evidência de interrupção como cenário distinto.
6. Em uma segunda rodada com filho ativo, salvar identidades, reiniciar somente o daemon, aguardar socket saudável e comparar attempt/generation/task/thread/run. T3 e filho devem continuar vivos. Exigir zero duplicatas e continuidade após reconciliação. Não iniciar outro daemon enquanto o anterior ainda existir.
7. Exercitar interrupção e desconexão controladas apenas no fixture; conferir `cancel_requested` enquanto houver descendentes ativos, parada confirmada e ausência de reinício indevido. Não confundir cancelamento manual de job bloqueado com continuidade autônoma aprovada.
8. Guardar registros sanitizados, fontes/hashes, comandos e exit codes. Encerrar processos temporários de apoio após a fila reconciliada; manter arquivos/estado para inspeção. Solicitar aceite de Marco sobre o cenário completo somente após os resultados serem apresentados.

## Registro obrigatório por rodada

```json
{
  "scenario": "AT-020",
  "code_commit": "preencher",
  "project_id": "preencher",
  "objective_id": "preencher",
  "interfaces_closed_at": {"t3_mac": null, "obsidian": null, "iphone": null},
  "interfaces_reopened_at": {"t3_mac": null, "obsidian": null, "iphone": null},
  "child_wait_started_at": null,
  "processes_before_close": null,
  "processes_after_close": null,
  "process_observations_during_interval": [],
  "processes_after_reopen": null,
  "closure_observation_method": null,
  "stage_one_terminal_at": null,
  "stage_two_started_at": null,
  "identity_before_restart": null,
  "identity_after_restart": null,
  "new_human_submission_in_interval": null,
  "duplicate_attempts": null,
  "manual_recovery": null,
  "verdict": "pending"
}
```

Usar timestamps ISO-8601 com fuso. Campos nulos são pendências, não evidência negativa. Ausência de nova mensagem, screenshot de janela fechada ou viewport móvel não substituem a sequência temporal. A rodada anterior já terminou: não serve para um fechamento futuro.

## Fechamento

Execute a bateria e a suíte completa após estabilizar alterações. Os testes locais não substituem o ensaio real com interfaces fechadas nem o restart com preservação de identidades. Registre o resultado da nova rodada, inclusive tentativas inconclusivas, e solicite o aceite sobre o escopo efetivamente demonstrado.

Na V1, a rodada final comprovou a transição durante 231,6 segundos de fechamento; um ensaio separado confirmou o restart preservando identidades. Os limites constam no [resumo público da validação](VALIDACAO_V1.md). Evidências brutas e conversas do ambiente de desenvolvimento permanecem privadas.
