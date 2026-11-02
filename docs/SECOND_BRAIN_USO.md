# Como usar o Second Brain

Leitura do que o Oráculo já guardou. Abrir o cofre não pede nova aprovação de
F0–F6 e não espera corpus, curador, captura ou rotina.

## Abrir

1. No Obsidian, abra a pasta do cofre unificado como vault. A nota raiz é
   `Inicio.md`.
2. Sem Obsidian, abra `index.html` na mesma pasta. A página mostra os projetos
   e também aponta para `Inicio.md`.

As duas visões são estáticas. Fechar e abrir de novo não busca dados novos
sozinho.

## Navegar

Em `Inicio.md` ou no painel:

1. Escolha um projeto. No Obsidian, o link está na seção Projetos. No painel,
   o modo inicial é Projetos.
2. Abra Tarefas.
3. Abra uma tarefa e, quando existir, uma subtarefa. O vínculo precisa estar no
   recorte. Título parecido ou a mesma onda não criam subtarefa. Sem filho, a
   página diz que não há subtarefas registradas.

Onda é um filtro opcional. Dá para ler sem escolher onda.

No Obsidian, `Inicio.md` também abre o mapa dos projetos. Cada grupo tem o
título e o nome da cor; a cor sozinha não identifica o projeto. Os três
cartões de um grupo abrem o início, as tarefas e a biblioteca. O gráfico
pinta as mesmas pastas e, na busca inicial, esconde cópias técnicas e as
notas que ligariam todos os projetos. Os arquivos continuam na pasta.

A área `Possível — a validar` não é parentesco. Ela só aparece quando os dois
projetos declaram o mesmo tópico ou quando o título das notas contém a mesma
palavra de uma lista curta, inteira, como API ou SQL. Zero sugestões é um
resultado válido. A nota `Referencias` continua sendo a lista das ligações
confirmadas.

A biblioteca lista as notas e as fontes do recorte, inclusive as que não estão
ligadas a uma tarefa. O texto entra por inteiro.

A nota `Referencias`, no Obsidian, e a seção de referências do painel mostram
só ligações explícitas que os dois lados resolvem. Memória privada de outro
projeto não é copiada para cá por semelhança de nome, caminho ou identificador.
A biblioteca compartilhada é o recorte que o serviço já colocou à parte.

## Atualizar a pasta

Quando quiser uma observação nova:

```sh
oraculo experience sync --output DIRETORIO_DO_COFRE
```

`DIRETORIO_DO_COFRE` é a pasta que você já abre no Obsidian. O comando preserva
`app.json` e os outros JSON soltos em `.obsidian/` que o pacote não gera.
Preserva também `workspace.json` e `graph.json` quando você ou o Obsidian já
mudou esses bytes. Se `graph.json` ainda é o do pacote anterior, a atualização
reescreve as cores. Para voltar às cores geradas depois de uma edição, apague
só esse arquivo e rode o comando de novo. Recusa a troca se uma nota, o mapa
ou o guia foram editados, se há arquivo desconhecido ou se há um atalho
simbólico. Se a atualização falha, a pasta anterior permanece.

Para limitar o recorte, repita `--project` com o identificador de cada projeto.
Sem `--project`, entram os projetos autorizados do catálogo.

Depois do comando, abra de novo `Inicio.md` ou `index.html`. Não há atualização
automática enquanto você lê.

A formatação automática do JSON pelo Obsidian não bloqueia a atualização;
alterações nos cartões, posições e conexões continuam protegidas.

Se o Obsidian estava aberto e o mapa novo não aparecer, recarregue o cofre.
Com a CLI nativa disponível, use `obsidian vault=NOME_DO_COFRE reload`.
Isso atualiza o índice do aplicativo após a substituição segura da pasta.
No Canvas, aproxime os grupos dos projetos para ler os cartões; as sugestões
de conexão ficam na área `Possível — a validar`, abaixo dos projetos.

Um exemplo real, só daquele instante: em 01/11/2026 às 18:21:40 UTC a pasta
tinha 5 projetos, 213 tarefas, 552 documentos e 4.146 arquivos.

O contrato da pasta, incluindo o que o comando recusa, está em
[SECOND_BRAIN_UNIFICADO.md](SECOND_BRAIN_UNIFICADO.md).

## Saber o identificador de um projeto

```sh
oraculo status
```

Na resposta, use `data.projects`. Em cada item:

- `id` é o valor de `--project`;
- `status` igual a `registered` é um projeto do catálogo;
- `path` é o caminho registrado. O comando não cria um título a partir dele.

O `--project` de `oraculo status` é opcional. Na implementação, o padrão é `*`,
o catálogo inteiro. Use o `id`, não o caminho da pasta, nos comandos abaixo.

## Consultar sem gravar

Troque `ID` pelo `id` do passo anterior. Estes comandos não publicam e não
ligam rotina:

```sh
oraculo brain query --project ID --query 'qual decisão você quer reler?'
oraculo brain report --project ID --output relatorio-novo.json
oraculo brain routine-status --project ID
```

`query` devolve o contexto encontrado, com referências, e não altera a base.
`report` pode gravar um JSON novo em `--output` e recusa sobrescrever esse
arquivo. `routine-status` mostra se a rotina está ligada. Ela começa desligada.

Uma consulta na release ativa, em 01/11/2026, devolveu 4 documentos: 5.590 de
6.000 tokens de detalhe e 369 de 800 do resumo, sem mudança na base. A fonte
integral lida nesse dia tinha 7.675 bytes, com hash conferido.

## O que só acontece se você confirmar

Não faz falta para ler.

- Captura de arquivos escolhidos: primeiro o plano, depois a confirmação do
  identificador do manifesto.
- Migração de páginas antigas: primeiro o plano, sem aplicar, depois a
  confirmação do identificador do plano. Não foi aplicada ao vault vivo.
- Rotinas: uma instalação nova começa desligada até `enabled` explícito em
  `routine-configure`. Na instalação operada em 02/11/2026, os cinco projetos
  autorizados já têm rotinas habilitadas. Não há gasto adicional permitido.

Os comandos e os limites estão em [SECOND_BRAIN_F5_F6.md](SECOND_BRAIN_F5_F6.md).

## Passo opcional

Para um piloto no projeto Oráculo, escolha as notas e quem as revisa:

```sh
oraculo brain pilot-start --project ID --file piloto.json
```

O JSON precisa trazer `curator` e `source_manifest`, uma lista de notas que já
estão nesse projeto. Iniciar o piloto não liga a rotina. O aceite real continua
pedindo 14 dias e duas revisões. Uma simulação não conta como aceite. Este
passo não bloqueia a navegação e não reabre a aprovação de F0–F6.

Os contratos e limites das fases estão no
[roadmap Second Brain](ROADMAP_SECOND_BRAIN.md). A ativação e os recibos operacionais pertencem à sua instalação.
