# Encerramento verificado de tarefas

`settled` organiza a conversa no T3. Não comprova deploy, aceite dos dados ou
conclusão de objetivos fora do contrato registrado. O Oráculo mantém separados
o resultado da execução, o sucesso do objetivo, o merge e o fechamento observado.

## Registrar uma intenção

Use uma conversa dedicada à entrega, com identidade conhecida. Coordenadores
permanentes não podem ser alvos. Registre os PRs nessa conversa pelas ferramentas
públicas `link_pull_request` e confira `list_thread_pull_requests`. O T3 já
acompanha esses vínculos e pode assentar a conversa após merge. Não vincule PRs
de tarefas a um coordenador permanente.

Crie um JSON privado com os IDs reais e o conjunto completo de PRs, por exemplo:

```json
{
  "dedupe_key": "entrega-encerramento-1",
  "objective_id": "objetivo-real",
  "task_id": "job-real-ou-tarefa-do-plano",
  "thread_id": "conversa-dedicada-real",
  "run_id": "run-real-da-tarefa",
  "policy": "all_merged",
  "authorization": {
    "actor": "operador",
    "reason": "Pedido explícito de encerramento desta entrega após o contrato",
    "scope": "dedicated_task",
    "covers_all_objectives": true
  },
  "pull_requests": [
    {
      "host": "github",
      "repository": "example/project",
      "number": 12,
      "base_branch": "develop"
    }
  ]
}
```

`host` e `repository` devem ser exatamente os valores canônicos retornados pelo
T3, inclusive quando o host não for GitHub. `task_id` resolve um job, uma tarefa
do plano ou uma delegação persistida. `job_id` é opcional e, se informado, precisa
concordar com essa resolução. A base é obrigatória; `head_sha` e `merge_sha` são
restrições opcionais. Quando exigidas, só passam com o SHA correspondente na
observação pública do T3. Se a versão instalada não expõe esse campo, o resultado
é bloqueado, sem inferir o commit por texto.

```bash
oraculo settlement bind PROJETO --file /caminho/privado/vinculo.json
oraculo settlement status PROJETO
oraculo settlement status PROJETO --binding-id ID
```

O protocolo também aceita `settlement.bind` e `settlement.status` em `request`.
O vínculo é imutável, deduplicado e auditado. Repetir o mesmo vínculo recupera a
intenção; reutilizar a identidade com outro conteúdo falha. O comando registra
intenção, sem alterar imediatamente a conversa. A reconciliação normal do motor
faz as leituras e, quando necessário, solicita o fechamento.

## Critérios e limites

- Todos os jobs do objetivo precisam estar `succeeded`, com contratos do plano
  satisfeitos e nenhum efeito ou execução incertos. Etapas não solicitadas no
  plano continuam sem comprovação.
- O conjunto de PRs observado deve coincidir exatamente com o registrado.
  Cada PR precisa estar `merged`, não rascunho, e na base esperada. PR aberto,
  fechado sem merge, abandonado, alvo divergente ou leitura ausente bloqueia.
- A conversa precisa pertencer ao projeto exato, manter o run autorizado
  concluído e estar sem atividade, perguntas ou descendentes pendentes.
  Títulos e referências em texto não servem como identidade ou prova de merge.
- A listagem pública permite verificar descendentes do mesmo projeto, com
  limites de 1.000 conversas e 100 descendentes. Falha de leitura ou limite
  excedido bloqueia. Dependências externas à árvore devem constar no plano;
  não autorize `covers_all_objectives` quando existirem objetivos não cobertos.
- As leituras públicas não são uma transação com a mutação. O motor relê a
  conversa antes do pedido e confere mudanças de run, revisão e itens. O T3
  continua responsável pelas regras de ciclo de vida e mensagens concorrentes.

O motor consulta o estado de host já acompanhado pelo T3. Não cria um segundo
watcher, não consulta o host em polling e não altera o banco do aplicativo.
PRs devem ser vinculados pela conversa responsável: o motor não cria vínculos
ausentes automaticamente, pois isso habilitaria fechamento pelo T3 antes de
validar todo o contrato. Um fechamento autônomo do próprio T3 com pendências
é registrado como observação, mantendo a política do Oráculo bloqueada.

## Estados e recuperação

`pending` significa intenção registrada; `blocked`, critério não satisfeito;
`requested`, pedido persistido/enviado; `uncertain`, pedido sem confirmação
observada; `confirmed`, leitura real de `settled:true` com os critérios
satisfeitos; `reopened`, mudança que invalidou a intenção antiga.

A resposta `settlesWhenTurnEnds:true` não é confirmação. Até uma resposta de
mutação contendo `settled:true` exige nova leitura. Se o T3 já assentou a
conversa, o motor só observa, sem repetir a mutação.

O pedido é reservado no SQLite do Oráculo antes de chamar T3. Reinício, timeout
ou resposta perdida preservam essa reserva e são reconciliados por leitura.
Como `t3_thread_organize` não oferece chave de idempotência, o motor nunca
reenvia automaticamente um pedido reservado. Uma falha antes do envio pode,
portanto, exigir intervenção humana, evitando duplicação após resposta perdida.

Em `uncertain`, consulte a conversa, o run e os eventos do vínculo. Se a leitura
posterior comprovar o fechamento correto, o motor confirma sozinho. Se a
solicitação foi interrompida ou não ocorreu, um operador deve verificar as
pendências e decidir um novo fechamento explícito no T3. Não edite a reserva
ou apague recibos para forçar retry.

Em `reopened`, a intenção antiga fica encerrada para automação: não existe loop
de settle/unsettle. O histórico permanece para uma nova decisão humana.
Nenhum comando desta integração reabre, arquiva ou exclui conversas, branches
ou PRs. Ferramentas opcionais ausentes bloqueiam apenas este fluxo; o contrato
legado do executor permanece disponível.

## Instalação e evidências

A migração 3 é aditiva: cria vínculos/eventos no estado do Oráculo e preserva
jobs, grants e slots da ORA-CONT-003, inclusive o padrão serial. Testes usam
projetos e PRs genéricos. Briefings, recibos T3 e evidências da instância ficam
fora do repositório público.

Para ativar, siga [Instalação](INSTALACAO.md). Validação do worktree não instala
o serviço permanente. Transferência exige nenhuma execução ativa/incerta, fila
controlada, backup consistente e rollback conjunto de código/estado, sem
segunda instância. Se o controlador estiver em uso, entregue o delta revisado
para ativação posterior.
