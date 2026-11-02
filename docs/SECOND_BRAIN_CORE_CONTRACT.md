# Contratos do núcleo Second Brain F0–F6

Recuperação, evidência, compilação e rotinas usam os contratos a seguir; dados operacionais e implantação pertencem a cada instalação.

## F0/F1: recuperação

`retrieve(context, project_id, query, *, limit=20, detail_tokens=6000,
brief_tokens=800) -> dict` em `memory/retrieval.py` devolve o envelope compatível
com `Context.bundle`, acrescido de `selection` e `brief`. `selection` informa
estimador, orçamento, bytes/páginas lidos e selecionados, cobertura, abstinência,
índice/fallback, referências integrais para fontes acima do orçamento e versão.
Nenhum documento é cortado silenciosamente. O índice é derivado, separado por
corpus e invalidado pelo hash do conjunto de arquivos. Escopo precede ranking,
cache, snippets e expansão de links. `brief` e detalhes têm orçamentos separados.

`Context.bundle` conserva uma forma explícita de executar o baseline legado real
para o benchmark. O caminho orçamentado deve ser integrado à consulta de memória
real na fonte; não basta uma função de demonstração sem consumidor.

Corpus sintético em 50, 500 e 5.000 páginas, pelo menos 60 casos bilíngues e os
tipos previstos no roadmap, ajuste separado do aceite congelado. Relatório com
recall@10, MRR, contexto/bytes e latência p50/p95, incluindo resultados negativos.
O conjunto de aceite não serve para escolher pesos. Metas são as do roadmap.

## F2: afirmações e fontes

`MemoryProposal` e o envelope de página continuam v1 para compatibilidade. O
domínio tipado novo é opt-in por `knowledge_schema: 1` e `claims: [...]` no
frontmatter. A ausência desse domínio significa legado **não avaliado**.

`SourceSpan` tem `path`, `sha256`, `start`, `end` e `quote`. Os offsets são bytes
UTF-8, início inclusivo e fim exclusivo, em raw imutável do projeto selecionado
ou shared já permitido. Hash, limites e bytes citados são conferidos. Síntese ou
saída derivada não é corroboração independente de si mesma.

`KnowledgeClaim` tem `id`, `project_id`, `text`, `kind` (`fact`, `hypothesis`,
`question`), `category` (`observation`, `decision`, `preference`, `inference`),
`support` (`not_evaluated`, `source_linked`, `supported`, `insufficient`,
`contested`, `retired`), `evidence: list[SourceSpan]`, `assessment` opcional,
`observed_at`, `recorded_at`, `valid_from`, `valid_until`, `supersedes`, e os
campos opcionais `subject`, `predicate`, `value`. Datas ausentes são desconhecidas.
IDs são locais ao projeto, nunca autoridade para unir projetos.

Fato novo tipado exige fonte válida; hipótese/pergunta pode registrar ausência.
`supported` exige também avaliação semântica explícita com `verdict`, `reviewer`,
`reason` e `assessed_at`; validação de bytes não concede esse estado. O julgamento
continua uma alegação atribuída e auditável, não prova automática da verdade.

API pública: `validate_knowledge(content, project_id, files) -> list[dict]` e
modelos `SourceSpan`/`KnowledgeClaim` em `memory/claims.py`. Publisher valida o
conteúdo tipado antes de escrever; fontes fora de escopo e citações circulares
são recusadas. Auditoria de legado não torna fatos antigos suportados.

Migração: `plan_migration(publisher, project_id) -> dict` e
`apply_migration(publisher, plan) -> dict`, em `memory/migration.py`. Dry-run
estável, hashes de base, aprovação explícita do plano no apply, operações
deduplicadas e Publisher como único escritor. Raw, IDs e reviewed preservados;
páginas reviewed recebem pendência, sem edição automática.

## F3/F4: compilação e atualidade

`compile_knowledge(publisher, project_id, sources, *, concept, title=None,
version="1", apply=False) -> dict`, em `memory/compiler.py`, usa somente raw
explicitamente selecionado, até 20 fontes e proposta de 2 MiB. `concept` é a
atribuição explícita do operador; não se infere entidade a partir de outro
projeto. Duas tarefas podem atualizar a mesma página com evidências acumuladas.
Identidade e no-op dependem do projeto, hashes das fontes e versão do compilador.
Retorno contém `state`, `operation_id`, `proposal` quando aplicável, `findings` e
limitações. Semântica incerta fica `source_linked` ou `not_evaluated`, nunca
`supported` por padrão. Reviewed recebe sugestão. Divergência é preservada,
concorrência gera conflito e execução repetida não publica de novo.

`freshness_audit(files, project_id, *, now=None) -> list[dict]`, em
`memory/freshness.py`, separa erro mecânico, possível contradição e contexto
insuficiente. `updated` não é observação; recência não escolhe a verdade. Vigência
somente quando declarada. Preferência não expira por falta de acesso. Substituição
e contestação ficam visíveis; não há rede automática nem GC canônico.

## F5/F6: operação e piloto

Comandos de captura selecionada, consulta somente leitura, salvar resposta,
auditar/revisar e relatório são integrados ao serviço oficial. Plano de captura
identifica destino e arquivos antes da coleta; apply exige confirmação daquele
manifesto. Arquivo com instrução maliciosa permanece dado, sem autoridade.
Conversas mistas e seleção não confirmada não são ingeridas automaticamente.

Rotinas começam desligadas. Nightly limitada a fontes pendentes e revisão
semanal somente leitura usam scheduler/admissão, grants, dedupe e backoff
existentes. Capacidade zero adia; cancelamento interrompe novos efeitos sem
apagar o histórico. Nenhum cron, inferência paga ou executor fora do serviço.
Métricas registram volumes e latências, sem conteúdo privado em logs gerais.

O piloto registra início, duas revisões semanais e aceitação/interrupção do
curador. Um ensaio acelerado comprova a lógica, mas não representa duas semanas
operacionais decorridas. Ativação, migração no vault vivo e piloto real têm
recibos próprios e não serão declarados por testes sintéticos.
