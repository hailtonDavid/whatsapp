# WhatsApp Web Automation v4 — varredura automática por grupos/contatos/telefones

Repositórios:

- GitHub: https://github.com/hailtonDavid/whatsapp
- Gitea: http://localhost:8030/hailtonDavid/whatsapp

Esta versão permite configurar uma lista de grupos, contatos ou telefones em um arquivo JSON.
O programa abre cada conversa automaticamente, baixa as mensagens visíveis, rola o histórico e repete o ciclo.
Também inclui uma opção de envio de mensagem seguindo o mesmo padrão de alvos da leitura.

## Fluxo

1. Abre o WhatsApp Web usando um perfil persistente próprio: `profile_whatsapp_v4`.
2. Permite login normal por QR Code.
3. Lê `config/targets.json`.
4. Para cada alvo habilitado:
   - abre a conversa;
   - captura mensagens;
   - rola o histórico;
   - salva apenas mensagens novas;
   - continua para o próximo alvo.
5. Repete o ciclo a cada `interval_seconds`.

## Envio de mensagens

O envio usa o mesmo arquivo `config/targets.json`. Para evitar disparos acidentais, o comando `send-once` roda em modo simulação por padrão. O envio real só acontece quando `--confirm` é informado. Quando `--message` for usada sem `--target-id`, também é necessário informar `--all`.

### Enviar uma mensagem informada pela linha de comando para um alvo

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --target-id numero_62_999488167 --message "Olá, mensagem de teste." --confirm
```

### Simular antes de enviar

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --target-id numero_62_999488167 --message "Olá, mensagem de teste."
```

### Enviar uma mensagem global para todos os alvos habilitados

Para evitar disparo acidental, mensagem global exige `--all` explicitamente:

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --message "Olá, mensagem de teste." --all --confirm
```

### Enviar mensagens configuradas no JSON

No alvo desejado, configure:

```json
"send": {
  "enabled": true,
  "message": "Olá, esta é uma mensagem de teste."
}
```

Depois execute:

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --confirm
```

Os registros de envio ficam em:

```text
exports/send/sent_log.jsonl
exports/send/last_send.json
```

### Validação real do envio

Nesta versão, o comando `send-once` não considera a mensagem enviada apenas porque apertou Enter ou clicou no botão. O script agora:

1. confirma que a conversa ficou pronta para envio;
2. confirma que o texto entrou na caixa correta do chat;
3. clica no botão de envio ou usa Enter;
4. verifica se a mensagem apareceu no chat como mensagem enviada.

Se não conseguir confirmar visualmente a mensagem no chat, o resultado fica com `ok=false` e é gerado um screenshot em:

```text
exports/send/debug/
```


## Instalação

```powershell
cd D:\Sistemas\whatsapp
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
copy config\targets.example.json config\targets.json
```

## Configuração

Edite:

```text
config/targets.json
```

Exemplo:

```json
{
  "interval_seconds": 60,
  "scrolls_per_target": 8,
  "delay_between_scrolls": 1.0,
  "delay_between_targets": 2.0,
  "append_only_new_messages": true,
  "targets": [
    {
      "id": "grupo_familia",
      "type": "group",
      "name": "Família",
      "enabled": true,
      "send": {
        "enabled": false,
        "message": "Olá, esta é uma mensagem de teste."
      }
    },
    {
      "id": "cliente_joao",
      "type": "contact",
      "name": "João Silva",
      "enabled": true,
      "send": {
        "enabled": false,
        "message": "Olá, João. Esta é uma mensagem de teste."
      }
    },
    {
      "id": "numero_62_999488167",
      "type": "phone",
      "phone": "5562999488167",
      "enabled": true,
      "send": {
        "enabled": false,
        "message": "Olá, esta é uma mensagem de teste."
      }
    }
  ]
}
```

### Campos

