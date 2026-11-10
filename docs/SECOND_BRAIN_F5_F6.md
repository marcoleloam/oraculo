# Second Brain — captura explícita, consulta e rotinas

Contratos de operação do CLI `oraculo brain`. Rotinas são opt-in; instalação e ativação do serviço são etapas separadas.

## Capturar somente arquivos selecionados

Todos os comandos exigem o ID registrado em `--project`. O serviço resolve o
diretório autorizado; o cliente não abre o SQLite nem o vault vivo.

```sh
oraculo brain capture-plan --project PROJETO --source-file /projeto/nota.md --output /tmp/plano-captura.json
oraculo brain capture-apply --project PROJETO --file /tmp/plano-captura.json --confirmation SHA_DO_MANIFESTO
```

O primeiro comando usa somente metadados dos arquivos selecionados. A resposta
declara projeto, caminhos, tamanho, identidade, datas do arquivo e
`manifest_sha256`; `content_collected` é falso. Examine o conjunto e o destino
antes de confirmar o SHA exato. O segundo comando revalida **todo** o manifesto,
recusa mudanças e symlinks e coleta somente `.md`/`.txt` dentro desse projeto.
Links dentro do projeto e links no arquivo final são recusados. Aliases de
diretórios acima da raiz registrada, como `/tmp` no macOS, são normalizados
somente quando apontam para a mesma raiz autorizada. Limites: 20 arquivos e
2 MiB no total. As fontes raw preservam os bytes UTF-8.
Repetições retornam os mesmos snapshots/recibos. Uma falha no publicador pode
deixar um subconjunto de snapshots imutáveis já confirmado; repetir é seguro,
não há promessa de transação única para os 20 arquivos.

Texto fornecido explicitamente pode usar um payload JSON com `text`,
`source_kind` e `sanitized`. `capture-plan` retorna o manifesto e
`capture-apply` recebe esse mesmo texto e a confirmação. O envelope RPC limita
texto inline a menos de 1 MiB; use arquivos selecionados para conteúdos maiores.
Export de conversa exige `source_kind: selected_chat` e `sanitized: true` após
seleção/sanitização humana. Isso não autoriza ler a conta ou histórico inteiro.
Não há conector, OCR, áudio, PDF nem varredura de diretórios nessa interface.

## Consultar, salvar e revisar

```sh
oraculo brain query --project PROJETO --query 'qual decisão sobre filas?'
oraculo brain save --project PROJETO --file /tmp/resposta-selecionada.json
oraculo brain save --project PROJETO --file /tmp/resposta-selecionada.json --apply
oraculo brain audit --project PROJETO
oraculo brain report --project PROJETO --output /tmp/relatorio-novo.json
```

`query` retorna contexto recuperado com orçamento, referências e abstinência;
não inventa uma resposta quando falta suporte. Não modifica o canônico e não
inicia inferência. Métricas limitadas são guardadas no estado privado, sem texto
da pergunta ou fonte em logs gerais. O orçamento pode ser fornecido no payload:
`limit` até 100, `detail_tokens` até 32000, `brief_tokens` até 4000; padrões
20/6000/800. A estimativa de tokens continua `utf8_bytes_div_4_v1`.

Uma fonte acima do orçamento fica em `context.selection.oversize`, com caminho
e hash. Para ler seus bytes completos, copie `path` e `sha256` para um JSON e use
`oraculo brain source --project PROJETO --file /tmp/referencia.json`.
O serviço confere o hash e o mesmo escopo projeto/shared; mudanças são conflito.
Conteúdo grande usa o mesmo transporte paginado e verificado, sem recorte de raw.

O JSON de `save` contém `title`, `text` e `evidence`. Cada evidência inclui
`path`, `sha256`, `start`, `end`, `quote`, com offsets em **bytes UTF-8** de uma
fonte raw do projeto ou shared autorizado. Uma resposta factual sem evidência é
recusada; para uma hipótese explícita use `as_hypothesis: true`. A resposta vira
conceito derivado com categoria `inference`, nunca corroboração independente de
si mesma. `source_linked` significa ligação mecânica conferida; não certifica
que a fonte implica semanticamente a conclusão. O salvamento exige `--apply`;
sem isso o comando só retorna uma proposta. Páginas `reviewed` devolvem pedido
de revisão. Novas respostas não são ingeridas automaticamente como raw.

`compile --file JSON [--apply]` recebe `sources` e `concept` explícitos e usa F3.
`review --file JSON [--apply]` recebe `decisions` e usa o construtor curatorial
de F4. `audit` e `report` são somente leitura canônica. O relatório apresenta
afirmações, estados de suporte, datas e contagens por estado, sem percentual de
qualidade inventado. Saídas grandes usam artefatos paginados e verificação de
hash/projeto. `--output` cria um arquivo privado novo e recusa sobrescrita.
Para leitura no Obsidian/painel, continue usando `experience sync`: a projeção
mostra todos os projetos autorizados, tarefas e subtarefas pelos nomes.

