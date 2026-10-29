# Experiência legível do Oráculo

## Requisitos e desenho do incremento

Obsidian e painel HTML local compartilham uma projeção determinística, selecionada
por projeto, com nomes, assuntos, tarefas, decisões, aprendizados e evidências.
IDs e caminhos canônicos continuam estáveis. Exportação nunca edita o vault:
produz um pacote novo, privado, reconstruível, com Markdown, fontes selecionadas,
JSON e painel responsivo pesquisável. Sem plataforma ou dependência de UI nova.

O serviço é a fronteira de seleção. O CLI exporta snapshots pelas interfaces do
serviço ou recebe fixture explícita; o gerador offline não abre banco/config/vault.
Não há sincronização automática nem alteração do runtime instalado nesta entrega.

Comunicações preservam o envelope ORACULO_CONTROL_V1 integral, event_id, ordem,
snapshot congelado e chave de envio. T3 expõe mensagem textual; não foi observado
um campo de evento técnico invisível. Novas tarefas recebem resumo pt-BR seguido
de detalhes expansíveis com JSON literal. `return_format: "json"` mantém corpo JSON
puro para integrações. Entregas antigas já congeladas permanecem byte compatíveis.
O consumidor da origem deve consultar status fresco antes de apresentar o resumo;
histórico/replay não significa estado atual, nem autoriza abrir tarefa.

Utilidade distingue memória encontrada pelo serviço, consulta confirmada no serviço,
leitura do agente desconhecida, aplicação declarada pelo agente e checks registrados.
Checks de um relatório não demonstram causalidade da memória. Benefício demonstrado
permanece desconhecido sem experimento/instrumentação vinculada. Nenhum ETA,
porcentagem, tempo economizado ou aprovação é inferido.

Manifesto: `presentation.py` (resumos/protocolo), `experience.py` (projeção/exportação),
`experience_html.py` (painel), CLI/serviço (interfaces), store/adapter (entrega),
testes de compatibilidade/isolamento e fixture sintética. Ownership: executor atual;
pai faz revisão, finish e eventual ativação. Código de rótulos CSS de 36bea35 é
reaproveitado seletivamente, sem afirmar ativação ou integração prévia.

## Prévia e reconstrução

```sh
oraculo experience export --file tests/fixtures/experience.json --project demo --output /tmp/oraculo-demo
# Snapshot oficial, depois de instalar serviço que contenha esta interface:
oraculo experience snapshot --project demo > /caminho/privado/snapshot.json
oraculo experience export --file /caminho/privado/snapshot.json --project demo --output /caminho/privado/export
oraculo experience status --file /caminho/privado/task-status.json
oraculo experience status --file /caminho/privado/task-status.json --json
```

Abra `index.html` no navegador ou `Inicio.md` no Obsidian (pacote como vault).
Reexportar o mesmo snapshot gera os mesmos bytes. Destino deve ser novo/vazio ou
conter exatamente a exportação anterior intacta; notas locais ou edições causam
recusa, nunca overwrite silencioso. Para nova seleção use outro diretório.
O pacote inclui conteúdo privado do projeto selecionado e shared: não publique.
Links para conteúdo ausente/fora do escopo aparecem como indisponíveis, sem link.
HTML mostra texto escapado, sem executar Markdown/HTML de fontes não confiáveis.

O snippet opcional `oraculo obsidian project-labels` mantém o contrato de 36bea35;
consulte OBSIDIAN.md. A navegação legível funciona sem esse snippet ou plugins.

## Métricas e limites

Contagens mostram fontes disponíveis/consultadas, declarações de aplicação,
checks passed/failed registrados e ocorrências de erros com mesma chave explícita.
Encontrada não significa lida pelo agente; consulta do serviço não atesta leitura
integral; aplicação é autorrelato. Erro recorrente exige a mesma chave registrada
em mais de uma tarefa. Notas antigas sem metadados recebem título do H1/alias ou
resumo, e dados ausentes continuam desconhecidos. Títulos não são identidade.

