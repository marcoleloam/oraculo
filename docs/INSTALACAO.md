# Instalação e manutenção

O pacote usa Python 3.12 já instalado. O CLI funciona em macOS e Linux;
serviço contínuo via LaunchAgent é suportado somente no macOS. Nenhum comando
de instalação baixa Python, autentica contas, habilita provedores, muda
capacidade ou edita perfis do shell.

## Preparar um artefato verificável

Execute em fontes revisadas, com `uv` e Python 3.12 disponíveis. O wheel inclui
prompts, configuração de exemplo, templates e a skill Oráculo. O sdist usa uma
allowlist e exclui documentos privados de tarefas, contexto e recibos.

```sh
uv sync --locked --python 3.12
uv build --out-dir dist
mkdir bundle
uv export --frozen --no-dev --no-emit-project --format requirements-txt \
  --output-file bundle/requirements.txt
uv tool run --from pip pip download --require-hashes --only-binary=:all: \
  --dest bundle -r bundle/requirements.txt
cp dist/oraculo-*.whl bundle/
```

O wheelhouse precisa corresponder ao sistema, arquitetura e Python de destino.
Não reutilize wheels macOS no Linux. Acrescente o wheel local às dependências
fixadas, calculando seu hash, e sele o inventário:

```sh
.venv/bin/python - <<'PY'
import hashlib
from pathlib import Path
wheel = next(Path('bundle').glob('oraculo-*.whl'))
version = wheel.name.split('-')[1]
with Path('bundle/requirements.txt').open('a') as out:
    out.write(f'\noraculo=={version} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n')
PY
.venv/bin/python -m oraculo.runtime manifest bundle \
  --release-id RELEASE --source-commit "$(git rev-parse HEAD)" \
  --schema-min 4 --schema-max 4
```

`manifest.json` v1 contém release, commit de origem, versão Python, intervalo de
schema e SHA256 de cada arquivo relativo. Confira e distribua esse manifesto por
um canal confiável junto do bundle e do bootstrap revisado. Hashes detectam
corrupção; não autenticam um distribuidor malicioso que substitua também o
manifesto. Registre o SHA do manifesto revisado fora do bundle.

## Instalar sem checkout permanente

```sh
ORACULO_PYTHON=/caminho/python3.12 ./scripts/install-cli.sh /caminho/bundle \
  --dry-run
ORACULO_PYTHON=/caminho/python3.12 ./scripts/install-cli.sh /caminho/bundle
```

O bootstrap extrai o instalador do wheel verificado. `runtime install` e
`runtime upgrade --bundle ...` preparam releases sem alterar a seleção ativa.
A instalação usa somente wheels locais e hashes fixados. Os destinos padrão são
`~/.local/share/oraculo/releases/RELEASE` e `~/.local/bin/oraculo`. Releases
prontas ficam sem permissão de escrita; verificação posterior detecta mudanças,
inclusive no interpretador externo. Não remova o Python de base enquanto alguma
release depender dele.

A seleção inicial é explícita e não inicia o serviço:

```sh
"$HOME/.local/share/oraculo/releases/RELEASE/venv/bin/python" -B \
  -m oraculo.runtime runtime activate RELEASE
export PATH="$HOME/.local/bin:$PATH"
command -v oraculo
oraculo --help
oraculo doctor
oraculo runtime status
```

Abra um shell novo com o diretório de bin no PATH. Se não estiver no PATH, use o
caminho absoluto e ajuste o perfil manualmente. O launcher usa `exec`, preserva
argumentos, stdin/stdout, sinais e exit code, sem depender de venv ativa ou cwd.
Um launcher/plist alheio ou symlink inesperado bloqueia a operação sem sobrescrita.
Reinstalar a mesma release e o mesmo manifesto é idempotente; reutilizar seu ID
com outro conteúdo é conflito.

