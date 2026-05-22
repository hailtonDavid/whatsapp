# WhatsApp Web Automation v4 — varredura automática por grupos/contatos/telefones

Repositórios:

- GitHub: https://github.com/hailtonDavid/whatsapp
- Gitea: http://localhost:3000/hailtonDavid/whatsapp

Esta versão permite configurar uma lista de grupos, contatos ou telefones em um arquivo JSON.
O programa abre cada conversa automaticamente, baixa as mensagens visíveis, rola o histórico e repete o ciclo.

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
      "enabled": true
    },
    {
      "id": "cliente_joao",
      "type": "contact",
      "name": "João Silva",
      "enabled": true
    },
    {
      "id": "telefone_maria",
      "type": "phone",
      "phone": "5562999999999",
      "enabled": true
    }
  ]
}
```

### Campos

- `id`: identificador interno usado no nome dos arquivos. Não use acentos nem espaços.
- `type`: `group`, `contact` ou `phone`.
- `name`: nome do grupo ou contato exatamente como aparece no WhatsApp Web.
- `phone`: telefone com DDI e DDD, somente números. Exemplo: `5562999999999`.
- `enabled`: `true` ou `false`.

## Rodar uma vez

```powershell
python src\whatsapp_auto_downloader.py run-once --targets config\targets.json
```

## Rodar varredura contínua

```powershell
python src\whatsapp_auto_downloader.py scan --targets config\targets.json
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

## Importante

Use apenas em conta própria ou em ambiente com autorização explícita.
Este projeto não burla QR Code, autenticação, criptografia, bloqueios ou mecanismos de segurança.
Para uso empresarial oficial, integração com atendimento, webhooks e alto volume, prefira a WhatsApp Business Platform / Cloud API.
