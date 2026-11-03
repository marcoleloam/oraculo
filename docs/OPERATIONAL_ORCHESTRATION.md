# Operação verificável — 0.3.0

## Seleção e autorização

O Router determina todas as opções elegíveis antes de Decisions: assinatura
verificada, modelos autorizados, catálogo vivo, cota, capacidade e gates do State.
Um alvo explícito no payload do job nunca é substituído. No managed, `target`,
`model_selection` e o modelo herdado do composer também não são substituídos.
Sem modelo fixado, um task nativo pode selecionar entre as opções autorizadas.
Tarefas independentes mantêm o contrato explícito existente, sem ranking novo.

`active` e `assisted` têm a mesma autoridade limitada: escolher uma alternativa
que o Router já permitiu. Ambos executam a baseline quando não recebem resposta
útil. Uma/nenhuma opção, policy/grant bloqueado ou fonte não verificável não
disparam API. Na aplicação, o serviço refaz catálogo, capacidade, política e
HEAD. A escolha da própria baseline não é registrada como `applied=true`.
No managed, mudança posterior de fonte/política após reservar mantém o efeito
bloqueado e pede reconciliação da mesma intenção; não cria outro lançamento.

`ci.observe` busca evidência no host real. Falhas com sinais ambíguos podem
produzir classificação; sinais únicos/ausentes usam classificação determinística.
A classificação vira somente uma feature de ranking posterior de uma execução
já autorizada. Não autoriza retry, cria sucessor, aprova CI, amplia escopo ou
aprova grant/merge/deploy/outcome. Revisão semântica continua independente.

As alternativas têm descrições úteis por traits fechados configurados pelo
operador, sem transmitir nomes/IDs do modelo. Por exemplo, para um modelo já
autorizado em `providers.codex.models`:

```toml
[providers.codex.routing_traits."MODELO_AUTORIZADO"]
capability = "verification" # general/coding/reasoning/research/verification
latency = "fast" # standard/fast/deliberate
```

Esses traits não autorizam modelo novo. Descrições arbitrárias do catálogo e
instruções de clientes não são transmitidas. Sem traits, a preferência ordinal
determinística continua sendo a informação disponível para ranking.

## Configuração de Decisions

Exemplo para o orçamento autorizado de US$1/dia UTC e US$0,10 por tarefa lógica:

```toml
[decisions]
enabled = true
mode = "assisted" # "active" tem a mesma autoridade limitada
model = "gpt-6-luna"
credential_reference = "ref:oraculo-openai-decisions"
daily_budget_microusd = 1000000
task_budget_microusd = 100000
max_calls = 80
max_concurrent = 2
deadline_seconds = 3

[decisions.tariff]
version = "openai-decisions-2026-11-03-upper-v1"
authorization_reference = "ref:operator-decisions-budget"
bound_reference = "ref:reviewed-byte-framing-rate-bound"
input_nanousd = 125
multiplier_milli = 2200
framing_tokens = 1024
output_nanousd = 0
```

As referências devem apontar para a autorização e revisão reais do operador;
copiar os nomes não fornece essa prova. `run_budget_microusd` é compatibilidade
com a sombra 0.2 e limita somente `shadow`; active/assisted usam a tarefa lógica.
O contador de chamadas também é diário e por tarefa, incluindo todos os contextos.
O State principal do daemon é compartilhado por managed, grants, triagem e canário.

