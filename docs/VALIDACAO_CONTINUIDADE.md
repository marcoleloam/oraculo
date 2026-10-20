# Validação da continuidade de execução

Atualização de 20/10/2026: planos persistentes com tarefas e dependências, critérios
por etapa, contextos T3 próprios, reconciliação e concorrência limitada de 1 a 4
executores (padrão 1). Ingestão de memória não conclui implementação pendente.

A suíte passou 335 testes tanto no checkout de desenvolvimento quanto na cópia
preparada para publicação. O lint também passou nos dois checkouts. A revisão independente
resolveu três achados. Um ensaio controlado exercitou duas tarefas e uma revisão
em contextos T3 distintos; o serviço instalado depois concluiu um plano serial
com três filhos, uma tentativa por tarefa e sucessão sem comando de continuação.
Reinício preservou o resultado sem duplicidade. Isso não substitui validação
da instalação do operador ou aceite dos projetos atendidos.

Os recibos operacionais e contextos de clientes permanecem privados. Durante o
ensaio, um executor relatou enumeração de nomes fora do projeto; as instruções
foram reforçadas e revisadas. A restrição de escopo dos agentes é declarativa,
não uma sandbox. O ajuste de texto não foi exercitado por novo executor real.

Reprodução local:

```sh
uv sync --locked --python 3.12
uv run --frozen pytest -q
uv run --frozen ruff check src tests scripts
```

Consulte [operação](OPERACAO.md#planos-executáveis-ora-cont-003) e o
[plano de exemplo](tasks/ORA-CONT-003-example-plan.json).