Relatórios novos podem incluir `title`, `topics` (lista de assuntos),
`memory_application` (lista de aplicações declaradas), `lessons` e `errors`
(listas de aprendizados e chaves/descrições de erro). Esses campos são retidos
na fonte estruturada pelo ciclo de memória, sob o mesmo limite de tamanho e
validação canônica do relatório. Não são provas de leitura ou benefício.
Sínteses novas usam título/alias legível, mantendo page_id e caminho originais.

Snapshot é uma observação local, não feed ao vivo ou transação distribuída com T3.
Snapshots acima do limite do socket usam o cache privado imutável de artefatos
do serviço, com leitura em blocos e conferência integral de SHA-256 pelo CLI
(limite de 64 MiB). Isso não altera notas, tarefas ou memória canônica.
Painel mostra última atualização observada. Relatórios, raw e fontes podem conter
declarações não verificadas. Nenhuma ação operacional é executada por links.
Integrações que fazem json.loads no corpo devem pedir return_format=json antes
de usar mensagens legíveis; parser oficial aceita os dois formatos. Detalhes HTML
podem não colapsar em todos os clientes T3: o resumo continua primeiro e o protocolo
permanece acessível. Não há alegação de suporte nativo a eventos invisíveis.

## Tarefas por projeto com o daemon já instalado

`experience serve` é uma central de tarefas, separada do pacote de memória.
A CLI candidata pode executá-lo contra um daemon que já ofereça `task list/status`
(`managed.read`, protocolo 1), sem ativar uma release ou reiniciar o serviço:

```sh
oraculo --config /caminho/privado/config.toml experience serve --project alpha --port 8765
```

O processo permanece em primeiro plano. Abra `http://127.0.0.1:8765/` e encerre com
Ctrl-C. Não usa launchd, não cria tarefa, não inicia executor e não modifica
capacidade/memória. Consulta somente a API oficial `managed.read` com ação `list`;
não abre SQLite/vault e não faz fallback offline. A listagem recebida do daemon
pode conter vários projetos; o servidor filtra antes de produzir a resposta HTTP
com somente título, identidade, apresentação, observações e links necessários.
Nenhum relatório, contexto, conteúdo de fonte, recibo ou caminho de projeto é
servido. Uma consulta não observa novamente o host: mostra a última observação
registrada pelo daemon, com data distinta da hora da consulta ao serviço.

O projeto fica fixo no argumento de inicialização. A atribuição canônica resolvida
prevalece; antes dessa resolução, a tarefa aparece no projeto da origem vinculada
pelo host. Nunca se infere projeto por título, basename ou caminho solicitado.
IDs desconhecidos não expõem tarefas de outro projeto. O servidor só aceita o Host
`127.0.0.1:<porta>`, sem CORS, acesso cross-site, parâmetros de consulta, seletor de
projeto pela URL, listagem de diretórios ou arquivos locais. Bind é exclusivamente
IPv4 loopback; não há opção para publicar na rede. Continua sendo uma interface
privada local, sem autenticação entre usuários/processos da mesma máquina.

O projeto aparece no cabeçalho e em cada linha, junto do título e de um ID curto
de dez caracteres (atalho visual, sem substituir o ID completo). A lista compacta
mostra atividade ou impedimento, estado e última mudança. Expandir uma linha revela
atividade completa, próxima ação, motivo e resultado do trabalho, além dos links
separados de origem e executor. **Identificação e observações** expõe ID completo,
projeto canônico, etapa, horário, execução observada, código de impedimento e revisão.
Títulos, atividades e motivos são texto, sem interpretar HTML/Markdown. Datas na
lista usam o fuso do navegador; os detalhes preservam a data fornecida pelo serviço.
Ausência de horário permanece desconhecida e fica depois das datas conhecidas.

**Em aberto** é a seleção inicial; **Histórico** reúne trabalho concluído,
resultados negativos/parados/cancelados e substituições registradas. Um run
`completed` sem certificação continua em aberto, aguardando validação. Bloqueio
com resultado ainda pendente também permanece em aberto. **Todas** permite
pesquisar ambas as seleções. Essa organização de leitura não modifica o lifecycle.

