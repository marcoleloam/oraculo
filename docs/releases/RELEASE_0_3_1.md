# Oráculo 0.3.1

Correção do caminho Decisions após espera do backend Python Keychain: o backend
agora usa `/usr/bin/security` assíncrono, cancelável e limitado, com pipe privado,
sem cache, segredos em argumentos, ambiente, arquivos ou logs. Preparação local
explícita e status seguro passam pelo daemon; nenhum deles chama a API paga.

A recuperação oficial de unknown registra acknowledgement auditado do limite
superior integral, com digest esperado, CAS e replay idempotente. Preserva a
entrada/resultado original, reserva, tarefa/dia e contagem para sempre. Bloqueio
é derivado de todos os unknowns não reconciliados. Schema/usage inválidos,
persistência incerta e corrupção continuam bloqueados. Deadline de 3 s, HTTP
sem retries, explicit/composer, baseline e kernel/grant/merge/deploy permanecem.

Leia [o procedimento de recuperação](../DECISIONS_RECOVERY.md) antes de operar.
O ack promove **somente o State principal** ao schema 8. Use manifesto com teto 8;
um runtime 0.3.0 (teto 7) não pode reabrir estado promovido. Não há migração em
massa nem reescrita de tarefas settled. Rollback requer runtime compatível,
nunca restauração de banco antigo.

Validação de desenvolvimento usa processos/socket/SQLite descartáveis e
transportes HTTP de laboratório. Não representa execução de API paga, acesso à
credencial real ou ativação de produção. Publicação/CI remoto/merge/ativação e
qualquer novo canário permanecem na operação responsável.
