# ORA-PR-001 — Azure Repos e reconciliação de integração existente

Estado: implementação candidata, revisão e ativação pela origem.

## Objetivo e escopo

Permitir que tarefas positivas já aceitas observem um PR externo de integração,
inclusive já concluído, sem recriar trabalho, reescrever relatórios ou duplicar
PRs/merges. O mesmo PR pode pertencer a várias tarefas, preservando identidade,
execução, autorização e memória por tarefa. Nenhum dado de cliente ou recibo
operacional pertence a este documento versionado.

## Decisões de implementação

- Selecionar adapter pelo remote real; preservar GitHub e acrescentar Azure
  Services HTTPS. Recibos Azure mantêm ID, URL web canônica, estado do host,
  mergeStatus, commits source/target/merge, nomes e GUIDs de repositório/projeto,
  organização, fonte REST e instante observado.
- Git REST 7.1; avaliações de políticas 7.1-preview.1, conforme documentação
  primária Microsoft. Paginação limitada, timeout, rejeição de redirects e
  códigos de erro estáveis. Criação/merge incertos só reconciliam depois da
  intenção durável. O servidor aplica as políticas; nenhum bypass ou auto-merge.
- Associação explícita exige reconcile_only e ID/URL exatos. O relatório mantém
  seu commit individual. Todos os commits declarados precisam estar disponíveis
  e ser ancestrais do HEAD integrado; igualdade de árvore não é evidência disso.
- Native tracking/watch Azure não é habilitado pelo adapter sem suporte
  verificado. A limitação é metadado local, não ACK do host. REST observa o
  merge; o conjunto nativo completo também é verificado no settlement.
- Implantação opcional e separada, vinculada ao SHA de merge, ambiente e instante.
  Alegações com referências de prova permanecem reported. Não há verificador
  configurado capaz de promover uma alegação para confirmed.
- Usar somente PAT literal do .env selecionado ou referência explícita a
  serviço/conta Keychain existente. Não reutilizar o namespace OAuth do T3 nem
  inventar capacidade de segredo do host. Nunca executar o arquivo .env.
- Nenhuma migração nova: armazenamento aditivo no payload privado existente;
  schema, runtime manifest e política de migração permanecem inalterados.

## Validação e limites

Os testes novos usam Git, SQLite, serviço/socket e memória locais descartáveis;
Azure/T3 e seus estados são sintéticos. Um servidor HTTP loopback verifica o
bloqueio de redirecionamento sem credenciais reais. Cobertura inclui identidade,
escaping, paginação, policies/review/checks, HEAD/base exatos, credenciais/erros,
integração positiva/negativa, cinco tarefas compartilhando PR, ACK perdido,
idempotência, gates de settlement e deployment separado. Regressões GitHub,
notificações e suite completa são parte da entrega; comandos/resultados finais
e histórico de falhas corrigidas ficam no report.json privado do executor.

Não há certificação de Azure/T3 real, autenticação operacional, rollout, CI
pública ou ativação. SSH Azure, visualstudio.com legado e Azure Server ficam
fora do parser. Status Azure sem iteração comprovada não satisfazem um check
obrigatório. Não há watcher Azure automático nem fetch automático de commits.
Observações de Git/REST/T3 são sequenciais, sem transação distribuída; o HEAD
source é fixado na conclusão, enquanto mudança concorrente do target impede a
certificação quando detectada. Manter janela cooperativa sem novos runs.

## Retomada oficial

Após revisão/ativação autorizada pela origem, cada origem legítima usa os IDs
existentes com payload privado documentado em `docs/MANAGED_TASKS.md`:

1. `task publish` com reconcile_only, PR explícito, source binding e integration.
2. `task pr-check` com política de observação; `task settle` apenas para retomar
   finalização de merge já observado, com ACKs literais e gates frescos.
3. Opcionalmente `task deployment` com relato e recibos privados, sem promover
   relato para implantação confirmada.

O pai possui finish, memória final, revisão, publicação segura, merge e qualquer
instalação/ativação. O executor retorna commit limpo e relatório privado; não
faz operações no cliente, tarefas antigas, banco/vault vivos ou serviço global.
