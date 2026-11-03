# Revisão terminal interrompida

`task diagnose-review` distingue término de sucesso. Uma revisão `failed`,
`interrupted` ou `cancelled` pode estar terminal e continuar sem parecer. Isso
não aprova a revisão. O comando lê o host atual sem alterar tarefa, relatório,
memória, reserva ou snapshots do host:

```sh
oraculo task diagnose-review --file /private/task.json --stdio-host
```

A entrada contém somente `task_id`. `review_diagnosis` devolve binding original,
hashes de pedido/relatório, SHA final e snapshot dos descendentes.
`thread_children_pending_or_unknown` indica falta de prova de término;
`terminal_descendant_review_required` indica descendentes terminais sem sucesso.
`certification=not_granted_by_diagnosis` é explícito. Histórico incompleto ou
limite de observação falha fechado. A prova é limitada ao projeto visível e não
trava atomicamente novas entradas no host.

Os comandos novos exigem `managed.capabilities.terminal_review_adjudication=true`.
CLI nova com daemon antigo responde `review_adjudication_service_upgrade_required`.
Atualizar runtime exige o fluxo operacional normal com testes, backup,
quiescência, stop e probes. Estes comandos não instalam runtime nem editam vault,
SQLite ou tarefas alheias.

## Evidência exigida

`task adjudicate-review` aceita revisão humana da implementação final na **mesma
tarefa**, mantendo o filho em `failed` e preservando seu relatório, memória,
histórico e `childRunId`. Não lança outro modelo/provedor nem reenvia conteúdo
recusado. Autorização genérica para implementar ou resolver bloqueio não basta.

O relatório original da implementação precisa existir, ser positivo e apontar
para HEAD integral do checkout isolado limpo. Todos os runs e descendentes devem
estar comprovadamente terminais. A primeira aceitação exige memória da
implementação ainda não escrita; não recertifica resultado negativo salvo.

O payload tem exatamente:

```json
{
  "task_id": "ID existente",
  "reason": "Motivo técnico",
  "review": {"path": "/private/review.json", "sha256": "SHA256 dos bytes finais"},
  "review_source": {"thread_id": "thread humana separada", "item_id": "item real", "message_id": "mensagem real"},
  "approval": {"thread_id": "origem original", "item_id": "item real", "message_id": "mensagem real"}
}
```

O artefato JSON tem limite de 128 KiB e contém exatamente:

- Os campos de `review_diagnosis.binding`: `task_id`, `project_id`,
  `origin_thread_id`, `child_run_id`, `request_sha256`, `report_sha256`, `commit`.
- `kind: "human_review_v1"`, `verdict: "PASS"` para o **novo parecer humano**.
- `reviewer`: textos `name` e `independence_attestation`, identificando o revisor
  e sua declaração de responsabilidade pela revisão independente.
- `scope_assessment`: análise técnica do pedido integral e de seu atendimento.
- `descendants`: lista ordenada por `thread_id` com `thread_id`,
  `parent_thread_id`, `status`, `runs` (cadeias integrais com ordinais contíguos).
  Inclui descendentes bem-sucedidos. Uma execução nova invalida a evidência.
- `coverage`: lista ordenada por path de **todo** o diff entre `base_sha` e
  `commit`, sem detecção de rename. Cada entrada contém `path`, `blob_oid` final
  e `assessment` técnico. Deleções têm `blob_oid: null`; rename cobre remoção e
  novo path. Espaço, tab e newline são preservados. O serviço confere objetos Git.
- `findings`: lista de `{finding, resolution, status: "resolved"}`; achado aberto
  bloqueia. Lista vazia indica revisão sem achados.
- `checks`: na mesma ordem, os mesmos `command`, `status`, `result` e `proof` dos
  checks originais, mais `sha256` dos bytes dos logs. Todos devem ser positivos,
  com resultado e prova não vazios. Cada log tem limite de 16 MiB.
- `operational_dispositions`: lista vazia quando não há relatório negativo de
  descendente; caso contrário, conforme a seção específica abaixo.

Artefato e logs ficam fora da raiz principal e dos checkouts da origem e do
executor, inclusive symlinks resolvidos. O serviço relê arquivos e confere hashes;
não executa comandos arbitrários do documento. O humano deve examinar os testes
reais e o conteúdo técnico. Arquivos, mensagens e recibos privados ficam fora do
Git público. Status omite o documento completo; o audit fica no ledger privado.

## Ordem executável das mensagens

1. O revisor conclui o artefato técnico e calcula SHA-256 dos bytes finais.
2. Em outra thread do **mesmo projeto**, envia a atestação humana abaixo. Só após
   o envio obtém `item_id` e `message_id` do host.