A busca local inclui título, ID completo ou curto (também com `#`), projeto,
atividade, etapa e motivo/código de impedimento; ignora maiúsculas e acentos.
Os botões de resumo contam a seleção de período e busca, antes do filtro de estado;
clicar em um estado aplica esse filtro, clicar novamente o remove. A contagem da
lista mostra quantas tarefas passaram pelo filtro e o total da seleção. A ordenação
inicial coloca bloqueadas/paradas antes de aguardando e executando, usando a última
mudança como desempate; **Mudança mais recente** ordena só por data. Não há
classificação inferida de severidade ou urgência. O projeto continua fixo, sem
seletor global nem expansão de acesso a outros projetos.

O botão **Atualizar agora** e a atualização a cada 15 segundos consultam o serviço.
Falha/timeout oculta a lista, substitui as contagens por **—** e indica **estado
atual desconhecido**, preservando a hora da última consulta bem-sucedida. Não exibe
zero tarefas como resposta de falha. Filtros, detalhes abertos e foco são preservados
na recuperação e nas atualizações de linhas que continuam presentes; linhas são
reutilizadas pelo ID completo. Nenhum snapshot antigo é exibido como estado atual.
Uma consulta válida sem tarefas possui mensagem de vazio própria. Respostas usam
`Cache-Control: no-store`. Os estados preservam a projeção `task_tracking`:

- **Aguardando / fila:** preparação, espera por capacidade, validação, gravação
  de memória, aprovação pendente ou run terminado sem certificação suficiente.
- **Executando:** fase de execução com observação compatível.
- **Bloqueada / parada:** impedimento persistido ou execução negativa/parada;
  mostra explicação, código de motivo quando disponível e próxima ação.
- **Concluída:** trabalho positivo com evidência de término, memória saved e
  recibo confirmed coerente com o ciclo/commit, sem validação/memória/aprovação
  pendente. Planejamento aguardando aprovação continua aguardando.

O resultado do trabalho, a fase de publicação e a implantação permanecem campos
conceitualmente separados: trabalho concluído não prova PR integrado ou rollout.
Links de origem e executor aparecem separadamente quando disponíveis. Não há ETA,
percentual ou sucesso inferido apenas de um run `completed`.

O desenho usa grafite, âmbar discreto e estados com rótulos textuais; não depende
somente de cor. Layout adapta-se a 390px sem rolagem horizontal. Busca e filtros
têm labels, foco visível e mensagens de consulta/contagem acessíveis. Detalhes usam
disclosure HTML nativo, com teclado; **Fechar detalhes** ou Escape devolvem foco à
linha. Sem imagens, fontes remotas, CDN ou dependência nova. CSP e limite loopback
continuam iguais. A origem valida e inicia o processo HTTP candidato; esta mudança
não instala, reinicia ou altera o daemon.

### Botão de projeto no T3

A origem pode configurar scripts de projeto via API depois de validar a CLI.
Exemplo sanitizado de script, sem configuração aplicada por esta entrega:

```json
{
  "id": "oraculo-task-panel",
  "name": "Tarefas do projeto",
  "icon": "play",
  "command": "oraculo --config /caminho/privado/config.toml experience serve --project alpha --port 8765",
  "runOnWorktreeCreate": false,
  "async": true,
  "autoOpenPreview": true,
  "previewUrl": "http://127.0.0.1:8765/"
}
```

Para dois projetos, use IDs privados somente nas configurações locais e portas
distintas. Antes de instalar a CLI candidata, o comando pode usar o Python 3.12
com dependências existentes e `PYTHONPATH=/checkout/candidato/src`, seguido de
`python -m oraculo ...`. O processo HTTP é novo e independente do daemon;
conflito de porta recusa a partida, sem matar outro processo. O contrato do botão
é preparado aqui; sua configuração/ativação pertence à origem. Acompanhamento
nas conversas continua pelo outbox gerenciado e links nativos existentes.

O snapshot/export estático também inclui tarefas bloqueadas antes da resolução,
último progresso, observação de execução e tipo/estado de aprovação. Continua
sendo exportação sem atualização automática, com o mesmo isolamento por projeto.
