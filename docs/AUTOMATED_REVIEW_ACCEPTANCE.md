# Entrega, revisão de CI, integração e ativação

## Contrato de produto

A origem recebe a implementação no escopo comprovado por execução, identidade,
HEAD, relatório, testes obrigatórios e memória. Revisão de qualidade pertence ao
CI/PR e condiciona integração conforme a política real. Não é pré-condição humana
ou manual para registrar uma entrega. Não existe novo payload de aprovação para
concluir a implementação.

Os recibos têm responsabilidades distintas:

- **Entrega:** relatório original, commit, checks obrigatórios, cadeia terminal,
  memória confirmada e liberação da reserva da implementação. `pr_pending` e
  `work_status=succeeded` significam entrega verificada, não revisão aprovada.
- **Qualidade:** `delivery_review` conserva a observação do revisor, inclusive
  `failed`, falta de parecer, achados e restrição não resolvida. Sempre declara
  `review_approved=false`, `integration_authorized=false` e
  `provider_redispatch_permitted=false`. Esse recibo de entrega não é alterado
  retrospectivamente quando outra fase avança.
- **CI/PR:** o check oficial **Product review (exact HEAD)** executa cenários de
  produto e invariantes, gera matriz requisito/cenário/prova/resultado, JUnit,
  logs e hashes no SHA exato. É avaliação automatizada determinística, não
  certificação humana ou revisão semântica independente por outro modelo.
- **Memória de CI:** cada rodada terminal observada tem ciclo próprio antes do
  merge, sem retroceder `work_status=succeeded` nem reescrever a memória/relatório
  da implementação. Ledger/outbox de observações e memória canônica confirmada
  são recibos distintos.
- **Integração:** checks e revisão obrigatórios observados no host real, política
  explícita, SHA atual e recibo de merge. Entrega ou memória não autorizam merge.
- **Ativação:** recibo separado; um merge não comprova instalação ou rollout.

O workflow público e seu template canônico são versionados juntos. A política
pode exigir `Product review (exact HEAD)` em `required_checks`; branch protection
remota continua sendo autoridade adicional. Esta tarefa não altera a proteção
em produção, não faz merge e não instala runtime. O job lê o HEAD do PR, executa
`scripts/review_product_eval.py` e preserva evidência positiva e negativa. A suíte
unitária sozinha e uma frase `PASS` do executor não substituem essa avaliação.

**Disponível não significa obrigatório.** Versionar o workflow cria um check
disponível; não o adiciona automaticamente às regras do GitHub. A política remota
anterior informada pela origem permanece intacta e não foi consultada/modificada
nesta execução. Para este candidato, uma política managed explicitamente
autorizada pode acrescentar o nome exato em `required_checks`: o PRLifecycle
então exige sua presença e sucesso, além dos checks/reviews anteriores. Sem essa
exigência ou uma regra remota observada, não se alega enforcement global. Qualquer
alteração de regra remota é outra operação autorizada, com observação e recibo
próprios. Os testes verificam ambos os casos e impedem `ready` quando o check
exigido estiver ausente, pendente ou negativo.

## Papéis anteriores à execução

O código anterior só distinguia `implementation`, `plan`, `validation` e
`research` como entregáveis; o lançamento nativo usava o papel genérico
`implementation`. `validation` sozinho não prova se um filho é revisão de
qualidade ou teste funcional obrigatório. Não se infere essa distinção do título,
texto de erro, `failed` ou de uma string dizendo `approved`.

Pedidos novos de validação podem declarar `task_role=quality_review` ou
`task_role=functional_tests`, com `deliverable_kind=validation` e `review_target`
exatamente igual a `{task_id, request_sha256, report_sha256, commit}` da
implementação. O alvo é obtido do relatório já submetido pelo executor. O serviço
confere esse binding e o chamador antes de criar/executar a tarefa. A requisição
original e seu hash continuam imutáveis. A CLI exige a capability
`delivery_review_separation`; daemon antigo falha com
`delivery_review_service_upgrade_required`, sem ignorar o papel silenciosamente.

A revisão de qualidade não precisa ser criada para toda entrega: a avaliação
normal de produto já roda no CI oficial. Quando existe um filho de qualidade,
seu término sem parecer não prende a memória/reserva da implementação. A reserva
própria do revisor e sua memória continuam separadas; se ele não entregou relatório,
essa lacuna permanece visível, sem fabricar sua finalização contábil.

