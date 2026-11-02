# F3 e F4 — compilação incremental e atualidade

Contrato de comportamento de `memory/compiler.py` e `memory/freshness.py`.
A integração em serviço, CLI e validação combinada de claims fica com a
frente de F5/F6. Estes módulos não publicam sozinhos fora de `Publisher`,
não abrem rede, não apagam raw e não acoplam falha de compilação ao
encerramento de uma tarefa.

Os exemplos usam projetos sintéticos (`alpha`, `beta`). Identidade de
projeto é a string exata: `alpha` e `Alpha` são conceitos distintos. Em
volumes que não distinguem maiúsculas no caminho, os dois não podem ser
inicializados como diretórios ao mesmo tempo; a recusa de escopo continua
sendo o prefixo da string, antes de ler o arquivo.

## F3 — `compile_knowledge`

```python
compile_knowledge(
    publisher,
    project_id,
    sources,
    *,
    concept,
    title=None,
    version="1",
    apply=False,
) -> dict
```

`sources` é uma lista de caminhos raw já existentes, no máximo 20, sem
duplicar o mesmo caminho. Aceita somente
`{corpus}/raw/{sha256}.md` do projeto ou `shared/raw/{sha256}.md`. O nome
do arquivo tem de ser o digest do conteúdo. Página de wiki ou de conceito
é `source_not_raw`. Caminho com `..`, prefixo de outro projeto ou fora
desse padrão é `source_outside_scope` e o arquivo não é lido. Ausência,
hash divergente e encoding inválido são `source_missing`,
`raw_hash_mismatch` e `invalid_source_encoding`.

`concept` é a atribuição explícita do operador. Não se infere conceito,
projeto ou suporte a partir do relatório. Dois projetos com o mesmo texto
de conceito não compartilham página. `Queue` e `queue`, e `café` em NFC e
NFD, também permanecem páginas distintas.

A página estável é `c` mais os primeiros 40 hex de
`digest(project_id + "\\0" + concept)`, em
`{corpus}/wiki/concepts/{project_key}--{page_id}.md`.

### Identidade da operação e no-op

O `operation_id` é `cmp-` mais os primeiros 40 hex do digest canônico de
projeto, conceito exato, versão do compilador e a união ordenada de
`(path, sha256)`. A versão entra na identidade; mudar a versão gera outra
operação sem duplicar claims já extraídos.

Se a versão registrada coincide e o conjunto pedido é subconjunto das
fontes já gravadas, o estado é `noop` com o `operation_id` armazenado e o
achado `sources_already_incorporated`. Fontes acumuladas não são
removidas. Com `apply=True`, o no-op chama `publisher.recover()` para
fechar um journal preparado cuja resposta se perdeu, sem segunda escrita
lógica. Se a página já contém a impressão digital da operação e a
resposta se perdeu, a repetição é esse no-op.

Raw registrado ausente ou com hash divergente devolve `conflict` e
`recorded_source_missing`. O histórico não é descartado para seguir só
com as fontes novas.

### O que entra na página

De um objeto JSON, ou do objeto em `evidence`, promovem-se apenas
`summary`, `decisions`, `statements`, `claims` e `limitations`.
`next_steps`, `pending`, `checks`, `results`, `changes`, `lessons`,
`operations`, `artifacts`, `criteria`, `instructions`, `content` e `body`
viram achado `not_promoted`. Fonte sem objeto JSON vira uma hipótese
`Fonte sem relatório estruturado.` e o achado `unstructured_source`.

Cada claim traz os campos do contrato: `id`, `project_id`, `text`, `kind`,
`category`, `support`, `evidence` (`path`, `sha256`, `start`, `end`,
`quote`), `assessment`, `observed_at`, `recorded_at`, `valid_from`,
`valid_until`, `supersedes`, `subject`, `predicate` e `value`. O suporte
inicial é só `source_linked`, quando o quote existe como bytes no raw, ou
`not_evaluated`. `assessment` permanece `None`. Suporte ou avaliação
declarados na fonte são ignorados (`support_not_accepted`,
`assessment_not_accepted`). Texto que pede para ignorar instruções, trocar
de papel ou ocultar algo do curador vira hipótese `not_evaluated` e
`instruction_not_authority`. `project_id` ou `concept` dentro da fonte não
redirecionam a compilação (`source_directive_ignored`).