- `id`: identificador interno usado no nome dos arquivos. Não use acentos nem espaços.
- `type`: `group`, `contact` ou `phone`.
- `name`: nome do grupo ou contato exatamente como aparece no WhatsApp Web.
- `phone`: telefone com DDI e DDD, somente números. Exemplo: `5562999488167`.
- `enabled`: `true` ou `false`.
- `send.enabled`: habilita ou não aquele alvo para envio por configuração.
- `send.message`: mensagem que será enviada quando `send.enabled=true`.

## Rodar uma vez

```powershell
python src\whatsapp_auto_downloader.py run-once --targets config\targets.json
```

## Rodar varredura contínua

```powershell
python src\whatsapp_auto_downloader.py scan --targets config\targets.json
```

## Enviar mensagem

Simular:

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --target-id numero_62_999488167 --message "Olá, mensagem de teste."
```

Enviar de fato:

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --target-id numero_62_999488167 --message "Olá, mensagem de teste." --confirm
```

## Diagnóstico

```powershell
python src\whatsapp_auto_downloader.py doctor
```

## Desbloquear perfil

```powershell
python src\whatsapp_auto_downloader.py unlock-profile --kill --kill-playwright --remove-locks
```

## Onde ficam as mensagens

Cada alvo gera dois arquivos:

```text
exports/messages/grupo_familia.jsonl
exports/messages/grupo_familia_latest.json
```

Também existe um estado de deduplicação:

```text
state/message_state.json
```

## Git — sincronizar GitHub e Gitea

Um único push envia para os dois remotos:

```powershell
git push origin main
```

Ou use o atalho:

```powershell
.\scripts\git-push-both.ps1
```

Para (re)configurar os remotos:

```powershell
.\scripts\git-ensure-dual-remotes.ps1
```

## Docker — stack completa (app + bancos)

Sobe o painel Flask, MongoDB e PostgreSQL (pgvector) com um comando:

```powershell
.\rodar_docker.bat
# ou
docker compose up -d --build
```

| Serviço | URL / porta |
|---------|-------------|
| Painel | http://127.0.0.1:5014/painel |
| MongoDB | `mongodb://127.0.0.1:27020/whatsapp` |
| PostgreSQL | `postgresql://whatsapp:whatsapp@127.0.0.1:5434/whatsapp` |

Parar tudo: `.\parar_docker.bat` ou `docker compose down`.

Configuração opcional: copie `.env.docker.example` para `.env.docker` (criado automaticamente pelo `rodar_docker.bat`).

Volumes persistentes: perfil WhatsApp, exports, state, cache de embeddings, dados dos bancos.

> **WhatsApp Web no container:** o Playwright usa Chromium embutido (`WA_BROWSER_CHANNEL=none`). A sessão fica no volume `whatsapp_profile`. Na primeira execução, conecte via QR no painel (Autenticação). Em alguns ambientes headless o QR pode exigir `WA_HEADLESS=false` temporariamente — ajuste em `.env.docker` e recrie o container.

### Apenas bancos (desenvolvimento local)

Se preferir rodar o Flask no Windows (`.\rodar_flask.bat`) e só os bancos no Docker:

```powershell
docker compose up -d mongo postgres
# ou
.\rodar_mongo.bat
```

No `.env` local:

```env
MONGODB_URI=mongodb://localhost:27020/whatsapp
MONGODB_DB=whatsapp
SEMANTIC_DB_URI=postgresql://whatsapp:whatsapp@localhost:5434/whatsapp
SEMANTIC_EMBEDDING_PROVIDER=fastembed
SEMANTIC_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
SEMANTIC_EMBEDDING_DIM=384
```

> **Porta 27020** evita conflito com outros MongoDB locais (ex.: na 27017).

Verifique no painel: status **MongoDB conectado** e **pgvector conectado**.

### Busca semântica (PostgreSQL + pgvector)