## Migração de páginas legadas

```sh
oraculo brain migration-plan --project PROJETO --output /tmp/migracao.json
oraculo brain migration-apply --project PROJETO --file /tmp/migracao.json --confirmation SHA_DO_PLANO
```

Revise o dry-run, fontes, limites e before-images antes de confirmar
`plan_hash`. A migração marca somente páginas legadas e não converte links
antigos em afirmações sustentadas. Claims já tipadas, raw e páginas revisadas
permanecem protegidos. Ver [F2](SECOND_BRAIN_F2.md) para CAS, lotes, replay e
preparação de rollback. O plano não autoriza restaurar o vault por Git reset.

## Rotinas opt-in e piloto

```sh
oraculo brain routine-status --project PROJETO
oraculo brain routine-configure --project PROJETO --file /tmp/config-rotina.json
oraculo brain routine-cancel --project PROJETO
oraculo brain pilot-start --project PROJETO --file /tmp/piloto-selecionado.json
oraculo brain pilot-review --project PROJETO --file /tmp/revisao-curador.json
oraculo brain pilot-status --project PROJETO
```

A configuração contém `enabled` booleano explícito, `timezone_name` (padrão
UTC), `nightly_hour` (2), `weekly_weekday` (0=segunda), `max_sources` (1–20) e
`max_pending_jobs` (1–3). Iniciar um piloto não habilita a rotina. O tick do
serviço só consulta projetos registrados/permitidos com `enabled: true`.

A rotina noturna seleciona até 20 raw pendentes do próprio projeto, até 2 MiB.
Raw já referenciado por conceitos não é selecionado de novo. O conteúdo shared
continua disponível para consulta, mas não é automaticamente incorporado como
novo pendente privado. A revisão semanal enfileira `lint` somente leitura. Jobs
usam os mesmos grants, reservas, cota e modelos de assinatura do serviço; não
há cron nem inferência paralela. Enfileirar não inicia um executor por si só.

O despacho não trata a consulta vazia da rotina como recuperação. Com
`strict_abstain` desligado, um corpus que ainda cabe em 6000 tokens de detalhe
pode devolver páginas pela compatibilidade
`query_unsupported_scoped_corpus_fits_budget` mesmo com abstenção
`no_useful_terms`. Quando o conjunto legado passa desse orçamento, a mesma
abstenção deixa `bundle.documents` vazio. Um teste só com poucas páginas não
alcança esse defeito.

Na ingestão sem termos úteis, o bundle é um recorte da operação: índice do
projeto, páginas canônicas que citam o raw congelado, `reviewed`, hashes
exatos e `base_commit`. A citação usa o `resolve_link` do publicador, com a
página de origem e os caminhos do projeto e de shared. Só entra o link que
resolve de forma unívoca para esse raw: caminho completo, `[[stem]]`,
`[[arquivo.md]]` ou caminho relativo à página. Link ambíguo, inseguro ou fora
desse raw não seleciona a página. O que não cabe no orçamento fica como
referência com hash, sem corte de bytes e sem abstenção de consulta.
`canonical_pages` conta o cânone próprio. `relevant_canonical_pages` conta só
as páginas próprias que citam o raw congelado. Sem página canônica própria a
cobertura é `empty_canon`. Se esse cânone existe e nenhuma página cita o raw,
a cobertura é `no_relevant_bases`; a página não citada não é copiada. Entrada
congelada inválida — caminho não canônico, fora do raw próprio ou shared,
ausente, diferente do disco, hash diferente do declarado ou do nome do
arquivo, acima de 2 MiB, ou mais de 20 fontes — levanta `MemoryError` antes
do bundle. O hash declarado não é reescrito. `read_only` no bundle de
ingestão repete a flag do job; o lint fica sempre `read_only`. A recusa de
`memory.propose` para consulta, lint e job `read_only` continua no serviço.
A revisão semanal executa o lint determinístico do corpus do projeto e
devolve contagens, achados e limites. Projeto sem raw e sem página canônica
fica `no_content`. Isso não é lint limpo. Consulta sem termos úteis continua
abstendo no caminho orçado, e uma ingestão que traz termos úteis continua
usando esse caminho. A rotina não promove shared, não se aprova sozinha e não
altera raw alheio. Shared citado pode ser lido; não entra como base mutável.

