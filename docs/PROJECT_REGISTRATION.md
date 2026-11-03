# Cadastro explícito de um projeto

O daemon prende os bytes de `config.toml`. Incluir um caminho em `allowed_projects` muda esse hash. Não há transação suportada para recarregar a configuração, e um restart depois dessa edição pode parar em `service_state_unproven`. O cadastro abaixo publica uma autorização estreita fora de `config.toml`. O processo que já está no ar lê o arquivo no próximo teste de escopo. Um restart posterior lê o mesmo arquivo e continua conferindo o hash antigo da configuração.

Isto só adiciona. Não remove, não edita `allowed_projects`, não altera reservas, teto, provedores, rotinas ou a proibição de gasto adicional, e não reinicia o serviço.

## Quando a política existe

O arquivo é `state_dir/project-authorization.json`, modo `0600`, criado por substituição atômica. Sem esse arquivo, `allowed_projects` vale exatamente como antes:

- omitido (`null`): qualquer diretório estritamente dentro de `projects_root` continua no escopo;
- lista: só o caminho exato resolvido de cada item;
- lista vazia: nenhum projeto estático.

A política dinâmica só acrescenta diretórios quando `allowed_projects` é uma lista. Ela não amplia a descoberta pela raiz e não autoriza caminho fora de `projects_root`, a própria raiz, `..`, ou um elo simbólico no caminho publicado.

Serviço, tarefas gerenciadas, memória, experiência e a CLI usam `Config.project_in_scope`. O tick continua usando essa função ao cruzar o catálogo T3. `status`, o acesso por ID e a memória direta inserem no estado local um projeto já publicado somente quando ainda não existe linha. Não criam objetivo, concessão ou despacho. Se a linha já existe como `unregistered`, ou com outro caminho, esses acessos não a reescrevem.

## Uso

`--config` fica antes do subcomando. `PROJECT_ID` e o caminho precisam ser os valores do catálogo T3 observado na hora, não um apelido nem um caminho escolhido à parte. O comando busca o catálogo pelo cliente T3 já configurado. Não aceita um arquivo de catálogo no lugar dessa observação.

```bash
oraculo --config "$HOME/.config/oraculo/config.toml" \
  project register plan --id PROJECT_ID --path /caminho/absoluto/do/projeto
```

`plan` não grava a política e não reinicia. A resposta traz `confirmation_required` e o campo `confirmation`. Se o par ID e caminho já foi publicado com o mesmo hash de configuração, a resposta é `already_authorized` e a mesma confirmação.

```bash
oraculo --config "$HOME/.config/oraculo/config.toml" \
  project register apply \
  --id PROJECT_ID \
  --path /caminho/absoluto/do/projeto \
  --confirmation CONFIRMATION
```

`apply` observa o catálogo de novo. A confirmação amarra o ID, o diretório canônico, o hash dessa identidade no catálogo, o hash da política anterior e o hash preso de `config.toml`. Qualquer diferença recusa sem gravar: catálogo ausente, apagado ou ambíguo, caminho diferente, confirmação trocada, política alterada por outro cadastro, configuração sem hash ou com bytes diferentes.

Repetir o mesmo `apply` bem-sucedido devolve `replayed` e não reescreve o arquivo. Dois cadastros concorrentes serializam num lock de dois segundos. O segundo, se usava a confirmação anterior, termina em `registration_confirmation_mismatch` ou `registration_lock_timeout` e não mistura os documentos. O limite de espera não cancela o primeiro.

## Erros

Os códigos são estáveis e não incluem o caminho recusado:

| Código | Recusa |
|---|---|
| `registration_config_unpinned` | configuração não carregada de arquivo, sem hash preso |
| `registration_config_changed` | bytes atuais de `config.toml` diferentes do hash preso |
| `registration_project_unknown` | ID ausente no catálogo observado |
| `registration_project_deleted` | entrada com `deletedAt` |
| `registration_project_ambiguous` | mais de uma entrada com o mesmo ID |
| `registration_identity_mismatch` | caminho pedido diferente do `workspaceRoot` observado |
| `registration_path_outside_root` | alvo fora de `projects_root` |
| `registration_path_is_root` | a própria raiz de projetos |
| `registration_path_symlink` | componente simbólico dentro da raiz |
| `registration_path_invalid` | caminho vazio, relativo, com `..` ou inexistente |
| `registration_path_not_directory` | o caminho existe e não é um diretório |
| `registration_project_id_invalid` | ID vazio, com barra, `..` ou caractere de controle |
| `registration_confirmation_invalid` | confirmação que não é SHA-256 em hex minúsculo |
| `registration_confirmation_mismatch` | confirmação de outro plano, catálogo ou política |
| `registration_duplicate` | mesmo ID ou mesmo caminho com outro vínculo |
| `registration_policy_invalid` | documento ausente de campos, hash, tipo ou limite |
| `registration_policy_symlink` | política ou lock é elo simbólico |
| `registration_lock_timeout` | outra publicação segurou o lock por 2 segundos |
| `registration_state_dir_invalid` | estado ausente, simbólico ou de outro usuário |
| `registration_catalog_invalid` | catálogo que não é a lista T3 esperada |

Uma política inválida não autoriza nenhum caminho dinâmico e não é reescrita. Os caminhos estáticos de `allowed_projects` continuam. Um diretório publicado que depois vira elo para fora da raiz deixa de passar no escopo; os outros diretórios publicados, se ainda resolvem para si mesmos, continuam. O arquivo não é alterado por essa conferência.

## Limites

- Não há comando de revogação. Apagar ou reescrever o arquivo à mão não é o fluxo suportado. Um documento cujo hash ou confirmação não fecha é ignorado por inteiro. O hash interno detecta edição inconsistente; não é uma assinatura contra quem pode reescrever o diretório de estado de forma consistente.
- O tick que já existia ainda pode marcar um projeto como `unregistered` se uma leitura posterior do catálogo T3 não o trouxer, ou se a entrada vier com `deletedAt`. A política não cria uma exceção nesse varredor e não dispara descoberta, inferência nem concessão. Enquanto a linha existir como `unregistered`, `status`, `project` e a memória direta não a devolvem para `registered`, mesmo com a política ainda autorizando o diretório e mesmo com um catálogo em memória velho ou marcado como apagado. Só o tick, ao observar de novo o projeto dentro do escopo, pode registrá-lo outra vez. Uma linha ausente continua podendo ser inserida por esses acessos.
- Um caminho já gravado que não é o da política também não é substituído por `status`, `project` ou memória direta.
- O cadastro não exige vaga livre. Também não libera reservas, não muda o PID e não cura saúde `disconnected` ou outro estado degradado do despacho.
- A configuração precisa ter sido carregada de arquivo (`Config.load`). Um objeto montado só em memória é recusado.
- O caminho gravado é o diretório real, estritamente dentro de `projects_root`, sem componente simbólico. O escopo dinâmico, como a lista estática, compara o diretório exato, não um filho.
- O CLI de produção chama `T3Client.list_projects`. Os testes usam catálogo sintético injetado na função; a CLI não ganhou um argumento para substituir essa observação.
- Este documento descreve o contrato do checkout. Não publica a política no estado real do operador e não ativa uma release. O sdist 0.2.0 inclui este contrato público.
