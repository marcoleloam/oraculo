# Cofre de leitura unificado

Projeção de leitura para todos os projetos registrados e autorizados. Um vault
Obsidian e um HTML offline. Os comandos desta página não alteram o publicador
canônico, o vault de origem nem o banco do serviço, e não trocam a release
instalada.

Consulte o [guia de uso](SECOND_BRAIN_USO.md) para abrir e atualizar a projeção.

## Leitura

A navegação é projeto, tarefa e subtarefa. Onda é filtro opcional, não pasta
obrigatória e não cria pai de tarefa. Subtarefa continua exigindo vínculo
explícito no snapshot, como na leitura de um projeto. Documentos entram por
inteiro. Conhecimento `shared/` é o recorte sanitizado que o serviço já colocou
no snapshot; memória privada de outro projeto não é copiada para cá por
vizinhança de nome, caminho ou id local.

Ids de projeto permanecem distintos mesmo quando os títulos são iguais. Ids de
entrada repetidos em projetos diferentes também permanecem distintos. Referência
entre projetos só existe quando o gerador encontra uma relação explícita e
resolvível. O CLI não inventa esse vínculo.

O vault de leitura é um diretório novo. `Inicio.md` é a raiz. Cada projeto fica
sob o `export_root` devolvido pelo gerador. A configuração `.obsidian` é a da
raiz do pacote. Links de wiki precisam resolver dentro desse único vault.

## Cores e mapa

Cada projeto recebe uma cor a partir do id, não do título e não da ordem em
que os snapshots chegaram. Títulos iguais continuam em pastas e cores
distintas. O nome da cor e o id estão em `Guia-do-mapa.md`. A pasta
compartilhada usa cinza e não é um projeto.

`Inicio.md` abre `Mapa dos projetos.canvas` e o guia. O mapa é JSON Canvas 1.0:
um grupo por projeto, com início, tarefas e biblioteca, mais a área
`Possível — a validar`. Não desenha uma nota por tarefa. Subtarefas continuam
na nota de tarefas.

`.obsidian/graph.json` é configuração nativa do grafo. Cada grupo usa
`path:"pasta/"`, com a barra final para não pegar uma pasta vizinha de nome
mais longo. A busca inicial esconde `Fontes/`, `Evidencias/` e as notas-raiz
que ligam o cofre inteiro. Nada é apagado. `Biblioteca/Fontes` também sai do
gráfico, porque o filtro procura o trecho `Fontes/`. A ajuda do Obsidian
explica grupos por busca e não lista as chaves internas desse JSON; o arquivo
segue o formato gravado pelo plugin nativo. Em 02/11/2026, o mapa e os seis
grupos do gráfico foram conferidos no Obsidian 1.14.5, no cofre de leitura
dos cinco projetos. Essa observação não certifica outras versões do aplicativo.

Ligações confirmadas saem só de relações explícitas já resolvidas. Se não há
nenhuma, o guia diz zero. Sugestão usa tópico declarado nos dois lados ou uma
palavra inteira de um vocabulário fechado no título. Não lê o corpo, não liga
títulos iguais e não grava relação canônica. SQL não casa com NoSQL. A linha
no mapa, quando existe, leva o rótulo `Possível`.

## Quem escreve o quê

O CLI não abre SQLite nem o vault operacional.

- `experience portfolio --file BUNDLE.json --output DIR` lê um arquivo local.
  Não usa config nem socket.
- `experience sync --output DIR [--project ID ...]` fala com o daemon já em
  execução. Sem `--project`, seleciona o catálogo. Com `--project` repetido,
  limita a esses ids. A ordem dos flags não muda o pacote: a leitura é ordenada
  pelo id.

`BUNDLE.json` tem `schema_version` 1 e `snapshots`, uma lista de snapshots de um
projeto só, no formato de `experience snapshot`. `project_titles` é opcional:
mapa de id do projeto para título. Chave desconhecida, valor vazio ou tipo
errado recusa o arquivo. Dois snapshots com o mesmo id também.

O catálogo vem de `status.get` com projeto `*`. A resposta real traz
`data.projects[]` com `id`, `path`, `status` e `coordinator_thread_id`, mais
estado operacional que o CLI não copia para o recibo nem para o gerador. Não há
endpoint novo. O esquema atual não tem título. Se uma linha trouxer `title` como
string não vazia, esse texto é o título humano e segue byte a byte, inclusive
espaços. O CLI não deriva título de `path`, de pasta ou de metadado de cliente.
Sem nenhum título no catálogo, `project_titles` vai vazio (`None`) e o snapshot
segue com o `projects[].title` que o serviço já emitiu.