A rotina noturna também grava no artefato do job um recibo compacto
`prepared_compilation` (`f6-prepared-compilation-v1`). O serviço o calcula com o
compilador em `apply: false` e o conceito explícito do payload, ou
`Compilação noturna incremental` quando o payload não traz conceito. O recibo
não contém o corpo da proposta nem cópia do cânone não relacionado. Traz o
alvo, a existência da página, `base_sha256`, `reviewed`, conflitos de alias no
cânone próprio, a cobertura e o `registry_base_commit` do bundle, o manifesto
congelado das fontes e o hash canônico do preview. `proposal_base_commit` nulo
é o contrato atual do compilador: a base global é opcional e o publicador
continua recusando página ou fonte divergente. Esse nulo não se substitui pelo
commit do registry. Status `ready` autoriza o executor a repetir
`brain compile` sem `--apply`, conferir os metadados e só então usar
`memory propose` no grant. `blocked`, `review_required`, `deferred` e `noop` não publicam.
Alias em conflito, página revisada, fonte inválida e metadado
preparado pelo chamador fecham em recusa. Conceitos explícitos continuam
recusando propostas acima do limite. Conceitos antigos que não são o alvo
não precisam estar no bundle. A construção do contexto não escreve o vault.

A página do conceito padrão não cresce sem limite: quando a proposta ultrapassa
2 MiB, o compilador preserva integralmente a página anterior e prepara um lote
novo, com nome/ID derivados do manifesto ordenado de fontes e hashes. Tenta o
lote completo e reduz por metades, sempre por fontes inteiras; uma fonte que
não cabe sozinha não impede a tentativa das seguintes. `target_concept` é o
conceito do lote, enquanto `concept`/`lookup.payload` conservam a rota original.
`compiled_sources` declara o subconjunto proposto; `deferred_sources` declara
caminhos, hashes e motivo dos adiamentos. O manifesto congelado continua
vinculando toda a seleção. Nenhum adiamento é marcado como incorporado.
A seleção noturna verifica se cada raw cabe integralmente em um lote vazio:
fontes individualmente grandes ficam em `selection.deferred_sources`, sem
ocupar as 20 vagas nem impedir fontes posteriores. Se só há adiamentos, a
rotina retorna `deferred_sources` sem criar um ingest condenado ao mesmo limite.
Essas fontes exigem revisão explícita; o produto não as trunca nem altera raw.
Um compile explícito só com essas fontes retorna `deferred`, nunca `noop`.
`not_promoted` conta campos de relatório preservados como dados (por exemplo,
checks), não páginas recusadas; não é erro impeditivo. Em rejeição por tamanho,
o código do recibo é `proposal_too_large`, mesmo com achados informativos antes.

Publicação noturna via `memory.propose` registra intenção durável em ciclo
próprio, ligado à tentativa, job, objetivo e thread/run. O ciclo só fica `saved`
com recibo `confirmed` e commit do publicador. A proposta persistida permite
reconciliar perda da resposta pelo journal sem gerar outro raw ou outra página
source, evitando realimentar a fila noturna. Isso registra a publicação mecânica,
não aceite do corpus ou certificação semântica. Não retropreenche ciclos antigos.
Menções literais a `reviews/` em claims e suas evidências são dados; referências
canônicas de navegação a reviews continuam recusadas pelo publicador.

Cobertura de contexto, transporte concluído ou `no_changes` sobre zero
documentos não são aceite de corpus. As execuções anteriores que abstiveram ou
fecharam sem inspeção permanecem falhas. A prova desta correção é sintética.
Capacidade/cota zero ou catálogo indisponível adiam explicitamente, com backoff
de 300 segundos e sem fallback de custo. O marcador diário/semanal e a chave
durável de objetivo impedem duplicação, inclusive após perda de resposta.

Queries e jobs lint não podem publicar via `memory.propose`. Jobs noturnos
exigem claims tipadas não vazias antes do publicador. Evidências de claims novas
ficam restritas à lista raw congelada do job; claims antigas só permanecem fora
dessa lista quando seu conteúdo é preservado exatamente. Novas claims noturnas
não podem se declarar `supported`. Os metadados da rotina não viram
novos snapshots raw. A seleção registra volume e latência; consultas registram
abstinência e referências selecionadas, claramente distintas de citações
semanticamente verificadas. Não se incluem payloads privados em logs gerais.
Erros de validação do envelope/proposta retornam código limitado, sem repetir
`input_value` ou o payload recebido.

`pilot-start` exige `curator` e `source_manifest` explícito do projeto. O
registro preserva hashes e datas. `pilot-review` exige o mesmo curador,
`outcome` (`useful`, `needs_work`, `stop`) e `note`; pode registrar
`curation_minutes`. A primeira revisão ocorre após 7 dias. Aceite exige
`accept: true`, 14 dias decorridos e revisão útil em ambas as semanas. Revisões
anteriores e pilotos anteriores permanecem no histórico. `stop` desliga e
cancela a rotina. O aceite não a habilita automaticamente.

Rollback da automação: `routine-cancel` desliga novos jobs e solicita o
cancelamento dos seus objetivos. Trabalhos já iniciados preservam
`effects_may_be_uncertain`; não se declara reversão de efeitos confirmados.
Raw, conceitos, journal e histórico de tarefas permanecem. A suíte descartável
exercita esse desligamento, cancelamento ativo, dedupe após crash e duas semanas
simuladas. O aceite humano real continua não observado em 02/11/2026. É opcional
e não bloqueia a leitura do cofre.
