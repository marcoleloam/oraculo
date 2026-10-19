# Validação pública da V1

A V1 funcional foi aceita em 19/10/2026. A versão do pacote é `0.1.0`; V1 identifica o primeiro escopo funcional entregue.

## Resultados

- Gate de aceitação da V1: **27/27 critérios aprovados**, sem pendências ou waivers; 24 critérios originais e três regressões complementares.
- Suíte automatizada: **180 testes aprovados**.
- Bateria de continuidade AT-020: **88 testes aprovados**, incluídos na suíte acima.
- Oito cenários de integração receberam aceite humano explícito no ambiente testado.

O gate foi produzido no ambiente de desenvolvimento. Conversas, credenciais, caminhos pessoais e evidências brutas desse ambiente não são publicados. Este resumo não é uma cópia do recibo selado nem substitui a validação de uma nova instalação.

## Escopos exercitados

- **AT-002 — provedores:** tarefas distintas com Claude, Codex e Grok pelas assinaturas existentes.
- **AT-003 — worktree:** base, branch e checkout conferidos; alteração não commitada da raiz preservada e ausente no filho.
- **AT-006 — memória:** recuperação de decisão, tarefa e aprendizado em sessão nova, com fontes e distinção entre histórico e estado atual.
- **AT-009 — entrega:** checks, merge e deploy reais em destino local descartável. Check falho bloqueou os efeitos; replays não autorizaram repetição. Não comprova uma instalação de produção.
- **AT-020 — continuidade:** janela do T3 fechada por 231,6 segundos, abrangendo a conclusão da implementação e o início automático da memória, com processos preservados e Obsidian fechado. Um ensaio separado comprovou reinício do daemon preservando identidades. A observação do iPhone foi humana.
- **AT-021 — Obsidian:** ingest/query/lint headless, revisão pelo plugin, isolamento de links, importação, proteção e conflitos. Fontes raw são projetadas como texto literal, mantendo os hashes originais.
- **AT-022 — proatividade:** início autônomo de melhoria e reavaliação sem duplicação. O aceite mantém o escopo limitado e não atribui continuidade autônoma às rodadas históricas que exigiram recuperação manual.
- **AT-024 — iPhone:** integração com o Mac aceita conforme teste e relato humano, sem telemetria independente do dispositivo.

## Preparação para publicação

O repositório público começa com um snapshot do código e dos arquivos publicáveis. O histórico privado de desenvolvimento permanece separado. O endereço MCP pessoal usado durante o desenvolvimento foi substituído por `https://t3.example.invalid/mcp`, que deve ser configurado por cada operador. Na cópia pública, a verificação de 19/10/2026 confirmou novamente 180 testes da suíte e 88 testes da bateria AT-020, sem falhas. Lint, formatação e simulação do instalador também passaram. A simulação não instala nem inicia o serviço.

## Reproduzir os testes locais

```bash
uv sync --locked --python 3.12
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q
uv run --frozen python scripts/test_at020.py
./scripts/install-service.sh --dry-run
```

Os testes locais usam serviços e transportes simulados quando necessário, além de processos reais nos cenários de lifecycle. Não consomem assinaturas para provar integrações externas. Para os ensaios reais, use o [roteiro de validação](VALIDACAO_ORACULO.md) e a [bateria AT-020](TESTES_AT020.md).
