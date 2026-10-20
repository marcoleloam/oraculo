# Instalação do Oráculo no Mac

Instalar o Oráculo significa preparar um serviço local no seu Mac. O T3 continua sendo a interface de conversa e execução dos agentes; o Oráculo coordena a fila, os critérios de entrega e a memória persistente.

## O que é instalado

- Ambiente Python 3.12 e dependências fixadas em `uv.lock`, na `.venv` do checkout.
- Configuração em `~/.config/oraculo/config.toml`.
- Estado operacional em `~/Library/Application Support/Oraculo`.
- Vault de memória em `~/Obsidian/Oraculo`, separado dos projetos.
- LaunchAgent em `~/Library/LaunchAgents/local.oraculo.plist` e logs em `~/Library/Logs/Oraculo`.

O script preserva configuração e dados existentes. A instalação prepara os arquivos; a ativação é um passo separado. O LaunchAgent usa caminhos absolutos deste checkout, portanto mantenha a pasta no lugar enquanto o serviço estiver instalado.

## Requisitos

macOS, Git, `uv`, Python 3.12, T3 Code com MCP e OAuth acessíveis e assinaturas dos provedores que serão habilitados. Para a revisão visual da wiki, use Obsidian com o plugin Karpathy LLM Wiki compatível; a versão exercitada na V1 foi 1.28.0.

## 1. Obter o código e preparar

```bash
git clone https://github.com/marcoleloam/oraculo.git
cd oraculo
./scripts/install-service.sh --dry-run
./scripts/install-service.sh --install
```

`--install` sincroniza as dependências, inicializa dados ausentes e escreve o LaunchAgent. Não inicia o daemon nem autentica contas.

## 2. Configurar a própria instância

Edite `~/.config/oraculo/config.toml`:

1. Substitua `https://t3.example.invalid/mcp` pelo endereço MCP real do seu T3. O domínio do exemplo não oferece um serviço.
2. Confirme `projects_root`, `vault_root` e `state_dir`. Vault e estado precisam ficar fora da raiz dos projetos.
3. Consulte os IDs reais dos provedores e modelos disponíveis no seu T3. Preencha `instance_id` e `models` apenas com modelos cobertos pelas suas assinaturas, sem extras.
4. Marque `billing_verified = true` somente depois dessa conferência. Inicialmente todos os provedores ficam desabilitados.

O Oráculo considera projetos cadastrados no T3 dentro de `projects_root`. Ao ativá-lo, a descoberta configurada pode iniciar objetivos autonomamente nesses projetos; selecione essa raiz de acordo com os projetos que deseja autorizar.

Para autorizar somente projetos específicos, configure `allowed_projects` no início
do TOML, antes dos blocos `[providers.*]`. A lista contém os caminhos reais exatos
dos projetos dentro de `projects_root`; não inclui automaticamente projetos aninhados.
Lista vazia autoriza nenhum projeto; omitir o campo mantém o comportamento por raiz.

```toml
projects_root = "~/projects/work"
allowed_projects = ["~/projects/work/projeto-a"]
```

Para executar apenas objetivos enviados pelo usuário, defina `discovery = []` no
bloco `[profiles]`, preservando os demais perfis configurados.

## 3. Autenticar e iniciar

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" auth login
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" doctor
./scripts/install-service.sh --start
./scripts/install-service.sh --status
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" status
```

O login é OAuth próprio do Oráculo, com credenciais no Keychain do macOS. Não copie tokens da sessão do T3 para o projeto. `doctor` confere condições locais; teste a conectividade e um objetivo descartável antes de usar projetos importantes.

O LaunchAgent roda na sessão do usuário e reinicia o daemon se necessário. Para continuar com as janelas fechadas, mantenha o Mac acordado e o processo do T3 em execução. Fechar a janela do T3 é diferente de encerrar o aplicativo com `Cmd+Q`. O iPhone acessa a interface do T3; não executa o daemon.

## Parar, atualizar e remover

```bash
./scripts/install-service.sh --stop
# Depois de atualizar o checkout:
./scripts/install-service.sh --install
./scripts/install-service.sh --start
```

Pare o serviço antes de reinstalar, alterar o ambiente Python ou refazer o login OAuth. Parar o daemon não equivale a cancelar agentes remotos: use os comandos de cancelamento e acompanhe a reconciliação quando necessário.

```bash
./scripts/install-service.sh --uninstall
```

A desinstalação descarrega e remove o LaunchAgent. Configuração, vault, estado, credenciais, ambiente Python e logs são preservados.

Comandos de objetivos, revisão de memória e diagnóstico: [Operação](OPERACAO.md).

## Atualização ORA-CONT-003 e rollback

O serviço permanente usa o checkout configurado no LaunchAgent da sua instalação.
Não reinstale a partir de worktree temporário nem inicie um segundo daemon.
Valide testes/revisão e confira `status`: nenhum executor ativo ou incerto e
nenhum job pronto para iniciar durante a transferência. Pare o LaunchAgent,
confirme a parada, preserve backup dos arquivos transferidos, configuração e
SQLite (incluindo estado consistente via backup SQLite), e só então transfira o
delta revisado para o checkout permanente. Verifique hashes de origem/base antes
de sobrescrever: divergência exige nova integração.

A migração de `executor_lease` para slots preserva grants e geração; padrão 1
continua compatível. Para rollback de versão, use juntos código e backup do
SQLite capturados sem atividade. Não restaure banco antigo depois de novos
efeitos: reconcilie primeiro, pois apagar recibos pode duplicar operações.

Após iniciar o único LaunchAgent, confira health, executors, max_executors,
planos, processos e caminho executado. Instalar código não demonstra sucesso
operacional de objetivos. Os resultados desta evolução estão em
[tarefa ORA-CONT-003](VALIDACAO_CONTINUIDADE.md).