A dispensa é estrita: todos os descendentes precisam estar terminais, com cadeia
completa, e cada exceção precisa de papel/alvo anteriores à execução. Papel
legado ausente ou binding divergente bloqueiam com lacuna de evidência. Não há API
que reescreva o pedido antigo para fabricar um papel conveniente. A alternativa
humana existente permanece explícita, sem ser default/fallback obrigatório.

O snapshot enumera descendentes recursivamente. Cada revisão/teste é conferido
contra seu **próprio proprietário**, identificado pelo alvo e pela relação real
de pai/origem na árvore observada; não contra o HEAD da raiz por conveniência.
Sua base de lançamento também precisa ser o HEAD desse alvo, antes de executar.
Um proprietário aninhado precisa de entrega positiva, execução/HEAD limpos,
cadeia completa e memória confirmada; seus hashes e logs ficam vinculados ao
recibo da raiz. A validação usa o recorte dessa árvore já observada e os recibos
originais, sem fazer `task_status` de netos como se a raiz fosse seu pai nativo.
Alvo trocado, fora da árvore, HEAD/log obsoleto e teste funcional negativo em
qualquer nível continuam bloqueando. O status e as provas dos filhos não mudam.

## Negativos e atualidade

Checks negativos da implementação sempre mantêm sua entrega negativa. Um filho
`functional_tests` precisa entregar validação positiva e memória confirmada no
alvo correto. Check negativo funcional/integration/unit dentro de uma revisão de
qualidade também bloqueia a entrega. Papel de qualidade não transforma teste
funcional falho em opinião opcional.

Check negativo `host` exige o log JSON original com task/thread/run e status
iguais aos observados, `workState=result_available` e ausência de filhos pendentes;
`layer=host` sozinho não basta. Uma revisão sem parecer não recebe PASS. Um FAIL
explícito sem escopo negativo comprovado permanece ambíguo e bloqueante.
Achados de qualidade são identificados por check negativo `layer=quality` em uma
requisição previamente vinculada como qualidade: permitem registrar a entrega,
mas bloqueiam integração por `quality_review_changes_requested`. Corrigir exige
nova evidência/HEAD/tarefa no fluxo autorizado; o relatório negativo original
não é sobrescrito nem limpo com um check verde genérico.

Os logs da implementação são privados, têm hashes registrados e são relidos antes
de liberar a reserva e nos gates posteriores. Nova execução, trabalho queued/
unknown, cadeia incompleta, novo HEAD, alteração de log ou alvo durante a memória
bloqueiam os efeitos correspondentes. Publicação também revalida depois de gravar
sua memória. Novo HEAD do PR exige novos checks; o adapter remoto mantém os gates
de identidade, checks, reviewDecision e mudanças solicitadas, sem bypass.
O primeiro recibo funcional delegado também fixa pedido/alvo, task/thread/run,
relatório, execução, memória e hashes dos logs. Esses bindings são comparados no
pós-memória, replay e publicação, inclusive sem qualquer revisão de qualidade.
Recalcular um digest de bytes editados não renova a prova. Campos voláteis de
exibição não participam dessa comparação.

Correção de implementação negativa usa nova tarefa/HEAD, testes novos e
`supersede` quando aplicável. Task/run/relatório/memória/recibos negativos ficam
intactos. `origin_validation` é validação focal da origem, `not_independent`, sem
certificação humana. A entrega não promove a origem a revisor independente;
provedor/modelo observados ou ausentes ficam registrados como tais.

## Restrição do provedor e retomada

Uma recusa ou possível restrição de segurança do revisor fica preservada no
snapshot original. Não se conclui semanticamente que seja falha de infraestrutura,
achado técnico FAIL ou aprovação. Entregar a implementação verificada não revoga
essa restrição, não autoriza redispatch e não significa publicação/integração do
projeto alvo. Nenhum conteúdo recusado é enviado a outro modelo/provedor.

