# Roadmap Second Brain — F0 a F6

O código inclui as sete fases e o cofre de leitura unificado. Instalação, ativação, habilitação de rotinas, migração e aceite de corpus são decisões separadas por ambiente.

1. **F0 — Avaliação:** corpus sintético bilíngue congelado e baseline reproduzível.
2. **F1 — Recuperação:** busca lexical com orçamento, cobertura, abstenção e isolamento por projeto.
3. **F2 — Evidências:** afirmações tipadas, proveniência e plano de migração com confirmação explícita.
4. **F3 — Compilação:** conceitos incrementais, idempotência e publicação pelo publicador canônico.
5. **F4 — Atualidade:** auditoria de validade, conflitos e propostas de curadoria.
6. **F5 — Uso:** captura selecionada, consulta, fontes integrais, salvar explicitamente e relatório pelo CLI.
7. **F6 — Rotinas:** manutenção limitada e opt-in, contexto autenticado para execução noturna e piloto com distinção entre simulação e aceite real.

O [cofre unificado](SECOND_BRAIN_UNIFICADO.md) apresenta todos os projetos autorizados, tarefas/subtarefas e biblioteca. O Canvas e o grafo nativos do Obsidian usam cores por projeto. Sugestões de conexão são rotuladas e separadas das relações confirmadas.

## Guias

- [Como usar](SECOND_BRAIN_USO.md)
- [Contratos do núcleo](SECOND_BRAIN_CORE_CONTRACT.md)
- [Avaliação e recuperação](SECOND_BRAIN_F0_F1.md)
- [Afirmações e migração](SECOND_BRAIN_F2.md)
- [Compilação e atualidade](SECOND_BRAIN_F3_F4.md)
- [Captura, consulta e rotinas](SECOND_BRAIN_F5_F6.md)
- [Gate sintético](SECOND_BRAIN_SYNTHETIC_GATE.md)

## Verificação

```sh
uv sync --frozen
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q
uv run --frozen python -B scripts/second_brain_release_gate.py --output /tmp/second-brain-gate.json
```

O gate usa dados sintéticos e ambientes descartáveis. Não migra o vault operacional, não ativa serviços, não instala conectores e não promove memória privada entre projetos.
