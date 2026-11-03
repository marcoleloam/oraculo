# Decisions: prontidão local e recuperação conservadora (0.3.1)

Use estes comandos na instalação do daemon, com sua configuração autorizada.
Eles passam pelo socket e protocolo oficiais; não abra nem edite o SQLite.
O socket mantém a proteção local existente. `prepare`, `status` e `acknowledge`
exigem operador local (`origin` sem thread/run/message); não são poderes de
um executor. Nenhum deles chama a API paga, cria grant ou altera políticas.

```sh
oraculo --config CONFIG decisions status --project PROJECT
oraculo --config CONFIG decisions prepare --project PROJECT
```

`status` não lê o Keychain. Mostra accounting global, entradas do projeto com
`entry_digest`, charge/reserva, tarefa lógica, dia e elegibilidade de recuperação.
O estágio de credencial é a última observação em memória deste daemon, não uma
sonda atual nem uma garantia de conectividade. Reiniciar descarta essa observação.

`prepare` é uma verificação explícita local: resolve a referência fixa
`ref:oraculo-openai-decisions`, descarta o valor e retorna apenas estado,
reason/etapa fechados e `api_calls=0`, `tokens=0`. Não reserva dinheiro nem
altera schema. Disabled, kill ou budget zero não resolvem credencial; tarifa
não configurada também impede preparação. Unknown financeiro não impede
preparar localmente, mas continua impedindo uma nova chamada paga.

O backend usa `/usr/bin/security find-generic-password` para service
`oraculo.openai`, account `decisions-api`, via subprocesso assíncrono. O valor
fica somente no pipe privado e memória do processo; não vai para argv, env,
arquivo, log, resposta de operador ou cache. Stderr é descartado. Não há
`keyring.to_thread` neste caminho, retry, `.env` ou credencial do cliente.
A leitura tem limite de 2 s; timeout/cancel mata e tenta recolher o processo
por até 250 ms. O deadline global da decisão continua 3 s, incluindo reserva,
credencial, HTTP, validação e persistência; o pequeno recolhimento pode ocorrer
após o prazo e nunca permite consumir uma resposta atrasada.

Se a preparação retornar `access_denied_or_missing`, `timeout` ou
`backend_unavailable`, o recibo orienta a ação local: no Acesso às Chaves do
macOS, desbloquear o chaveiro de login, localizar o item acima e conferir o
acesso do `/usr/bin/security`; autorizar apenas o acesso necessário quando o
macOS solicitar e repetir `prepare`. O backend não altera ACL nem escolhe
“permitir todos os aplicativos”. Ausência do item e ACL negada compartilham
um reason seguro porque não lemos stderr para diferenciá-los. Um timeout não
prova a causa exata. Este comando não é um canário HTTP.

## Reconhecer a reserva integral de um desconhecido

Uma chamada unknown conserva toda a reserva e a chave nunca é reenviada.
A recuperação registra **separadamente** que o operador aceita contabilizar o
limite superior inteiro para sempre. Não confirma custo real nem sucesso HTTP.
A entrada, resultado, evento original, charge, tarefa/dia UTC e contagem de
chamadas permanecem intactos. Um unknown histórico sem estágio continua
`stage=unknown`; diagnóstico posterior não reescreve o envio anterior.

1. Obtenha `decisions status`. Revise a entrada, seu `entry_digest` atual e a
   evidência privada sem copiar segredos. Não confunda a chave da decisão com
   o digest completo da entrada.
2. Crie um JSON privado de reconhecimento como abaixo. Substitua as referências
   pela identidade e evidência reais; o hash é SHA256 da evidência revisada.
   Referência e hash são uma declaração auditada do operador: o daemon não abre
   um arquivo arbitrário nem atesta a verdade dessa evidência.
3. Execute `decisions acknowledge --project PROJECT --file ACK.json` e depois
   `decisions status`. O mesmo payload tem replay idempotente; qualquer alteração
   exige revisão. Digest antigo ou identidade reutilizada é recusado.

```json
{
  "key": "CHAVE_64_HEX",
  "expected_entry_digest": "DIGEST_ATUAL_64_HEX",
  "idempotency_key": "ref:operator-upper-ack-unique",
  "actor_reference": "ref:responsible-operator",
  "reason": "conservatively_accounted_upper_bound",
  "evidence_reference": "ref:reviewed-local-diagnosis",
  "evidence_sha256": "SHA256_EVIDENCIA_64_HEX"
}
```

```sh
oraculo --config CONFIG decisions acknowledge --project PROJECT --file ACK.json
```

Somente unknown terminal com tarifa e contrato de output zero explícitos,
bound finito, request íntegro, reserva integral e razão elegível pode receber
ack. Schema/usage/refusal desconhecidos, usage acima do bound, persistência
incerta, contabilidade corrompida, intent/sent ou artefato alterado não podem.
Um intent/sent expirado é conservado como unknown pelo engine ao admitir uma
nova intenção; nunca se converte uma operação em voo por mero ack.

SQLite serializa CAS, deduplicação, evento auditado
`decisions_unknown_upper_acknowledged` e atualização derivada do bloqueio.
Todos os unknowns, inclusive de outros projetos, precisam estar reconciliados;
o operador não fornece nem limpa um booleano. Circuito, kill, orçamento,
limites diários/por tarefa e gate de concorrência continuam valendo. Uma nova
intenção autorizada pode seguir depois de todos esses gates; nunca repita o
canário anterior para “testar novamente” a mesma chave.

Apenas um ack bem-sucedido promove o State principal ao schema **8**. Abertura,
status e preparação não migram bases antigas; o ledger managed não é promovido.
Um runtime/manifesto limitado a 7 deve recusar a base promovida. Instale 0.3.1
com manifesto que suporte 8 antes do ack. Rollback requer código compatível,
sem restaurar banco antigo nem remover o marcador de schema. Nenhuma decisão
anterior ou tarefa settled é reaberta/recertificada.

## Etapas e canário real

O engine persiste `before_http` antes da preparação, `possible_send` antes de
entrar no cliente HTTP e `response` ao receber headers. Crash/timeout conservam
a última etapa; erro genérico não prova ausência de envio. Transportes opacos
são classificados conservadoramente como `possible_send`. Nenhuma etapa
restitui reserva ou transforma unknown em sucesso.

O canário real continua sendo `decisions.canary` com a origem managed existente,
no mesmo State e budget da tarefa. Preparação disponível, testes com MockTransport
e CLI/socket de laboratório não certificam API real, faturamento ou rollout.
O operador/origem executa qualquer canário novo somente após a publicação e
recuperação autorizadas, sem reenviar a intenção anterior.
