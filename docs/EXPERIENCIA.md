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
