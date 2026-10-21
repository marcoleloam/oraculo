# Skill Oraculo e instalação por provedor

`skills/oraculo` é a fonte única da skill. O pacote a distribui em
`oraculo/assets/skills/oraculo`; `templates/integration` contém blocos cooperativos
para incorporar a instruções existentes mediante o escopo autorizado. O
instalador não altera `AGENTS.md`, `CLAUDE.md`, configurações ou permissões do host.
O CLI e o daemon mantêm as regras executáveis; Markdown seleciona o fluxo.

Esta entrega oferece instalação local verificável e um harness de aceitação.
**Nenhum provedor é anunciado como integrado validado.** Arquivo instalado,
descoberta, invocação, processo CLI e confirmação do serviço são etapas diferentes.
As operações geridas mutantes exigem daemon com protocolo 1; a resposta
`managed_service_upgrade_required` bloqueia o fluxo até manutenção autorizada.
Consulta offline continua identificada pela sua fonte e não confirma daemon online.

## Inspecionar e escolher um destino

```sh
oraculo skill inspect --provider codex --project /workspace/example
oraculo skill inspect --provider claude --root /workspace/example/.claude/skills
oraculo skill inspect --provider grok --evidence /private/discovery.json
```

`--root` é o diretório de skills, **sem** o sufixo `oraculo`. Sem destino explícito,
somente uma raiz no registro de descoberta permite seleção automática. A ausência
de evidência ou várias raízes não autoriza adivinhar o destino de uma instalação.
`--home` e `--project` delimitam as raízes locais de inspeção; nos ensaios use home
descartável e cwd explícito. Nenhum comando abre sessão de modelo ou faz login.

O operador registra a observação real do catálogo do harness ou de `grok inspect
--json` num arquivo privado, fora do checkout público:

```json
{
  "schema_version": 1,
  "provider": "codex",
  "provider_instance_id": "instance-returned-by-live-host-catalog",
  "harness_version": "observed-version",
  "mechanism": "harness_catalog",
  "source": "private-reference-to-raw-discovery-evidence",
  "observed_at": "2026-01-01T00:00:00Z",
  "roots": ["/workspace/example/.agents/skills"],
  "skill_sources": []
}
```

`roots` contém diretórios efetivamente considerados pelo harness; `skill_sources`
lista **todas** as fontes já descobertas da skill Oraculo, incluindo plugins,
diretórios extras e compatibilidade, mesmo fora das raízes convencionais. Para
Grok, normalize `skills[].source.path` das entradas de nome `oraculo` e registre
`mechanism=grok_inspect`. Não deduza suporte de uma pasta vazia. Para Codex/Claude,
use catálogo ou transcrição observável do harness; `--version` sozinho não revela
as raízes. Os registros são observações fornecidas pelo operador, não atestados
criptográficos nem garantia de completude. Recolha-os novamente ao trocar de
cwd, worktree, versão, configuração ou sessão.

O instalador examina também os caminhos convencionais e comandos homônimos.
Fontes em plugins/configuração que não constam do registro não são descobertas
magicamente. A opção `mechanism=isolated_fixture` existe para testes e **não**
confere suporte. Um registro aceito nunca transforma `loaded_skill` em observado.

## Instalar, repetir e remover

```sh
oraculo skill install --provider codex --root /workspace/example/.agents/skills \
  --home /isolated/home --project /workspace/example \
  --evidence /private/discovery.json --dry-run
oraculo skill install --provider codex --root /workspace/example/.agents/skills \
  --home /isolated/home --project /workspace/example \
  --evidence /private/discovery.json
oraculo skill uninstall --provider codex --root /workspace/example/.agents/skills \
  --home /isolated/home
```

O dry-run retorna o plano e hashes com `dry_run=true`; não cria sequer lock ou
diretório. `state=installed/updated/uninstalled` no dry-run descreve a ação prevista,
não um efeito realizado. A execução grava `.oraculo-install.json` junto à skill:
schema, provedor proprietário, hashes SHA-256 relativos, digest do conjunto,
instante e referência à descoberta. O manifesto contém destinos privados e não
deve ser commitado ou distribuído. Reinstalação idêntica é `unchanged` e preserva
bytes/mtime do manifesto. O lock `.oraculo-install.lock` serializa instaladores
cooperantes e permanece no diretório pai, sem afetar a descoberta da skill.

Conteúdo preexistente sem manifesto não é adotado, mesmo idêntico. Edição externa,
arquivo ausente, symlink, manifesto inválido, proprietário diferente e duplicidade
bloqueiam a operação. Não há `--force`. Uninstall verifica **todo** o inventário
antes de remover somente os arquivos geridos; arquivos extras e seus links são
preservados. Uma skill carregada via compatibilidade de outro provedor deve ser
removida pelo provedor proprietário, nunca por uma segunda instalação concorrente.