Para ensaios, todos os comandos aceitam `--root`, `--bin-dir`, `--label`,
`--plist-dir`, `--log-dir`, `--timeout` e `--dry-run` após a ação. A configuração
é opção global: `oraculo --config /caminho/config.toml runtime ...`. Use todos os
destinos isolados, inclusive configuração, estado e socket. `--dry-run` não
escreve arquivos nem solicita manutenção. `doctor` e `runtime status` são
consultas; configuração ausente produz diagnóstico.

## Configurar e iniciar

Inicialização de configuração/vault/estado é separada da instalação e exclusiva
de uma instalação nova. `init` recusa estado, ledger ou socket existentes; nunca
o use como upgrade. Para layout existente, siga a migração ou manutenção abaixo:

```sh
oraculo --config "$HOME/.config/oraculo/config.toml" init
```

Confira `projects_root`, `allowed_projects`, `vault_root` e `state_dir`. Vault e
estado ficam fora da raiz de projetos. `allowed_projects = []` não autoriza
nenhum projeto; omitir a lista mantém descoberta pela raiz. Para não descobrir
objetivos automaticamente, use `discovery = []` em `[profiles]`.

Substitua a URL MCP de exemplo, consulte IDs reais de provedores/modelos no host
e habilite apenas assinaturas verificadas. Login OAuth é explícito, com
credenciais no Keychain do macOS; não copie tokens para o projeto.

```sh
oraculo auth login
oraculo doctor
oraculo service start
oraculo service status
oraculo status
```

O plist usa o Python absoluto da release, configuração absoluta e variáveis de
identidade. `service status` diferencia unloaded, loaded, starting, online,
stopping, failed/unknown. Loaded não prova disponibilidade: online exige socket,
PID, release, hash/caminho da configuração e protocolo correspondentes. A parada
espera remoção de launchd, término do PID anterior e desaparecimento do socket,
com prazo finito. Não apaga um socket antigo para forçar progresso.

O plist também fixa o SHA256 da configuração esperada. A partida confere esse
hash antes de bootstrap, e o próprio daemon confere os bytes carregados antes de
abrir State ou publicar socket. Uma troca concorrente de configuração bloqueia
a partida; se ocorrer dentro de bootstrap, o processo recusa a configuração e
o job pode permanecer carregado sem serviço saudável. Restaure a configuração
registrada antes de usar a recuperação oficial; não apague o journal. Um novo
start após uma operação concluída adquire uma nova manutenção, inclusive quando
chamado por restart com o serviço já parado.

## Upgrade e recuperação

```sh
oraculo runtime upgrade --bundle /caminho/novo-bundle --dry-run
oraculo runtime upgrade --bundle /caminho/novo-bundle
oraculo runtime activate NOVA_RELEASE --dry-run
oraculo runtime activate NOVA_RELEASE
oraculo runtime status
oraculo service status
```

Ativação exige manutenção exclusiva e quiescência comprovada pela interface
oficial. Admissão permanece fechada durante parada, troca atômica de `current`,
atualização do plist, partida e probes. Capacidade e configuração são preservadas.
Se o serviço estava parado, permanece parado. Sem daemon, a interface oficial
adquire locks e verifica estado; estado desconhecido bloqueia.

O arquivo privado `operation.json`, no root de instalação, registra intenção,
release anterior, token de manutenção e etapas antes dos efeitos. Não publique
esse arquivo. Interrupção mantém dados e recibos intactos. Repetir `runtime activate` com o
mesmo target retoma a operação registrada. Consulte status e, para retornar,
recupere a release anterior compatível:

```sh
oraculo runtime rollback --dry-run
oraculo runtime rollback
```

Downgrade incompatível com o schema atual é bloqueado. Configuração alterada
concorrentemente precisa ser reconciliada antes de recuperação. Falha na partida,
identidade desconhecida ou rollback que também falha mantém manutenção e um
estado de recuperação explícito. Não restaure um banco antigo sobre efeitos
novos. Não existe promessa de rollback automático de efeitos externos.