O [guia oficial](https://developers.openai.com/api/docs/guides/decisions),
a [referência create](https://developers.openai.com/api/reference/resources/decisions/methods/create)
e os [preços](https://developers.openai.com/api/docs/pricing), consultados em
03/11/2026, documentam `POST /v1/decisions`, `gpt-6-luna`, opções choice com
`value` e `description` e possibilidade de refusal. Uncached input custa
US$0,10/M tokens e cache write 30m US$0,125/M; output de Decisions não é cobrado.
O exemplo usa 125 nanoUSD/token, multiplicador superior explícito 2 × 1,10 e
output zero declarado, abrangendo cache 30m, contexto e adicional regional.
Revalidar esse bound ao mudar contrato, contexto, região ou tarifa. A validação
active/assisted recusa tarifa abaixo desse bound e output zero apenas implícito.

Reserva = `ceil((bytes JSON UTF8 + framing_tokens) * input_nanousd *
multiplier_milli / 1000000)` microUSD. Bytes e framing são um contrato superior
revisado, não tokenização medida nem limite de cobrança imposto pela OpenAI.
Usage acima do bound, schema desconhecido, refusal, timeout ou envio incerto
preservam a reserva inteira. Nunca há reenvio de uma intenção sent/unknown.
TTL é 5 min; deadline de recepção/validação/consumo inicial é 3 s. Circuito e
kill são duráveis; desligar/religar invalida respostas antigas por geração.

A resolução de credencial é lazy e somente em memória, usando Keychain macOS:
service `oraculo.openai`, account `decisions-api`. Não usar valor em argv, env,
arquivo, chat, log ou mensagem de erro. Testes não abrem esse Keychain. Os
requests contêm somente índices locais e features fechadas; prompts, memória,
paths, IDs, nomes de modelos/provedores e logs privados não são enviados.

## CI e outcomes

`Service.ci_observer` usa `RepositoryPRLifecycle` por padrão. GitHub usa o login
já configurado em `gh`, sem aceitar evento ou booleano do caller. O projeto é
ligado a um remoto Git real cujo host/repository correspondem exatamente ao
binding. Sem nome fixado, deve haver um único remoto correspondente (por exemplo,
`public-safe`); dois aliases correspondentes são ambíguos e recusados. Um remoto
explicitamente fixado permanece imutável; nunca se cria/renomeia `origin`. PR HEAD/base, run/attempt, check suite e jobs são
reconsultados. Checks obrigatórios podem estar em workflows distintos: cada
check é ligado à sua suite/run/attempt e ao mesmo HEAD/PR. Políticas vêm de
branch protection e applicable rules autenticadas; acesso ausente bloqueia.
O adapter suporta checks GitHub Actions; status externo, required workflow de
ruleset ou paginação acima de 100 itens ficam inconclusivos. Não existe
`allow_no_checks` no caminho operacional. O lifecycle managed também compara
checks/review solicitados com a política atual antes do merge autorizado.

Azure reutiliza autenticação/REST existentes. O caminho aceita builds cujo
sourceVersion é exatamente HEAD, tentativa demonstrada pelo timeline e política
bloqueante com buildDefinitionId/context.buildId correspondentes, mais status
policies observáveis. Builds de merge sintético ou metadados insuficientes
ficam bloqueados. Não há polling Azure adicional; usa eventos/pr-check existentes.

Os ingressos usam `Envelope(schema_version=1, request_id, project_id, origin,
kind, payload)` pela CLI `oraculo request --file ENVELOPE.json`:

- `ci.observe`: `{ "job_id": "..." }`, pelo coordenador. Nenhum payload de evento.
- `outcome.verify`: `{ "attempt_id": "...", "generation": 1,
  "criterion_id": "...", "cwd": "..." }`, pelo executor com grant vivo.
- `decisions.canary`: `{ "task_id": "..." }`, pela origem da tarefa managed.
  Envia apenas features sintéticas e usa o mesmo accounting dessa tarefa no
  daemon. `applied=false`; não lança execução nem concede qualquer efeito.

Um critério CI declara `outcome: {version: 2, kind: "ci", commit: SHA,
ci_binding: {repository, pr, head, base, run, attempt}}`. Não declara argv nem
artefato do caller. `outcome.verify` precisa observar CI no host através do
adapter real instalado, com fonte Git limpa no SHA exato antes/depois. No consumo,
revalida HEAD, receipt hash, cursor e frescor máximo de 5 min. Observações falhas,
outro SHA/attempt, review pendente ou conflito invalidam o gate. Critérios
semânticos precisam de review independente e suas provas de serviço, como antes.

## Rollout local e atualização

Configuração de destino utilizável, sem script externo:

```toml
[rollout_targets.local]
runtime_root = "~/.local/share/oraculo"
```

O serviço usa `oraculo.local_rollout.probe`, manifesto/inventário verificados,
LaunchAgent/processo observado e `runtime.probe` pelo socket autenticado local.
O endpoint liga o processo ao manifesto do interpretador instalado no startup;
não confia em identidade enviada pelo caller. A CLI read-only equivalente é
`python -m oraculo.local_rollout --root ROOT --config CONFIG --environment local`.
Essa CLI faz uma sonda; o lifecycle `rollout.verify` executa as duas (>=5 s) e
produz o recibo com validade 120 s. Mudança de processo, config, target, artefato,
fonte ou tarefa invalida o resultado. O relatório/merge e uma alegação deployment
`reported` prévia continuam obrigatórios. O serviço nunca infere deploy de merge.

Manifestos usam `source_commit` = SHA público integrado que gerou o artefato.
Se diferente do commit reportado pela tarefa, sele o bundle usando também
`--delivery-source-commit SHA_DO_REPORT`. Esse vínculo é parte do manifesto
revisado; não é editável pelo payload de rollout. O ambiente vem do destino
configurado. Liveness/readiness não apagam `dispatch_attempts_exhausted` nem
limpam reservas. O diagnóstico permanece no recibo e no status.

A origem deve parar/carry com a config antiga; ativar a release nova ainda com
essa config até terminar o guard/journal antigo; somente então, offline, aplicar
patch atômico da configuração autorizado; iniciar com nova config e carry fresco.
Nunca alterar journals/config no meio de operação antiga. Não existe atalho pelo
verificador de rollout. Não restaurar DB antigo para evitar a barreira 7.

Nenhum fluxo acima migra, reconstrói ou recertifica memória antiga. Leitura/query
e terminal write continuam no ciclo de memória existente, com scope, hash,
origem e canonical publication preservados. Um report narrativo não substitui
recibo de escrita final; a origem conclui o lifecycle depois do retorno terminal.