Atualização limpa prepara um diretório novo, guarda a versão anterior em
`.oraculo-previous`, troca o diretório e remove somente arquivos comprovados do
manifesto anterior. Extras alheios bloqueiam atualização, preservando-os. Uma falha
de swap tenta restaurar imediatamente o diretório anterior. Interrupção de processo
com backup restante retorna `skill_interrupted_install`; não há limpeza cega.
Para recuperar, pare instaladores concorrentes, confira os hashes dos manifestos
no destino e backup e preserve ambos até decidir qual versão manter. Se o destino
estiver ausente e o backup íntegro, renomear o backup para `oraculo` restaura a
versão anterior. Se ambos existirem, compare-os antes de remover somente arquivos
comprovadamente geridos. Esse reparo é explícito, não rollback automático de
configurações ou efeitos externos. Não se promete segurança contra um processo
local adversarial que troca diretórios durante a operação.

Saída: `schema_version`, `state`, `error_code`, `retryable`, `recovery_action`,
`operation_id`, `source`, instante, destino e provas. O CLI integrado retorna 0
para consulta/ação observada, 1 para bloqueio/falha operacional e 2 para entrada
inválida. Inspeção pode mostrar conflitos sem alterar arquivos; instalação faz o
gate de conflito novamente. `diagnose(...)` é uma inspeção sem mutações, utilizável
pelo doctor antes de carregar configuração do daemon.

## Evidência por provedor

Verificação documental e inspeção determinística em 2026-10-21:

- **Codex CLI 0.163.0:** documentação indica `.agents/skills` em cwd/ancestrais até
  a raiz Git e `~/.agents/skills`; `$oraculo`/seletor de skills são entradas
  explícitas. O catálogo do harness inspecionado usa também `~/.codex/skills`.
  Não migrar nem copiar para ambos por suposição. `agents/openai.yaml` contém
  somente metadados/política Codex suportados; não é contrato universal de
  permissões. [Documentação oficial](https://learn.chatgpt.com/docs/build-skills).
- **Claude Code 2.1.302:** caminhos pessoais/projeto `.claude/skills`, invocação
  `/oraculo`; plugins podem ter namespace. A documentação descreve regras
  específicas para skills em subdiretórios e fallback do checkout principal
  para worktrees a partir de versões determinadas. Esses casos precisam de
  ensaio no harness usado; existência local não comprova carregamento no T3.
  [Documentação oficial](https://code.claude.com/docs/en/skills).
- **Grok 1.0.62 (1474f9cbdd91):** `grok inspect --json` local terminou com exit 0,
  detectando outras skills de usuário `.grok/skills` e compatibilidade Claude.
  A documentação inclui `.grok/skills`, skills de plugins e raízes extras,
  compatibilidade Claude e `~/.agents/skills`; invocação `/oraculo`.
  `allowed-tools` não concede nem restringe ferramentas. A observação de outras
  skills não valida a skill Oraculo.
  [Documentação oficial](https://docs.x.ai/build/features/skills-plugins-marketplaces).

Consultas reais ao CLI instalado e ao serviço isolado foram observadas em sessões
dos três provedores, separadas da descoberta/invocação nativa. O `skills/list`
do app-server Codex 0.163.0 descobriu uma única skill de projeto habilitada na
raiz, subdiretório e worktree externa das fixtures temporárias. Isso não certifica
invocação pelo modelo nem equivalência com o catálogo da sessão T3. `grok inspect`
nas três fixtures não listou Oraculo e reportou `projectTrusted=false`; nenhuma
confiança/configuração global foi alterada. A sessão Claude observada retornou
`Unknown skill: oraculo`. Para os três, `loaded_skill=not_observed` pela invocação
nativa, e o ciclo completo da matriz continua sem certificação.
Versão desconhecida não é rejeitada por um número arbitrário; permanece sem
suporte anunciado até descoberta e aceitação próprias. `providerInstanceId`
deve vir do catálogo vivo, não de um nome de modelo fixado nesta documentação.

## Harness executável e matriz de aceitação

O harness não inicia modelos, não chama T3, não abre daemon nem altera home real:

```sh
uv run --frozen python tests/acceptance/skill_provider_harness.py prepare \
  --provider codex --directory /tmp/oraculo-codex-trial
```

Execute separadamente para `claude` e `grok`, sempre num destino novo. Ele prepara
uma instalação de projeto, checkout Git e worktree externa reais, prompts de
cenários em `cases/*/{implicit,explicit}.txt`, CLI sintético em `bin/oraculo` e
`matrix.json`. Os commits de fixture excluem manifesto/lock privados. A seleção
do destino inicial usa documentação e é explicitamente `isolated_fixture`;
confirme discovery real e fonte/hash antes de considerar esse ponto validado.

O integrador conduz as sessões reais **em série**, após liberar os slots de
implementação, usando cada provedor/versão do catálogo vivo. Em T3 use checkout
de fixture com a skill de projeto; não presuma que um HOME temporário do shell
muda o catálogo de uma sessão T3 existente. Registre fonte/hash únicos e evidência
de carregamento disponibilizada pelo harness. Abra nova sessão quando necessário
e registre separadamente o comportamento de sessão já existente.

Cada prompt estabelece a fixture e o executável isolado, sem indicar a resposta
esperada. O CLI sintético registra argv/payload/exit/PID/resposta em `trace.jsonl`.
Observe o término real do ensaio e avalie:

```sh
uv run --frozen python tests/acceptance/skill_provider_harness.py evaluate \
  --directory /tmp/oraculo-codex-trial --case status --completed
```

`--completed` é uma observação do operador, não prova extraída do modelo. Sem ela,
ausência de chamadas não vira passe de um caso negativo. `result.json` conserva
comandos, falhas e digest da trace; nunca certifica carregamento ou serviço.
Revise a transcrição também: uma delegação por ferramenta externa ao CLI é bypass
que a trace sozinha não detecta. A fixture deve impedir essas ações por escopo.
Use destino novo para cada variante/contexto, preservando as traces anteriores.

A matriz cobre novo pedido, pergunta simples, status, continuação, resume, evento
atrasado, relatório de memória, executor já delegado, planejamento, finish de
implementação, operações de consulta e serviço indisponível. O avaliador verifica
chamadas indevidas/duplicadas, IDs, payloads, gates de planejamento e comandos
obrigatórios. Tests determinísticos verificam também reutilização de chave com
brief divergente. Para cada provedor repita em raiz, subdiretório e worktree externa,
invocação explícita/implícita, sessão nova/existente e versão não validada.

**Gate adicional obrigatório para anunciar suporte:** com configuração, socket,
estado e corpus de teste isolados, substituir o CLI sintético pelo CLI instalado
real e usar o bridge caller-bound real. Coletar processo CLI, resposta canônica
correlacionada, memória found/empty/indisponível, um único filho por intenção,
retorno e memória final confirmada. Não chamar um socket falso de serviço real.
Esse ensaio depende do integrador e da liberação dos slots; não é executado nem
certificado pelo harness de routing. Se o host não expõe prova confiável de carga,
manter `loaded_skill=not_observed` mesmo com CLI/serviço confirmados. A cobertura
parcial continua explícita, sem transformar o texto do agente em prova.

## Limite de enforcement e instruções cooperativas

Inspeção delimitada do catálogo MCP T3 e `orchestrator_capabilities`, com
`t3_environment_read` reportando servidor 0.1.0 em macOS/arm64, mostrou APIs de
delegação, leitura, cancelamento e gerenciamento. Não expôs um ponto de veto
global de execução por admissão Oraculo. Não foi inspecionado ou modificado código
T3. Isso não prova inexistência de mecanismos internos; não há mecanismo
comprovado neste escopo para anunciar enforcement.

O ponto coberto é a entrada cooperativa `oraculo task` e sua identidade de bridge,
validada pelo daemon. Execução direta de ferramentas ou delegação T3 fora dessa
entrada pode contornar a skill. Um hook de um provedor não comprova cobertura de
outros provedores/transportes. Indisponibilidade bloqueia a entrada gerida sem
inventar um bypass. Para desativar a integração, remover somente a instalação
gerida e o bloco cooperativo explicitamente aplicado, preservando o restante das
instruções e acesso CLI. Nenhum hook ou configuração global é instalado aqui.

Templates devem ser incorporados como blocos, preservando instruções e aprovações
existentes. Sua herança deve ser observada na raiz, nos subdiretórios, em worktrees
externas e sessões existentes; não se presume que uma worktree fora do ancestral
leia as instruções desse ancestral.

## Integração do módulo

O integrador registra `skill_install.register_parsers(subparsers)` e despacha
`skill_install.dispatch(args)` **antes** de `Config.load`. Para doctor, usa
`diagnose(provider=..., root=..., home=..., project=..., evidence=...)`; todos os
argumentos são opcionais exceto os que a operação concreta exige, e nenhum
diagnóstico consulta bancos ou serviço. Os recursos devem constar de wheel e
sdist. Smoke de artefato instalado precisa testar `resources_root()` sem checkout;
os testes de fonte não substituem essa validação da frente de empacotamento.