Após revisão e instalação oficial futura, a origem poderá usar
`task diagnose-review` e `task resume` **na mesma implementação/HEAD e ID**. Se
execução/testes/término estiverem comprovados e o papel/alvo original sustentar a
separação, o serviço salva a memória e libera a reserva da implementação,
conservando o revisor failed/sem parecer. Se faltarem os campos originais de papel
ou alvo, o diagnóstico identifica essa lacuna; não reclassifica tarefas privadas
por conta própria. Esses campos não foram observados no caso externo informado
nesta execução: não se promete liberar essa ocorrência.
Precisamente, não foram fornecidos/observados nesta execução o pedido canônico
original com `task_role`, o `review_target` anterior à execução e seus hashes de
pedido/relatório/HEAD. Há apenas a informação recebida de revisão failed com
possível restrição e sem parecer. Não se converte isso em timeout ou prova de
escopo funcional. Se o registro legado realmente não contiver os campos, esta
versão não oferece migração/auditoria oficial para anexá-los retrospectivamente.
Será necessária uma extensão de auditoria baseada em evidência histórica
verificável, não reclassificação livre, nova implementação para escapar do
registro nem humano obrigatório. Este delta não promete tal recuperação.

Depois, `publish` abre/reconcilia o PR autorizado e `pr-check`/`merge` consultam os
checks/revisão reais no HEAD. CI pendente/falho, achado de qualidade não resolvido,
HEAD novo ou política não satisfeita continuam impedindo integração. O cliente
externo, seu checkout e seus runs não foram usados nos testes deste delta.

## Matriz e limites da avaliação

### Memória do laço de CI antes do merge

`pr-check` (ou o mesmo caminho de `merge`) observa o repositório autenticado,
persiste a observação no ledger/outbox existente e grava cada rodada de checks
obrigatórios terminais usando `memory.begin/finish`. Não há watcher novo nem
redispatch: a origem chama o serviço ao receber o evento de PR, como antes.
Sem observação/retorno do host, o serviço não inventa uma rodada.

`ci_rounds` expõe identidade, resultado e recibo de memória de cada rodada;
o relatório completo mantém a prova autenticada, task/projeto/PR/HEAD/política,
relatório e memória da implementação. Uma falha CI fica em ciclo `failed`, embora
a entrega continue `succeeded`. Cancelamento/neutral/skip conservam a conclusão
original nos checks; não provam falha funcional nem sucesso obrigatório.
Checks pendentes/ausentes não ganham resultado terminal fabricado. Check verde
também não aprova reviewDecision, revoga recusa do provedor ou autoriza merge.

No GitHub, a descoberta de IDs usa a API autenticada e reaproveita o observador
existente para conferir PR/repositório/base/HEAD, workflow/run/attempt, check-suite,
jobs e política real, com releitura de consistência. A precisão é **por check**:
`facts.check_bindings` registra `workflow_check_run_attempt` somente quando o
observador comprovou check/run/attempt/job/HEAD. Checks adicionais da política
managed continuam identificados pelo snapshot exato, mesmo ao lado de workflows
remotos conhecidos. Quando o host só expõe checks (incluindo o caminho Azure
atual), a precisão desse check é `check_snapshot_without_run_attempt`.
Não se fabrica attempt=1. Estados diferentes sem ID identificam snapshots
diferentes, sem certificar execuções distintas. O relógio local `observed_at`
nunca identifica tentativa/rodada; replay exato não grava outro ciclo. O snapshot
de um check managed adicional pode mudar e acrescentar rodada sem atribuir-lhe
o run de outro check. Conclusão/prova de um check realmente identificado não pode
mudar na mesma tentativa, inclusive entre rodadas com outros checks diferentes.
Hashes integrais de endpoints continuam no primeiro relatório de auditoria;
somente fatos e jobs pertinentes aos checks da política entram no binding de
contradição. Metadados/jobs opcionais irrelevantes não fabricam nova tentativa
nem contradizem uma prova obrigatória que permaneceu igual.

A intenção e a chave estável são persistidas antes de I/O. Resposta perdida de
begin/finish retoma o mesmo ciclo; `pending`/`failed` usa retry oficial, sem editar
SQLite/vault. Falha de leitura, escrita ou binding fica na etapa correspondente
`ci_round_memory_*`, mantendo o CI negativo e a entrega original. Qualquer rodada
já registrada precisa ter memória confirmada antes de avançar o merge; checks
exigidos incompletos também impedem `ready`. O HEAD/checks/execuções são relidos
após gravar memória e antes de emitir merge. Tentativa regressiva, mudança de
resultado terminal da mesma identidade ou prova divergente bloqueiam.

