# F0/F1 — avaliação congelada e recuperação com orçamento

A avaliação usa 60 perguntas bilíngues, dois projetos e um escopo compartilhado sintéticos. Há recortes de ajuste e aceite congelado, nas escalas de 50, 500 e 5.000 páginas. O baseline usa `Context.bundle`, não uma aproximação do algoritmo.

```sh
uv run --frozen python -B scripts/second_brain_eval.py --output /tmp/second-brain-evaluation.json
```

O recibo registra métricas, configuração, versões e limitações da execução atual. A fixture congelada fica em `tests/fixtures/second_brain/manifest.json`.

`memory/retrieval.py` fornece busca lexical com orçamento de detalhe e resumo. O projeto é filtrado antes do ranqueamento; shared entra apenas quando permitido. A resposta inclui cobertura, abstenção, índice, fallback, bytes e páginas selecionadas. Fontes maiores que o orçamento recebem referência para leitura integral autorizada, sem truncar o original.

O estimador de tokens é baseado em bytes UTF-8 divididos por quatro; não é um tokenizer do provedor. Não há embeddings ou chamada paga implícita. Passar o benchmark sintético não equivale a aceitar o corpus real.