Se o bootstrap carregou a substituição, mas ela não responde com saúde válida,
`runtime rollback` usa o journal para parar somente essa substituição. Também é
possível executar `service stop` antes de `runtime rollback`. A recuperação exige
o plist administrado exato, manifesto íntegro, target launchd, programa, argumentos,
ambiente e configuração registrados. Depois de `bootout`, comprova job descarregado,
PIDs anteriores mortos e socket ausente; então readquire a guarda offline com a
mesma operação e token. Identidade desconhecida, token diferente ou socket residual
bloqueiam sem forçar limpeza. `service start` não contorna uma ativação pendente.
`service restart`, inclusive quando já parado, usa a mesma validação de
configuração, guarda offline e registro de identidade anterior ao bootstrap.
O primeiro início e um novo início após uma operação concluída também criam
essa operação antes de carregar launchd. Estado ainda ausente exige concluir
`oraculo init`; trabalho existente ativo/incerto bloqueia o bootstrap.

Se a manutenção terminou, mas a gravação final do journal falhou, repetir a
operação readquire o token do último encerramento confirmado. A readmissão só
ocorre após nova prova de quiescência; trabalho iniciado nesse intervalo bloqueia
a recuperação. Outra operação de manutenção concluída substitui esse recibo e
impede reutilizar silenciosamente uma identidade antiga. O hash registrado vem
dos mesmos bytes usados para carregar a configuração, inclusive na migração.
Se houve tentativa de encerrar manutenção e o serviço ficou não saudável,
o journal sozinho não prova que a admissão continua fechada. A parada de
recuperação exige prova atual de manutenção via socket antes de bootout; socket
indisponível, admissão reaberta ou token divergente bloqueiam essa parada.

## Migrar o layout legado, explicitamente e offline

Um layout com `current -> releases/ANTIGA`, `.venv` e plist sem marcador de ownership
não é uma release verificada. Preparar diretamente nessa raiz permanece bloqueado.
Use primeiro outro root e outro bin-dir para instalar o bundle revisado:

```sh
ORACULO_PYTHON=/caminho/python3.12 ./scripts/install-cli.sh /caminho/bundle \
  --root /caminho/candidato --bin-dir /caminho/candidato/bin
```

Antes da janela de manutenção, registre o valor **literal** de `readlink current`,
o SHA256 dos bytes do plist legado e o PID observado do processo antigo. Inspecione
o plist: label, Python em `.venv`, working directory e `--config` precisam ser os
da instalação selecionada. Um hash fornecido não autoriza converter um plist de
outro programa. Reconcilie trabalho ativo/incerto pela interface oficial antes de
parar o serviço legado. A parada continua sendo uma ação operacional separada;
exit 0 de `bootout` não comprova término. Não altere capacidade, reservas ou banco
para satisfazer a guarda.

Para o label padrão, depois de conferir plist/PID e quiescência, a parada
legada usa o próprio launchd (ajuste label e caminho apenas conforme os registros):

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.oraculo.plist"
launchctl print "gui/$(id -u)/local.oraculo"
```

A segunda consulta deve confirmar que o job não existe; outro erro não prova
parada. Aguarde também término do PID registrado e ausência do socket configurado.
O `migrate-legacy --dry-run` abaixo verifica esses três probes e bloqueia se a
parada ainda não terminou. Não use o wrapper novo `install-service.sh --stop` para
esse plist legado: o controlador novo exige ownership e retorna `plist_conflict`.
Não remova o socket, mate processo desconhecido nem altere o plist para superar
esse gate. Se o prazo operacional expirar, preserve os registros e investigue.

Execute o novo código pelo Python do candidato **somente depois** da parada.
Substitua os valores abaixo pelos registros conferidos; `--previous-pid` é o PID
anterior real, maior que 1, mesmo que ele já tenha terminado:

```sh
/caminho/candidato/releases/NOVA/venv/bin/python -B -m oraculo \
  --config /caminho/config.toml runtime migrate-legacy \
  --root /caminho/instalacao --bin-dir /caminho/bin \
  --candidate-root /caminho/candidato --release-id NOVA \
  --label local.oraculo --plist-dir /caminho/LaunchAgents \
  --log-dir /caminho/logs \
  --expected-legacy-pointer 'releases/ANTIGA' \
  --expected-plist-sha256 SHA256_CONFERIDO --previous-pid PID_ANTERIOR \
  --dry-run