Uma nova tentativa real acrescenta rodada ligada à anterior. Isso não infere
diagnóstico ou correção. Se houve mudança de implementação já entregue, o fluxo
autorizado usa **nova tarefa**, novo relatório/testes/commit e publicação própria;
o relatório da nova tarefa pode apontar `evidence.ci_predecessor={task_id,round_id}`
para a falha confirmada do mesmo projeto/repositório. O serviço exige commits e
HEADs efetivamente diferentes e conserva ambos. Diagnóstico é opcional:
`evidence.ci_diagnosis={summary,proof:{path,sha256}}` só é registrado se fornecido
com artefato privado real; não nasce de uma string "CI failed". Esses campos são
referências opcionais na evidência já existente, não documento de aprovação para
entregar. Nenhuma tarefa corretiva é criada automaticamente.

O resumo canônico contém falha/checks, identidades e vínculos das rodadas; o
envelope estruturado preserva a prova completa. Uma nova conversa/execução lê
essa memória pelo ciclo oficial do projeto antes do merge. Não depende apenas
de consultar histórico no ledger. Os testes usam o contexto selecionado de uma
conversa nova para decidir diagnosticar a rodada negativa e vincular a correção;
também verificam que âncoras curtas sobrevivem a uma síntese extensa abreviada.
A seleção tem orçamento: não garante recuperar todas as rodadas em toda consulta.
Se a referência relevante não vier no contexto, sua ausência é lacuna de leitura,
não prova de inexistência de falha nem autorização para inferir uma correção.
A evidência integral fica na fonte canônica referenciada. Não existe aprendizado
dos pesos ou certificação de adequação de todo produto cliente nesse harness.
A CLI exige a capability `ci_round_memory` para
`pr-check/merge/settle`: daemon antigo declara necessidade de atualização oficial,
sem alegar que salvou memória CI. Runtime global permanece inalterado nesta tarefa.

O contrato executável é [AUTOMATED_REVIEW_ACCEPTANCE.json](AUTOMATED_REVIEW_ACCEPTANCE.json).
O recibo medido contém cada parametrização, resultado, SHA, JUnit e hashes dos
logs; ausência, falha ou skip em cenário obrigatório faz o avaliador falhar.
`ACCEPTED_OFFLINE` significa cenários offline aprovados e mantém explícitos os
limites de host sintético, CI remoto não observado e ativação não realizada.

| Requisito | Cenário | Prova | Resultado exigido |
| --- | --- | --- | --- |
| Entrega separada de parecer | API/CLI; revisor failed, sem relatório, recusa ou achado de qualidade | `test_delivery_completes_without_quality_approval_or_humans` | Memória/reserva da implementação resolvidas; nenhum PASS de revisão |
| Teste funcional e papel desconhecido | Filho funcional negativo, check funcional na revisão, papel ausente e trabalho vivo | `test_functional_failure_unknown_role_and_live_work_still_block_delivery` | Entrega bloqueada, histórico intacto |
| Correção autorizada | Implementação negativa e nova tarefa/HEAD/testes | `test_functional_negative_correction_preserves_original_report_memory_and_ids` | Negativo original preservado; substituição explícita |
| Prova atual e replay | Mudanças de HEAD/log/run/alvo após memória; repetição da mesma entrega | `test_delivery_revalidates_after_memory_without_fake_review_pass` e jornada API/CLI | Não libera prova obsoleta; não duplica memória/execução/reserva |
| Integração independente da entrega | CI pendente/falho, achado de qualidade, HEAD novo e política satisfeita | `test_delivered_quality_review_does_not_authorize_merge_and_ci_revalidates_head` | Só integra com gates satisfeitos; revisão original não é reescrita |
| Host remoto real preservado | Head/check/reviewDecision divergentes | `test_pr_lifecycle.py` | Sem emissão de merge em observação negativa/ambígua |
| Escopos aninhados | Raiz → implementação filha → revisão/testes do HEAD da filha; alvo trocado/externo, base/log/HEAD obsoletos e negativo funcional | `test_nested_delivery_review.py` | Proprietário correto e prova entregue; nenhum target arbitrário ou consulta nativa como pai de neto |
| Laço CI em memória canônica | API/CLI; falha autenticada, replay, escrita incerta, diagnóstico com prova, nova tarefa/commit/avaliação e nova conversa | `test_ci_memory_loop_*` | Falha e nova rodada salvas antes do merge; mesma chave/ciclo na retomada; entrega imutável; IDs/HEAD/tentativa divergentes não liberam merge |

Esta correção automatiza a avaliação de produto no CI oficial; não promete revisão
semântica autônoma por modelo para todo projeto, runner universal de todos os
checks nem observação de CI remoto nesta execução. A integração pública e a
revisão de aceitação deste delta pertencem ao parent; o runtime global segue
intacto até o fluxo oficial posterior.
