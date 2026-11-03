# Oráculo 0.2.0

Esta versão reúne orquestração verificável, Second Brain e cadastro dinâmico de
projetos. Versão do pacote, commit de fonte, release instalada, ativação e
implantação são identidades e provas separadas. Este documento descreve o
contrato distribuído; não certifica publicação nem ativação em uma instalação.

## O que muda

- Eventos de CI vinculados a projeto, repositório, PR, HEAD/base, run e attempt
  entram por um observador autenticado. Eventos duplicados, conflitantes ou de
  outro binding não liberam admissão. Um campo `authenticated=true` do chamador
  não substitui a autenticação do observador instalado pelo serviço.
- Critérios `OutcomeContract` v2 vinculam commit, comando, artefato e hash. O
  serviço executa o comando sem shell e verifica fonte limpa antes e depois;
  critérios sem contrato continuam legados, sem atestação retroativa. Resultados
  semânticos exigem a revisão independente prevista no plano.
- Verificação de rollout reutiliza o journal de operações e exige identidade de
  fonte, merge, ambiente e artefato, além de um verificador configurado. Relato
  de implantação e confirmação do destino continuam separados.
- Entregáveis geridos de validação e pesquisa têm contratos próprios. A origem
  continua responsável por fechamento, memória confirmada e publicação.
- Second Brain mantém captura explícita, consulta com fontes, auditoria,
  compilação e rotinas controladas. O painel de tarefas e o portfólio continuam
  disponíveis; dados de um projeto não autorizam acesso a outro.
- Cadastro de um projeto observado no catálogo T3 publica somente uma adição
  em `state_dir/project-authorization.json`, sem editar a configuração presa
  pelo processo nem reiniciar o serviço. Preserva capacidade, provedores,
  rotinas e reservas; não reativa linhas `unregistered` sem um novo tick válido.
- Permanecem os diagnósticos de despacho, a escolha explícita de commit limpo
  quando o checkout contém alterações e a manutenção com carry auditado. Uma
  atualização não limpa falhas históricas nem cancela reservas antigas.

## Defaults e custo

Decisions vem com `enabled=false`, `mode=off`, orçamentos diário e por corrida
iguais a zero e nenhuma tarifa aprovada. O caminho desligado não resolve
credenciais nem envia chamadas. A implementação de sombra produz recomendações;
não muda rotas, grants, critérios, merge ou deploy. Testes e CI usam transportes
sintéticos. Nenhuma API paga é necessária para instalar ou validar esta versão.
Uma futura habilitação exige autorização e limites financeiros independentes;
a assinatura de um provedor não comprova crédito de API.

`additional_spend_allowed=false`, políticas de escopo e resolução OAuth no
Keychain permanecem preservados. Ausência ou falha de autenticação recusa o
cadastro, sem arquivo de política parcial. Conhecer um ID não autoriza um projeto.

## Compatibilidade e rollback

O runtime 0.2.0 suporta schema até **6**, com promoção lazy: estado principal
legado permanece em 4 e ledger gerido legado em 5 enquanto não forem usados
contratos novos. Persistir semântica nova promove o banco correspondente antes
da escrita. Leitura offline somente leitura não migra nem cria ledger ausente.

Prepare o bundle 0.2.0 com `--schema-min 4 --schema-max 6`. O intervalo pertence
ao código real distribuído: o serviço instalado respeita também o teto do
manifesto. Um manifesto antigo limitado a 4 não ganha capacidade de escrever
schema 6 ao receber fontes novas. Não altere manifestos ou releases imutáveis.

Depois de promover qualquer banco a 6, rollback só é possível para uma release
que declare **e implemente** suporte a esse schema. Um runtime limitado a 4 não
pode ler os novos dados 6; um leitor legado de ledger limitado a 5 também recusa.
Não restaure backup antigo como rollback: isso perderia IDs, reservas, recibos e
efeitos posteriores. Conserve os dados e use uma release compatível ou prepare
uma correção progressiva. Backups servem à recuperação auditada, não ao recuo do
histórico operacional.

O `release_id` e `source_commit` do bundle devem apontar para o SHA público
integrado e revisado que produziu o artefato. Um commit de preparação privada
não identifica a release pública. Siga [Instalação e manutenção](../INSTALACAO.md)
para wheelhouse específico do sistema/Python, hashes, preparação, manutenção,
carry auditado quando aplicável e ativação separada.

## Comandos disponíveis

Confira a CLI da instalação e o status antes de decidir uma mudança operacional:

```sh
oraculo --help
oraculo --config "$HOME/.config/oraculo/config.toml" status
oraculo --config "$HOME/.config/oraculo/config.toml" runtime status
oraculo --config "$HOME/.config/oraculo/config.toml" brain query \
  --project PROJECT_ID --query 'decisão e fontes'
oraculo --config "$HOME/.config/oraculo/config.toml" experience serve --all-projects
```

Para adicionar um projeto real do catálogo, use o par ID/diretório observado e a
confirmação exata retornada por `plan`:

```sh
oraculo --config "$HOME/.config/oraculo/config.toml" project register plan \
  --id PROJECT_ID --path /caminho/absoluto/do/projeto
oraculo --config "$HOME/.config/oraculo/config.toml" project register apply \
  --id PROJECT_ID --path /caminho/absoluto/do/projeto --confirmation CONFIRMATION
```

O segundo comando modifica a política operacional e observa novamente o catálogo.
Consulte [Cadastro de projeto](../PROJECT_REGISTRATION.md) para recusas, replay,
concorrência e limites. Eventos CI, outcome e rollout são operações do protocolo
local do serviço; não há subcomandos CLI homônimos implícitos.

## Verificação offline e integração real

No checkout revisado e limpo, com Python 3.12 e dependências do lock disponíveis:

```sh
uv sync --frozen --group dev
uv run --frozen python -B scripts/orchestration_ci.py \
  --expect-head "$(git rev-parse HEAD)" --output /diretorio/externo/novo \
  --run-id local-review --run-attempt 1
uv run --frozen pytest -q -rs
```

O CI obrigatório exige lint, formato, seleção pytest/JUnit sem skips, gate
Second Brain com escalas 50/500/5.000, sombra sintética, build wheel/sdist e
instalação em ambiente descartável fora da fonte. Recibos vinculam SHA, comandos,
exits e hashes dos artefatos; fonte limpa e HEAD são conferidos antes e depois.
Build/install podem baixar dependências fixadas; offline aqui significa ausência
de chamadas aos serviços operacionais, credenciais e APIs de modelos. Um cache
preparado permite executar sem rede de dependências. O template de workflow deve
ser ativado somente na árvore pública revisada e sanitizada.

A suíte global inclui skips explícitos de snapshots históricos exatos e de
integrações GUI/launchd opt-in. Eles não contam como passes. Integrações reais de
OAuth/T3, callbacks de CI, provedores, Obsidian e ativação exigem provas separadas.
Simulação não comprova utilidade humana, latência live, cobrança ou implantação.
Os pacotes incluem módulos, exemplo de configuração, prompts, templates e skill;
relatórios de execução e evidências privadas ficam fora do sdist e do wheel.
