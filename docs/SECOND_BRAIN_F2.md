# F2 — afirmações tipadas e migração segura

Implementação do contrato em [SECOND_BRAIN_CORE_CONTRACT.md](SECOND_BRAIN_CORE_CONTRACT.md).
O envelope de `MemoryProposal` e da página continua `schema_version: 1`. O domínio
tipado é opt-in: `knowledge_schema: 1` e `claims` no frontmatter. Ausência desses
campos significa legado **não avaliado**. Esta entrega não ativa o runtime, não
migra vault vivo e não promove afirmação a `supported`.

## API

`SourceSpan`, `KnowledgeClaim` e `validate_knowledge(content, project_id, files)`
estão em `memory/claims.py`. O publicador chama a validação depois de reunir as
fontes brutas e as páginas propostas, e antes de escrever. Achados informativos
viajam no recibo em `knowledge_findings`. Achado com `severity: error` recusa a
publicação.

`SourceSpan` usa `path`, `sha256`, `start`, `end` e `quote`. Os offsets são bytes
UTF-8, início inclusivo e fim exclusivo, e precisam cair em fronteira de caractere.
O hash e os bytes citados são conferidos contra raw imutável do projeto ou de
`shared` já permitido. Página derivada só entra na cadeia se ela própria alcança
raw sem ciclo e sem voltar à página que a cita. A síntese não corrobora a si mesma.

`KnowledgeClaim` guarda identidade local ao projeto (`id`, `project_id`), `text`,
`kind` (`fact`, `hypothesis`, `question`), `category` (`observation`, `decision`,
`preference`, `inference`) e `support` (`not_evaluated`, `source_linked`,
`supported`, `insufficient`, `contested`, `retired`). Evidência, avaliação,
`observed_at`, `recorded_at`, `valid_from`, `valid_until`, `supersedes`,
`subject`, `predicate` e `value` são opcionais. Data ausente permanece
desconhecida; lint temporal fica com a fase de atualidade.

Fato tipado exige evidência que alcance raw. Hipótese e pergunta podem registrar
ausência. `supported` exige avaliação com `verdict`, `reviewer`, `reason` e
`assessed_at`, e o veredito precisa concordar com o estado. Citação mecanicamente
correta não concede `supported`. `insufficient` e `contested` permanecem mesmo
quando o trecho confere. A avaliação é atribuída e auditável; não é prova da
verdade externa.

## Migração

`plan_migration(publisher, project_id)` só lê. O plano é determinístico e traz
`plan_hash`. `apply_migration(publisher, plan)` exige `approval` igual a esse
hash. Cada lote precisa ter de 1 a 20 mudanças e no máximo 2 MiB serializados.
Esse teto é conferido antes de validar o modelo, de ler journals ou de escrever.
Pedido malformado, inclusive lote com 21 mudanças, devolve `MemoryError` com
código curto e não repete o conteúdo da página. Lotes aceitos passam pelo
Publisher, com `operation_id` estável. Replay devolve o recibo existente.
Journal preparado é concluído pelo recover do publicador. A imagem anterior
fica no journal para rollback; esta fase não executa restauração automática
nem migração no vault vivo.

Página legada não revisada recebe somente `knowledge_schema: 1` e `claims: []`.
Legado aqui significa ausência das duas chaves. Toda outra chave do frontmatter,
o corpo, `page_id`, `schema_version` e `reviewed` permanecem. O estado resultante
é `not_evaluated`. A única diferença admitida depois da escrita é `updated`,
quando o Publisher carimba a data de hoje. Página `reviewed` entra em
`pending_review` e não é editada. Página já tipada não é reescrita nem
rebaixada: `not_evaluated`, `source_linked`, `insufficient`, `contested`,
`supported` e `retired` ficam como estão. `knowledge_schema` diferente de 1
não é rebaixado. Página que sozinha estoura 2 MiB fica em `deferred`. Raw não
entra nos lotes e não é escrito. Edição concorrente que muda o hash-base
recusa o lote antes de escrever.

## Limites

O validador não julga significado além da avaliação explícita que recebeu. Não
escolhe vigência, não resolve contradição semântica e não percorre wikilinks
como se fossem evidência. A revisão humana continua protegendo `reviewed`.
Integrar serviço, CLI e captura é a frente de F5/F6.