Autorização é `experience.snapshot`. Esse comando passa por `service.project`:
registrado e dentro do allowlist. A negativa é
`project_not_registered_or_outside_scope`. No modo geral esses ids saem da
seleção e aparecem em `excluded_unauthorized`. Um id pedido explicitamente não
é descartado: o comando falha. Projeto ausente do catálogo ou com `status`
diferente de `registered` também falha, antes de baixar snapshot. O transporte
de snapshot é o já existente, inclusive artefato grande com SHA-256. O corpo
selecionado é encaminhado sem reescrita.

Falha de serviço, catálogo malformado, id duplicado no catálogo, snapshot
incompleto, integridade, timeout ou projeto devolvido com outra identidade
interrompe o comando. A exportação anterior não é aberta para escrita. Não há
pacote parcial sem um dos projetos que deveriam entrar.

Os módulos `oraculo.experience_portfolio` e, dentro dele, o HTML offline são
importados só na hora de montar o pacote. Se não estiverem no mesmo
interpretador, o comando falha com `experience_portfolio_unavailable` e não
cria diretório.

## Estático e atualização

`experience export` e `experience portfolio` congelam os bytes do arquivo que
receberam. Abrir o HTML ou o Obsidian não observa o daemon de novo.

`experience sync` pede uma observação nova. O recibo traz `mode` `snapshot` ou
`refresh`, `observed_at` do modelo quando o gerador o devolve como string, e
`freshness` por projeto: `project_id`, `observed_at` do snapshot ou `null`, e
`source` `file` ou `service`. `null` significa que o snapshot não trouxe data.
O CLI não preenche com o relógio local. No sync, `service.health` e
`service.protocol_version` repetem o `status.get` desta execução. Saúde
degradada não é convertida em sucesso silencioso nem em prova de que o pacote
continua atual depois do comando.

As leituras do catálogo e dos snapshots são sequenciais. Não formam uma
transação com o host nem com o Git do vault canônico.

Reexecutar no mesmo diretório usa `refresh_export`. O destino precisa ser uma
exportação anterior intacta, com os mesmos bytes do manifesto. A nova versão
é montada primeiro em um diretório temporário; depois substitui a anterior.
Novas tarefas, documentos ou projetos podem acrescentar e retirar caminhos.
O Obsidian pode reformatar o JSON do Canvas ao abri-lo. A atualização aceita
essa mudança de serialização somente se recuperar o hash original ao aplicar
o formato do gerador; mover um cartão, editar texto, aresta ou propriedade
continua sendo uma alteração local protegida.

Nota editada, arquivo local desconhecido, canvas ou guia editados, ou symlink,
recusam a atualização. Arquivos JSON diretamente em `.obsidian/` são
configuração local. `app.json` e qualquer JSON que não faz parte do pacote
gerado ficam byte a byte. `workspace.json` e `graph.json` também ficam quando
o disco difere do último pacote. Se ainda estão iguais a esse pacote, a
atualização grava a versão nova, para a cor acompanhar um projeto que entrou
ou saiu. JSON aninhado em `.obsidian/` não é configuração local. Uma alteração
concorrente durante a montagem recusa a troca.
Se a substituição falhar, a versão anterior é restaurada. Uma interrupção do
processo pode deixar um backup privado `.NOME.previous-*` junto ao destino.

## O que permanece intocado

O publicador canônico continua sendo o único escritor do vault de origem. Estes
comandos não fazem commit, não publicam proposta e não reiniciam o serviço.
`experience export`, `experience snapshot` e `experience status` seguem válidos
para um projeto.

O pacote de leitura pode conter o mesmo material privado que um snapshot de
projeto já contém, mais o `shared/` sanitizado que veio nele. Não é artefato
para publicar. Não mistura o pedido, o contexto ou os argumentos de lançamento
que o snapshot de experiência já omitia.

## Recusas do CLI

- `experience_portfolio_invalid_artifact`
- `experience_portfolio_no_projects`
- `experience_portfolio_duplicate_project`
- `experience_portfolio_unknown_title`
- `experience_portfolio_unavailable`
- `experience_portfolio_invalid_model`
- `experience_portfolio_invalid_files`
- `experience_portfolio_unsafe_path`
- `experience_sync_invalid_project`
- `experience_sync_duplicate_project`
- `experience_sync_service_failed:<mensagem>`
- `experience_sync_invalid_catalog`
- `experience_sync_unknown_project:<ids>`
- `experience_sync_project_not_registered:<ids>`
- `experience_sync_no_authorized_projects[:ids]`
- `experience_sync_incomplete:<id:motivo,...>`

O comando de projeto único `write_export` continua recusando mudanças no conjunto
de caminhos com `experience_export_file_set_changed_use_new_directory`.
A atualização do cofre unificado também pode responder `experience_export_locally_modified`,
`experience_requires_empty_or_owned_export` e
`experience_symlink_destination`.