Mensagens salvas no MongoDB são indexadas com embeddings para busca por **significado** (ex.: “boleto”, “atraso na entrega”, “desconto”).

API: `POST /api/semantic/search`, `POST /api/semantic/reindex`, `GET /api/semantic/status`.

| Camada | Função |
|--------|--------|
| MongoDB (27020) | Armazenar mensagens escolhidas |
| Postgres + pgvector (5434) | Busca semântica / RAG |

## Importante

Use apenas em conta própria ou em ambiente com autorização explícita.
Não use para spam, disparo em massa ou contatos sem autorização.
Este projeto não burla QR Code, autenticação, criptografia, bloqueios ou mecanismos de segurança.
Para uso empresarial oficial, integração com atendimento, webhooks e alto volume, prefira a WhatsApp Business Platform / Cloud API.

## Envio robusto v7

A versão v7 altera o envio para não considerar sucesso apenas porque o botão foi clicado. Agora o script:

1. abre a conversa;
2. localiza a caixa real de mensagem no rodapé do chat;
3. insere ou valida a mensagem já preenchida pela URL oficial do WhatsApp Web;
4. clica no botão real de envio;
5. confirma se a mensagem apareceu como mensagem enviada no histórico;
6. verifica se ela não ficou pendente com ícone de relógio.

### Enviar para número

No `config/targets.json`, use telefone em formato internacional, sem espaços:

```json
{
  "id": "numero_62_999488167",
  "type": "phone",
  "phone": "5562999488167",
  "enabled": true,
  "send": {
    "enabled": true,
    "message": "Olá, esta é uma mensagem de teste."
  }
}
```

Comando:

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --target-id numero_62_999488167 --confirm
```

### Enviar para grupo

No `config/targets.json`, use o nome exatamente como aparece no WhatsApp Web:

```json
{
  "id": "grupo_teste",
  "type": "group",
  "name": "NOME EXATO DO GRUPO AQUI",
  "enabled": true,
  "send": {
    "enabled": true,
    "message": "Olá, esta é uma mensagem de teste enviada para o grupo."
  }
}
```

Comando:

```powershell
python src\whatsapp_auto_downloader.py send-once --targets config\targets.json --target-id grupo_teste --confirm
```

### Diagnóstico de falha

Quando o envio não for confirmado, o script salva print da tela em:

```text
exports/send/debug/
```

E o resumo fica em:

```text
exports/send/last_send.json
```

Se `verification` vier como `visible_but_pending`, a mensagem apareceu no chat, mas o WhatsApp ainda não confirmou envio ao servidor. Nesse caso, verifique conexão do celular, WhatsApp Web e internet.

## Inventário automático de grupos v8

A versão v8 adiciona o comando `list-groups`, que tenta localizar os grupos disponíveis na sessão atual do WhatsApp Web e gera dois arquivos:

```text
exports/groups/groups.json
exports/groups/groups_targets_template.json
```

O primeiro arquivo é o inventário técnico dos grupos encontrados. O segundo já vem no mesmo padrão do `targets.json`, com todos os grupos como `enabled=false` e `send.enabled=false` para evitar envio ou leitura automática sem revisão.

### Gerar lista de grupos

```powershell
python src\whatsapp_auto_downloader.py list-groups --print-names
```

### Gerar em caminhos específicos

```powershell
python src\whatsapp_auto_downloader.py list-groups --output exports\groups\groups.json --targets-output config\grupos_detectados.json --print-names
```

Depois de validar os grupos desejados, você pode copiar os itens de `config\grupos_detectados.json` para `config\targets.json` e alterar manualmente:

```json
"enabled": true
```

Para permitir envio por configuração, altere também:

```json
"send": {
  "enabled": true,
  "message": "Sua mensagem aqui"
}
```

Observação: o comando depende da sessão autorizada do WhatsApp Web e da sincronização local. Se o WhatsApp Web estiver sincronizando conversas, deixe terminar e execute novamente.