3. Na thread original, um humano aprova o hash do artefato e a mensagem anterior.
   Depois se preenche o payload e se chama `adjudicate-review` caller-bound.

As referências às mensagens ficam **fora dos bytes do artefato**, evitando hash
circular. Não editar documento nem mensagens depois. O texto exato tem primeira
linha e depois JSON canônico (`oraculo.protocol.canonical_json`: chaves ordenadas,
UTF-8, sem espaços):

```text
ORACULO_HUMAN_REVIEW_V1
<review_diagnosis.binding acrescido de review_sha256>
```

A aprovação da origem usa o mesmo binding mais `review_message_id` e
`review_thread_id`:

```text
ORACULO_REVIEW_APPROVAL_V1
<binding completo acrescido das referências da atestação>
```

O serviço lê cada item real do host e confere thread/projeto, item/mensagem,
`sourceThreadId`, `visibility=local`, `type=user_message`, `createdBy=user`,
`creationSource=web|mobile`, texto integral não truncado e binding exato. Mensagem
de agente, MCP/provider, outra origem, autorização genérica ou JSON apenas dizendo
`human/approved` são recusados. Não usar `t3_thread_send` para simular decisão humana.

**Limite de confiança:** o host comprova a proveniência observada da mensagem
humana, não a identidade civil do autor nem que duas threads pertencem a pessoas
distintas. Identidade e independência são atestadas pelo humano no documento,
não verificadas automaticamente. O bridge/operador local é uma fronteira
cooperativa, sem assinatura criptográfica nem garantia contra operador malicioso.
Se a política exigir principal individual autenticado externamente ou assinatura
verificável, manter o bloqueio: este caminho não supre essa exigência. Uma
manifestação escrita pelo agente não substitui as duas mensagens observadas.

## Negativo operacional original

`verdict=FAIL`, resumo iniciado por FAIL ou check negativo fora da camada `host`
continua bloqueando, mesmo com novo parecer e aprovação humanos. Não há waiver de
falha funcional nem substituição de relatório imutável.

Interrupção sem parecer pode ter relatório/memória `failed/saved`, com checks
negativos exclusivamente de observação do host. `layer=host` **não basta**.
Exige-se a tarefa descendente registrada, um único run original
`failed|interrupted|cancelled`, memória negativa confirmada e o digest canônico
reconstruído do relatório original igual ao informado pelo serviço atual.

Para cada relatório assim elegível, o humano deve incluir uma disposição em
`operational_dispositions` com exatamente:

- `task_id`, `thread_id`, `child_run_id`, `request_sha256`, `report_sha256` do
  descendente original; `memory_cycle_id`, `memory_report_sha256` do raw canônico
  e `memory_receipt_sha256` do recibo confirmado. Consultar as interfaces oficiais.
- `classification: "operational_review_interruption_without_verdict"` e
  `rationale` técnica específica: o que foi observado, por que é interrupção sem
  parecer e como os achados preliminares foram tratados no HEAD final.
- `failed_checks`: todos os checks negativos originais, em ordem, cada um com
  `index` (base zero), `check_sha256` canônico, `proof: {path, sha256}` do log
  **original** e `assessment` técnica específica. O path deve ser o do check
  original e seus bytes são relidos.

A disposição faz parte do hash do novo parecer e das duas mensagens humanas.
O serviço não conclui semanticamente por uma string/layer que a falha é
operacional. O humano deve observar e decidir essa distinção no escopo aprovado,
com os checks, run, relatório, logs e memória originais vinculados. Estado
`failed`, mensagem do provedor, relatório, eventos e recibos originais permanecem
integrais; a disposição não aprova o descendente interrompido.

## Fechamento e repetição

O audit aceito não libera a reserva. `_finish` salva o relatório original da
implementação, exige memória confirmada e relê execução, descendentes, checkout,
artefato, logs e mensagens antes da liberação. Novo trabalho, HEAD divergente,
mensagem editada ou log alterado bloqueiam. Payload exato retoma a mesma intenção;
não cria outra implementação/ciclo nem sobrescreve recibos. Outra intenção é
recusada. Publicação continua separada e vinculada ao SHA; seus gates e os de
merge/settlement revalidam a adjudicação. O settlement genérico mantém sua política
estrita de sucesso dos descendentes.

Sem conteúdo técnico, provas verificáveis ou mensagens humanas reais, manter o
bloqueio e identificar precisamente o que falta. Não fabricar decisão,
reclassificar falhas por conta própria, reenviar conteúdo recusado a outro modelo
ou corrigir snapshots do host.