`recorded_at` só entra se o relatório traz `recorded_at` ou `finished_at`
em data ou instante com fuso. `updated` da página e o relógio do processo
não observam a afirmação. Data ilegível é omitida (`unparsed_date`).

Dois valores explícitos para o mesmo `subject` e `predicate` ficam
`contested`. Nenhum vira verdade. `supersedes` é uma lista de ids de
claims. Vazia quando não há substituição. Um id único no relatório entra
como lista de um elemento. Se essa lista aponta para outro id do mesmo
par, os dois valores permanecem e o achado é `explicit_supersession`.
Suporte `retired` não volta a ativo. Fato sem trecho verificado desce a
hipótese `not_evaluated`.

A claim publicada cabe em `KnowledgeClaim`: sem campos extras, `supersedes`
sempre lista, e cada evidência só com `path`, `sha256`, `start`, `end` e
`quote`. Offsets são bytes UTF-8 do raw selecionado. Fato com quote
mecânico fica `source_linked`; hipótese sem span fica `not_evaluated`.
O compilador não emite `supported`.

Uma segunda compilação do mesmo conceito acrescenta a fonte nova e os
spans novos. Aliases, tags e a seção `Contexto anterior` já presentes
permanecem. O corpo anterior sem compilador entra uma vez nessa seção.

`created` e `updated` do conteúdo proposto ficam em `1970-01-01` só como
envelope da página, para o hash do journal não depender do relógio. Esse
placeholder não é copiado para `observed_at`, `recorded_at`, `valid_from`
nem `valid_until`. Data de claim desconhecida permanece `null`. Uma data
`1970-01-01` só aparece na claim quando a fonte a declarou. O `Publisher`
substitui `created` e `updated` na escrita e não revalida a afirmação.

### Estados

| Estado | Quando |
|---|---|
| `proposed` | `apply=False` e a página não está reviewed. Inclui `proposal`. |
| `confirmed` | `apply=True` e `Publisher.publish` devolveu recibo. |
| `noop` | Fontes pedidas já incorporadas na mesma versão. Sem `proposal`. |
| `suggestion` | Página `reviewed`. Inclui `suggestion` e não chama `publish`. O conteúdo sugerido conserva `reviewed: true`. |
| `conflict` | CAS, operação reutilizada com outro payload, identidade da página ou raw registrado inválido. Não há segunda tentativa de escrita. |
| `rejected` | Página ambígua ou proposta acima de 20 páginas ou 2 MiB. |

`ambiguous_concept_page` e `page_identity_conflict` param antes de
propor. `limits` sempre declara tetos, `semantic_certification: false`,
suportes padrão, `network: false`, `canonical_gc: false`,
`task_completion_coupled: false` e `auto_ingest_concept: false`. A página
de conceito gerada não é aceita como raw.

`apply` precisa ser bool. Conceito, título e versão inválidos levantam
`MemoryError`. `base_commit` da proposta é `None`. `batch_id` é o
`operation_id`. `source_hashes` é a união completa. Citações são
wikilinks dos raw, nunca da página de conceito.

## F4 — `freshness_audit`

```python
freshness_audit(files, project_id, *, now=None) -> list[dict]
```

A função só lê o mapa `files`. Caminhos fora de `{corpus}/` e `shared/`
entram apenas na contagem `out_of_scope_ignored`, sem o texto. Página cujo
`project_id` não é o auditado nem `shared` é erro `page_project_mismatch`.
Página de conceito, entidade ou fonte sem `knowledge_schema` e sem claims
é `legacy_not_evaluated`.

Cada claim produz um registro `claim_status`. Três tempos permanecem
separados:

