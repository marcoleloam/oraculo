# Oráculo

<p align="center">
  <img src="assets/branding/oraculo-logo-saturno.png" alt="Logo do Oráculo: hexágono do polo norte de Saturno cercado por raios, órbitas, estrelas e luas dourados sobre fundo escuro" width="280" />
</p>

Serviço local que coordena trabalho nos projetos de `~/projects/work` pelo T3 Code e mantém memória persistente em um vault Obsidian separado.

O Oráculo escolhe Claude Code, Codex ou Grok conforme a tarefa. A execução é serial por padrão e permite até quatro executores, conforme a capacidade configurada e os escopos de escrita. Pode descobrir melhorias e avançar objetivos com verificações e recibos. Usa somente modelos e autenticações de assinaturas existentes explicitamente habilitados na configuração; não oferece fallback para APIs pagas.

A memória funciona com o Obsidian fechado. Aprendizados de projeto podem ser promovidos para a área compartilhada após sanitização pelo agente, por um objetivo de memória específico. O serviço publica a wiki canônica; o plugin Karpathy LLM Wiki trabalha em uma revisão versionada no mesmo vault, importada ao encerrar a revisão quando não há conflito.

O fluxo de [tarefas geridas](docs/MANAGED_TASKS.md) resolve o projeto autorizado, consulta sua memória e abre um contexto com branch e worktree próprios. Mantém o vínculo com a conversa de origem até a validação, publicação do PR, checks, merge e encerramento. Trabalho concluído, memória salva, PR aberto, merge e implantação são estados separados. A entrada depende de um adapter cooperativo na origem; este pacote não instala um hook global no T3. A preparação e o push do código revisado continuam sob responsabilidade da origem.

## V1 validada

A V1 funcional foi aceita em 19/10/2026: **27/27 critérios aprovados**, incluindo continuidade com a janela do T3 fechada e revisão isolada no Obsidian. O [resumo público da V1](docs/VALIDACAO_V1.md) registra resultados, escopos e limitações. A entrega foi validada em destino local descartável; a instalação no ambiente de uso segue os passos abaixo.

## Preparação local

Requisitos: macOS com sessão de usuário ativa, Python 3.12, `uv`, Git, T3 Code acessível por MCP e assinaturas existentes dos três provedores. Para revisão visual, Obsidian e plugin Karpathy LLM Wiki 1.28.0. O serviço depende do Mac ligado e acordado.

Para obter o código:

```bash
git clone https://github.com/marcoleloam/oraculo.git
cd oraculo
```

Na raiz deste checkout:

```bash
uv sync --locked --python 3.12
uv run --frozen oraculo --help
./scripts/install-service.sh --dry-run
```

Para preparar configuração, estado, vault e LaunchAgent, sem iniciar o serviço:

```bash
./scripts/install-service.sh --install
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" doctor
```

Edite `~/.config/oraculo/config.toml`: substitua `https://t3.example.invalid/mcp` pelo endereço MCP da sua instância T3; confirme os IDs reais de provedores/modelos no T3 e a cobertura pelas assinaturas. A configuração inicial contém `billing_verified = false` e `models = []`: nenhum provedor fica elegível até essa configuração. Faça o login OAuth próprio do Oráculo antes de iniciar:

```bash
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" auth login
./scripts/install-service.sh --start
uv run --frozen oraculo --config "$HOME/.config/oraculo/config.toml" status
```

A ativação permite descoberta e execução autônomas nos projetos cadastrados no T3 dentro da raiz configurada. `doctor` verifica condições locais; não comprova autenticação, cota ou conectividade. O aceite funcional da V1 está registrado no resumo de validação; cada instalação requer configuração e ativação próprias.

## Documentação e validação

- [Operação](docs/OPERACAO.md): configuração, comandos, revisão, entrega e recuperação.
- [Tarefas geridas](docs/MANAGED_TASKS.md): projeto, isolamento, memória, retorno à origem, PR e merge.
- [Encerramento verificado](docs/ENCERRAMENTO.md): vínculo da entrega, critérios e confirmação de fechamento.
- [Validação](docs/VALIDACAO_ORACULO.md): evidências necessárias para integrações reais e aceite.
- [Validação da V1](docs/VALIDACAO_V1.md): resultados, escopos e limitações.
- [Bateria AT-020](docs/TESTES_AT020.md): testes reproduzíveis de continuidade.
- [Instalação no Mac](docs/INSTALACAO.md): o que é instalado, como configurar, iniciar e parar.

Verificação automatizada do checkout:

```bash
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q
```

Os testes locais não substituem os cenários reais de OAuth, provedores, plugin, iPhone e entrega descritos na validação.

## Licença

Código distribuído sob a [licença MIT](LICENSE). Integrações externas como T3, Obsidian, plugins e provedores continuam sujeitas às respectivas licenças e assinaturas.