```

Repita sem `--dry-run` para executar. O dry-run valida arquivos e probes de parada;
não adquire manutenção nem certifica quiescência. A execução exige job descarregado,
PID morto e socket ausente; adquire locks exclusivos das duas raízes e a guarda
oficial offline do daemon/ledger. Estado ausente, ocupado ou desconhecido bloqueia.

O comando registra `legacy-migration.json`, copia os bytes do plist e o texto do
ponteiro para `legacy-backups/OPERATION_ID/`, copia e verifica o candidato na raiz
final, troca `current` e cria um **novo** plist administrado e launcher. Preserva
todos os arquivos da release antiga e os bytes da configuração. O journal e os
backups são privados. Não cria manifesto para a release antiga nem a adota.
Sob a mesma guarda, `State.backup` grava um snapshot consistente, privado, com
SHA256 no journal. Esse snapshot já é **schema 4**, pois a abertura oficial do
estado pode migrá-lo antes do backup; não é backup pré-migração de schema 3.

Após sucesso, o serviço continua parado e a admissão permanece fechada. Confira
o recibo `migrated` e inicie explicitamente usando os mesmos destinos:

```sh
/caminho/bin/oraculo --config /caminho/config.toml service start \
  --root /caminho/instalacao --bin-dir /caminho/bin \
  --label local.oraculo --plist-dir /caminho/LaunchAgents --log-dir /caminho/logs
```

`service start` confirma a identidade nova e encerra a manutenção correlacionada
de `service-operation.json`. Confira saúde, release e capacidade efetiva 3 pelo
status oficial. Se a migração for interrompida, repita **o mesmo comando** com os
mesmos valores: ele retoma o mesmo journal/token e conclui para a frente, inclusive
após trocar o ponteiro ou arquivar o plist. Não apague journals/locks nem restaure
o plist antigo para contornar um bloqueio. `runtime activate` e `service start`
recusam uma migração incompleta.
Se a primeira partida da release migrada falhar deixando o job carregado, use
`service stop` com os mesmos destinos. A recuperação consulta o journal de serviço
e mantém a manutenção. Prepare uma correção compatível com schema 4 e execute
`runtime activate CORRIGIDA`; ele reutiliza a manutenção da parada confirmada.

**Limite de rollback:** a guarda oficial pode migrar o estado para schema 4 antes
da troca de arquivos. A partir desse passo, código legado que só lê schema 3 não
pode ser reiniciado com segurança, mesmo se o restante da migração falhar. Os
backups de ponteiro/plist preservam evidência; não são um mecanismo de downgrade.
Não existe rollback automático para essa release não verificada, nem restauração
de banco antigo. Retome a migração ou prepare uma release corrigida compatível
com schema 4. Depois de completar a migração, upgrades entre releases verificadas
voltam a usar o rollback compatível descrito acima.

## Parada e remoção

```sh
oraculo service stop
oraculo service restart
oraculo service uninstall
```

Parada administrativa fecha admissão após comprovar quiescência. `start` retoma a
manutenção registrada pela parada; restart só faz bootstrap depois da parada
confirmada. Desinstalação remove apenas o plist administrado após parada, mantendo
configuração, estado, memória, credenciais, logs e releases. O script legado
`install-service.sh` encaminha start/stop/restart/status/uninstall ao CLI.

Novas ações usam exit 0 para sucesso observado, 1 para falha/bloqueio operacional
e 2 para entrada inválida. Recibos JSON incluem versão, estado, error_code,
retryability, recovery_action, operation_id, fonte e instante. Não confunda
instalação com ativação, saúde do daemon com sucesso do trabalho, ou testes de
launchd com validação de integração dos provedores.