- `observed_at` é o instante observado declarado na claim.
- `recorded_at` é o instante em que o relato foi registrado.
- vigência é `valid_from` / `valid_until` quando foram fornecidos.

`updated` da página muda `page_updated` e nunca `observed_at`, suporte ou
`current`. O achado carrega `updated_revalidates: false`. Sem janela de
vigência, `current` é `None`: ausência não significa atual. Sem `now`, a
comparação não corre (`validity_not_compared`) e nada é declarado
expirado. Com `now` consciente, `current` só é verdadeiro enquanto o
instante cai na janela. Data sem hora em `valid_until` inclui o dia civil.
`now` ingênuo levanta `MemoryError("naive_now")`. Instante sem fuso na
claim é `timezone_unknown`, severidade `insufficient`, e não é comparado.

### Classes

| Severidade | Uso |
|---|---|
| `error` | Falha mecânica: página inválida, data inválida, quote que não é `raw[start:end]`, fato sem evidência. |
| `contradiction` | Mesmo projeto, conceito, sujeito e predicado exatos com mais de um valor. `winner` é `None` e `newer_wins` é `false`. |
| `insufficient` | Fuso desconhecido, homônimo não fundido, contexto que não basta para decidir. |
| `alert` | Fonte rejeitada ou aposentada ainda ligada a claim ativa (`retired_source_supports_active_claim`). |
| `record` | Estado, limites da auditoria, contagem fora de escopo. |
| `history` | Substituição explícita. O anterior fica `current: false`; evidência, links e raw permanecem. |

Agrupar contradição usa a string exata. Homônimos por caixa ou Unicode
(NFKD, marcas combinantes removidas, casefold) só geram
`homonym_not_fused`. Projetos diferentes não se fundem. Se algum registro
do grupo traz em `supersedes` um id do grupo, não há
`possible_contradiction`. A claim guarda essa lista. A leitura também
aceita um id legado em string, sem gravá-lo de novo nessa forma. Cada id
substituído gera um achado `supersession`.

Preferência não gera achado de expiração por inatividade. Só uma janela
explícita compara vigência. O último item é `audit_limits`, com `network`,
`canonical_gc`, `garbage_collected`, `updated_revalidates` e `newer_wins`
falsos.

### Proposta de curadoria

`build_review_proposal(files, project_id, decisions) -> dict` não escreve
e não usa `now` para carimbar datas. Ações: `retire` (suporte `retired`,
evidência intacta), `reobserve` (exige `observed_at`; se o suporte era
`supported`, desce a `source_linked` e `assessment` continua vazio),
`pointer` (hipótese nova, `supersedes` lista com o id antigo, evidência
vazia) e `supersede` (texto novo obrigatório; o texto antigo permanece).
A proposta reescreve as claims no formato validado: lista `supersedes`,
spans só com os cinco campos e datas explícitas ou `null`. O envelope
`1970-01-01` não vira data observada nem registrada. `supported` sem
avaliação compatível não é reemitido como sustentação semântica.

Decisão inválida, claim desconhecida ou ambígua, ou campo obrigatório
ausente, rejeita a proposta inteira. Lista vazia é `noop`. Página
`reviewed` tocada devolve `suggestion` sem chave `proposal`; o conteúdo
sugerido mantém `reviewed: true`. O `operation_id` (`cur-` mais digest)
depende das bases, do conteúdo renderizado e do projeto, então dois
textos diferentes não colidem. `source_hashes` reaproveita wikilinks de
raw já presentes no frontmatter e no mapa `files`. Links e evidências
anteriores ficam. `limits.raw_deleted` e `limits.canonical_gc` são falsos.

## O que estes módulos não fazem

A saída já segue `KnowledgeClaim` e passa em `validate_knowledge` quando
os spans apontam para o raw selecionado. Estes módulos não enfraquecem
essa validação, não adicionam comando de CLI e não publicam fora do
`Publisher`. Não há ingestão automática da página gerada, GC canônico,
rede, nem acesso ao vault operacional.
