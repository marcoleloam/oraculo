# ORA-CLI-001 — Limites de resposta da CLI e dos probes

## Objetivo e estado

Versionar e incorporar o reparo existente para consultas agregadas maiores que
um envelope de entrada, preservando as duas etapas da correção e a separação
entre código integrado, cliente de recuperação e versão padrão ativa.

Estado observado em 2026-10-25: incorporação local concluída, validação focal
aprovada; publicação e integração na branch principal ainda pendentes.
O fluxo gerenciado de revisão aguarda capacidade. A ativação padrão não ocorreu.
Este documento é um registro Git sanitizado; não comprova encerramento de tarefa,
gravação canônica de memória, merge remoto ou rollout.

## Causa

`status`, `task list` e `doctor` falhavam com
`Separator is not found, and chunk exceed the limit`. O daemon respondia;
os clientes recusavam respostas agregadas superiores a 1 MiB.
Na investigação original, respostas no transporte tinham 1.201.958 bytes
(status) e 2.538.481 bytes (lista).

Havia dois limites independentes: o leitor assíncrono da CLI e
`ServiceController.request`, usado pelos probes síncronos de saúde. Corrigir
somente o primeiro recuperou consultas, mas deixou o probe limitado.

## Solução e rastreabilidade

A base original do reparo é
`be18295db7357fa2baf9ab306e63110020c909a2`.
Os commits originais foram preservados e reaplicados com `cherry-pick -x`;
não houve recriação da implementação.

1. `991f2f158fd188dc1e3fe4393ee64266ba57c6d6` —
   `fix(cli): allow bounded aggregate responses above request limit`.
   Define `MAX_RESPONSE_BYTES = 16 * 1024 * 1024` e aplica esse limite ao
   buffer e à validação da resposta da CLI.
2. `f7ee421631d2f1ee966aa910260f5afb81d3f150` —
   `fix(service): share aggregate response limit for health probes`.
   Usa o mesmo limite na leitura síncrona do controlador de serviço.

`MAX_ENVELOPE_BYTES` permanece em 1 MiB para requisições. Respostas acima de
16 MiB continuam sendo rejeitadas; a mudança não é truncamento nem leitura
ilimitada. Correlação e validação JSON permanecem exigidas. Não há alteração
do daemon ativo, de schema, reservas, configuração ou capacidade neste reparo.

Arquivos de implementação: [protocol.py](../../src/oraculo/protocol.py),
[cli.py](../../src/oraculo/cli.py) e
[service_control.py](../../src/oraculo/service_control.py).
Regressões: [test_cli_response_limit.py](../../tests/test_cli_response_limit.py)
e [test_service_control.py](../../tests/test_service_control.py).

## Incorporação contra a base atual

A base local de incorporação
`638e5e5013c68024dfff09f96a3cb0f2cc5aef9b` já contém o agrupamento de
notificações. A base pública segura é
`25b752f11b76e13dbded6b6f68e9ed7991f03b00`. As duas aplicações foram
concluídas sem conflitos textuais, em worktrees isolados.

- Branch local: `review/client-response-limit-20261025`.
  Etapa CLI: `c500e69e0135d150708ffee4f0cbad8748e4b8dd`;
  etapa probe: `48a34262973d8e1dcabe2084ed8615baf77899b5`.
- Branch preparada sobre a história pública: `fix/client-response-limit`.
  Etapa CLI: `228be884e32ddc528ec6140ec031a38c60feb00b`;
  etapa probe: `e34a8babf731a3a7675ad255459f96caea34040f`.
- A revisão encontrou somente um ajuste de formatação no decorador do teste.
  Foi corrigido separadamente, sem alterar o comportamento:
  `b927b2c3521593231b6885b1b8b8e6e927383b61` local e
  `c76672f409ecdb50da64c4e3bf4d421eb0dfc9a8` na história pública preparada.
  A verificação de formatação foi repetida e passou.

O checkout principal e a branch do reparo original foram preservados.
Preparar uma branch de história pública não equivale a push, PR ou merge.

## Validação

Reparo original: **40 passed, 5 skipped**, Ruff e `git diff --check` aprovados.
Os skips dependem de integração launchd; não foram promovidos a aprovação.

Incorporação atual: **57 passed, 5 skipped**, nas suítes
`test_cli_response_limit`, `test_service_control`, `test_cli`,
`test_protocol` e `test_notification_delivery`.
Ruff, formatação dos cinco arquivos e `git diff --check` aprovados.
Os testes exercitam sockets descartáveis, respostas acima de 1 MiB,
fronteira exata de 16 MiB, rejeição acima da fronteira e manutenção do limite
de requisição. Não constituem validação funcional de outros projetos.

Consultas somente leitura com a CLI do código incorporado:
`status`, `task list`, `doctor` e `service status`, todas com exit 0.
A identidade do daemon permaneceu a da release anterior; não houve restart.
Tamanhos de arquivos JSON formatados na saída da CLI não devem ser confundidos
com as medições originais dos frames no transporte.

Histórico de recuperação preservado: a primeira tentativa ainda falhava no probe
síncrono; a segunda etapa corrigiu esse consumidor. Houve rejeição inicial de
manifesto com SHA abreviado, corrigida com o SHA completo antes da instalação.
Recibos, logs integrais, snapshots e evidências de host permanecem privados.

## Recuperação separada e rollout

A release verificada `client-response-fix-f7ee421` fornece o comando
`oraculo-recovery`, recuperando as consultas sem substituir o daemon.
Isso é diferente de ativar a versão padrão: o ponteiro e o serviço padrão
continuam na release anterior na observação registrada.

A implantação requer artefato revisado, quiescência atual comprovada,
manutenção pela interface oficial e probes posteriores de identidade e saúde.
Nenhuma edição de release imutável, banco, reserva ou capacidade é um meio
válido para forçar essa etapa.

## Critérios de encerramento

- [x] Causa e duas etapas originais registradas com hashes completos.
- [x] Aplicação local e aplicação sobre história pública segura sem conflitos.
- [x] Regressões focais, lint, formatação e whitespace aprovados.
- [ ] Revisão gerenciada concluída quando houver vaga, com memória confirmada.
- [ ] PR publicado, checks do HEAD público e integração remota comprovados.
- [ ] Se houver rollout autorizado: quiescência, ativação padrão e probes
      confirmados em recibos separados; caso contrário, manter rollout pendente.

Não anexar snapshots, IDs de conversas, dados de clientes ou evidências privadas
a esta tarefa publicável. Um commit deste registro não satisfaz os itens pendentes.
