# Second Brain — gate sintético de release

O gate sintético torna o código tecnicamente elegível para avançar de fase; não comprova utilidade humana nem ativa o serviço. Pilotos com relógio injetado ficam em `accepted_simulation`; aceite real exige relógio real.

## Três fases, sem misturar

| Fase | Quem executa | O que prova |
|---|---|---|
| Pré-ativação sintética | `scripts/second_brain_release_gate.py` | Lifecycle virtual, regressões selecionadas e o benchmark congelado no HEAD em execução |
| Ativação no host | origem, depois deste gate | Quiescência, backup, instalação do bundle, diagnóstico de `dispatch_attempts_exhausted`, corpus real só se o usuário escolher |
| Fumaça pós-ativação | origem, somente leitura | Serviço já trocado responde; este runner não a inicia |

O recibo pode marcar `technically_eligible_for_next_phase` e, ao mesmo tempo,
`activation_performed: false`. As condições de host ficam em `gaps` com
`not_executed`. Isso não é uma falha do gate sintético e não autoriza instalar
sozinho.

## Comando

```bash
uv run --frozen python -B scripts/second_brain_release_gate.py \
  --output /tmp/second-brain-synthetic-gate.json
```

O processo espera cada comando no foreground, com timeout, e termina. Não há
`sleep`, cron, inferência paga nem espera de calendário. Recusa sobrescrever o
JSON. `--plan` grava os comandos com status `not_executed` e elegibilidade
falsa. `--expect-head SHA` recusa começar se o HEAD divergir.

O recibo traz, por verificação: comando, status derivado do exit, exit,
duração, SHA-256 do log e SHA-256 do artefato. Exit 0 com skip, recall abaixo
do piso ou artefato ausente vira `failed`. O runner não assina contagens que
não leu do artefato.

## Casos obrigatórios

Fixture `tests/fixtures/second_brain/synthetic_gate_lifecycle.json`.

- Relógio virtual `2026-11-02T03:00:00+00:00`, 30 dias, fuso UTC, hora noturna 2,
  semana na segunda (`weekday` 0). O artefato repete `clock: virtual_injected_clock`.
- Dois projetos homônimos `atlas-harbor` e `atlas-ridge`, rótulo de exibição
  Atlas só na fixture (`fixture_host_metadata` / `not_live_host`). Shared
  sanitizado. O projeto `alpha` do serviço descartável permanece de fora.
- Semente do benchmark congelado: `20261101`, manifesto `second-brain-f0-v1`.
  Este gate não reescreve rótulos nem escolhe outro ranker.
- Códigos sintéticos públicos `HQ-17-CARMINE` e `RQ-44-INDIGO`.
- Rotina nasce desligada. `additional_spend_allowed` permanece falso.
- Adiamento por capacidade zero e, depois do backoff de 300s no relógio
  virtual, por cota. Nenhum objetivo nasce nesses ticks. Nenhum executor parte.
- 30 agendamentos noturnos e 5 semanais (segundas 02, 09, 16, 23 e 30 de
  novembro de 2026). A semanal é `lint` somente leitura. A noturna seleciona
  no máximo 20 raw do próprio projeto e no máximo 2 MiB; o arquivo acima de
  2 MiB fica de fora.
- Replay do mesmo dia devolve o mesmo job. Chaves de dedupe não se repetem.
- No último dia virtual, `grant.acquire` / `grant.claim` reais no serviço
  descartável. A proposta noturna sem claims tipadas é recusada. `routine-cancel`
  marca `effects_may_be_uncertain`, não apaga canônico e não abre escrita nova.
- Piloto com o mesmo relógio: antes de 7 dias recusa; no dia 7 uma revisão útil
  não aceita; no dia 14 o aceite fica `accepted_simulation`. A rotina não liga
  sozinha por causa do piloto.
- Consulta `knowledge.query` com limite 20, 6.000 tokens de detalhe e 800 de
  brief. Orçamento inválido (32.001) é recusado. A consulta não move o HEAD.
  Fato sem evidência é recusado. Hipótese explícita sem `--apply` não grava e
  não nasce `supported`.
- Raw, página `reviewed` e shared permanecem byte a byte.

O teste `tests/test_second_brain_synthetic_gate.py` não usa skip. Skip nesse
arquivo, ou junit com failure/error/skip, reprova o gate.

## Benchmark congelado

O runner chama o medidor existente, sem alterar pesos:

```bash
uv run --frozen python -B scripts/second_brain_eval.py \
  --output /tmp/second-brain-f0-f1.json \
  --scales 50,500,5000
```

Por escala de aceite, no mesmo JSON:

- as três escalas 50, 500 e 5.000 são obrigatórias, sem duplicações;
- recall@10 do candidato ≥ 0,90 e o `acceptance_gate.passed` já calculado;
- regressão de idioma vazia (queda máxima registrada 0,02 contra o baseline
  daquele idioma);
- MRR do candidato ≥ MRR do `legacy_bundle` **da mesma corrida**;
- recall e MRR finitos no intervalo [0, 1]; `latency_ms_p95` finito e não
  negativo. O número não é SLA e não escolhe ranker.

Uma corrida parcial com `--scales` serve para desenvolvimento e não libera
avanço de fase. Os testes adversariais recusam escala ausente ou repetida e
métricas NaN, infinitas ou negativas.

`choice.constant_matches` precisa ser verdadeiro: o ajuste na escala 50 continua
mandando no ranker congelado. A corrida histórica f1-v2 (recall 1,0, MRR
0,1111, p95 268,70 ms em 5.000 páginas) é contexto, não certificado desta
execução. Recall não é acurácia semântica de resposta.

## Regressões selecionadas

Uma invocação, sem skips admitidos:

- `tests/test_second_brain_evaluation.py`
- `tests/test_memory_retrieval.py`
- `tests/test_memory_scope.py`
- `tests/test_knowledge_routines.py`
- `tests/test_knowledge.py`
- `tests/test_knowledge_service.py`

Pacote, `launchctl`, vault vivo e o daemon instalado não entram. A origem faz
a instalação e a quiescência.

## Parar e reverter

O runner para de considerar elegível quando qualquer comando obrigatório estoura
o timeout, sai diferente de 0, traz skip, perde o artefato, fica abaixo do
recall, regride idioma, fica com MRR abaixo do baseline da corrida, ou quando
o HEAD muda no meio. Não há retentativa.

Reversão da automação, no produto já existente e no cenário virtual: `routine-cancel`
desliga ticks novos e pede cancelamento dos objetivos. Trabalho já reivindicado
permanece com efeito incerto. Raw, reviewed, journal e a história do piloto
ficam. Não há `git reset` de vault real neste gate.

Depois de um recibo verde, a origem ainda precisa da janela de quiescência, do
bundle e da fumaça somente leitura. Sem corpus ao vivo escolhido pelo usuário,
a ativação controlada não seleciona um projeto cliente.
