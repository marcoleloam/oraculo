# Oráculo 0.3.0

Esta minor conecta Decisions à admissão real, instala observação CI autenticada
no serviço e oferece um destino de rollout para a própria instalação local.
Código/testes, publicação, merge, ativação e canário pago exigem recibos distintos.
Este documento não certifica execução externa ou ativação.

- `grant.acquire` seleciona somente modelos elegíveis por assinatura, catálogo,
  capacidade, escopo e política. `active` e `assisted` podem ordenar alternativas;
  alvo explícito e composer permanecem imutáveis. Sem ambiguidade não há chamada.
  A resposta não concede grant, autoriza efeitos ou comprova outcome.
- Tarefas managed nativas sem modelo fixado podem usar a mesma seleção. Um
  composer fixado conserva seu modelo. A intenção de lançamento continua única,
  com memória, capacidade e parentage verificados pelo lifecycle existente.
- Accounting usa o State principal, dia UTC global e projeto+tarefa lógica,
  inclusive triagem, contextos, retentativas, canários e restart. Limites máximos:
  1.000.000 microUSD/dia e 100.000 microUSD/tarefa. Isto é admissão local, não um
  hard cap de billing da OpenAI. Custos incertos conservam reservas e bloqueiam
  chamadas seguintes. Não existe reset para liberar orçamento incerto.
- `Service` instala `RepositoryCIObserver`, via `gh`/`RepositoryPRLifecycle`, e
  Azure REST quando o host expõe binding verificável de build/política. Os
  ingressos existentes e o outbox são reutilizados; nenhum novo watcher existe.
- `OutcomeContract(kind="ci")` exige observação autenticada exata e recente do
  host. Relatório narrativo, commit limpo ou assinatura de payload do caller não
  comprovam testes. Os contratos de comando/artefato e revisão semântica
  independente permanecem disponíveis, sem atestação retroativa dos legados.
- O destino `runtime_root` verifica inventário/manifesto, fonte, merge, artefato,
  processo e configuração com duas sondas separadas por pelo menos 5 segundos.
  Liveness e readiness são distintas do diagnóstico de backlog legado. A view e
  o gate invalidam recibos expirados ou cuja configuração mudou.

Defaults continuam desligados (`enabled=false`, `mode=off`, orçamento diário
zero, sem tarifa/credencial autorizadas). Não há necessidade de API paga nos
testes, CI ou instalação. Siga a [configuração operacional](../OPERATIONAL_ORCHESTRATION.md)
para habilitação explícita e o [runbook de instalação](../INSTALACAO.md).

O ceiling agora é **7**. Main legado fica em 4 e ledger managed em 5;
recursos anteriores continuam promovendo lazy para 6. A primeira escrita de
accounting operacional, rota managed aplicada, CI do host ou outcome CI promove
o banco correspondente a 7, atomicamente. 0.2.0, limitado a 6, recusa esse banco.
Prepare um bundle novo com `--schema-min 4 --schema-max 7`; não altere manifesto
imutável, restaure banco antigo ou tente recertificar memórias F0–F6.

O template de CI mantém os nomes públicos `Tests (ubuntu-latest)`,
`Tests (macos-latest)` e `Orchestration offline gate`, com timeout 30 min e
JUnit/durations. Os templates privados em `templates/ci` só são exportados ao
checkout público sanitizado; não publicar a história privada.

Os testes novos usam transportes OpenAI/GitHub/Azure e controle launchd de
laboratório, com Git, SQLite, sockets e artefatos descartáveis reais. Canário
pago, CI público, permissões remotas e rollout live permanecem operações da
origem. Não confundir essas provas com uma aprovação da suíte local.
