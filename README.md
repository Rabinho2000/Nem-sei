# Monitoring Board Local

Aplicacao local em Flask para acompanhar instalacoes, monitorizacao diaria,
contratos, tickets corretivos, exportacoes e integracoes.

## Arranque

1. Criar e ativar um ambiente Python, se necessario.

2. Instalar dependencias:

```powershell
pip install -r requirements.txt
```

3. Criar a configuracao local:

```powershell
Copy-Item .env.example .env
```

4. Preencher `.env` com os valores locais, incluindo as credenciais atuais do
   FusionSolar e uma password de login para a app.

5. Arrancar a app:

```powershell
python app.py
```

6. Abrir no browser:

`http://127.0.0.1:5000`

Por defeito a app escuta apenas em `127.0.0.1`. Para expor na rede local, usa
explicitamente `python app.py --host 0.0.0.0`.

## Seguranca local

- O login usa `APP_USERNAME` e `APP_PASSWORD` ou `APP_PASSWORD_HASH` no `.env`.
- Se a password da app nao estiver configurada, o login fica bloqueado.
- Todos os formularios `POST` sao protegidos por CSRF.
- A password FusionSolar nao e mostrada no ecra. Se `FUSIONSOLAR_PASSWORD`
  estiver definida em `.env`, esse valor tem prioridade e nao e copiado para a
  base de dados.
- Os logs ficam em `logs/monitoring_board.log`, fora do Git. Se `DATA_DIR`
  estiver definido, ficam em `DATA_DIR/logs/monitoring_board.log`.

## Estrutura

- `app.py`: app Flask, rotas principais e bootstrap da base local.
- `monitoring_board/routes/`: blueprints separados, com autenticacao em `auth.py`.
- `monitoring_board/services/`: regras de dominio reutilizaveis, como helpers FusionSolar.
- `monitoring_board/db.py`: helpers SQLite, backups e queries pequenas.
- `tests/`: testes basicos de seguranca, DB e services.

## Testes

```powershell
python -m pytest -q
```

Checks adicionais de release para reporting:

```powershell
python -m ruff check monitoring_board tests
python -m compileall monitoring_board
python -m pip check
python -m monitoring_board.reporting_storage_check --database .\data\monitoring_board.db --root .\data\uploads\generated_reports --dry-run
```

Guia operacional: [docs/reporting_operations.md](docs/reporting_operations.md).

Modelos financeiros carregados por instalacao ficam em
`DATA_DIR/uploads/financial_models/<asset_id>/` e sao referenciados pela tabela
`source_files`. Um backup apenas da base SQLite nao inclui estes ficheiros
originais. Para um backup completo, inclui tambem `DATA_DIR/uploads`; o script
`scripts/backup.sh` faz isso quando `INCLUDE_UPLOADS=1`.

## Integracao FusionSolar

No `.env`, preencher as credenciais e, se quiseres controlar os jobs diarios
por variavel de ambiente:

```text
FUSIONSOLAR_PRODUCTION_SYNC_ENABLED=true
FUSIONSOLAR_PRODUCTION_SYNC_TIME=00:10
FUSIONSOLAR_DIAGNOSTICS_SYNC_ENABLED=true
FUSIONSOLAR_DIAGNOSTICS_SYNC_TIME=00:30
```

## Integracao Sigenergy

Esta fase suporta apenas monitorizacao atual Sigenergy: autenticacao por App
Key/App Secret, lista de instalacoes e `energyFlow` atual. Ainda nao inclui
historico de producao, alarmes, inversores, strings, disponibilidade por
inversor ou controlo remoto.

No `.env`, preencher:

```text
SIGENERGY_ENABLED=true
SIGENERGY_APP_KEY=
SIGENERGY_APP_SECRET=
SIGENERGY_BASE_URL=https://api-eu.sigencloud.com
SIGENERGY_AUTH_ENDPOINT=/openapi/auth/login/key
SIGENERGY_SYSTEMS_ENDPOINT=/openapi/system
SIGENERGY_ENERGY_FLOW_ENDPOINT=/openapi/systems/{system_id}/energyFlow
SIGENERGY_ONBOARD_ENDPOINT=/openapi/board/onboard
SIGENERGY_REGION=eu
SIGENERGY_STATE_SYNC_INTERVAL_HOURS=1
SIGENERGY_SNAPSHOT_RETENTION_DAYS=90
```

Se a conta nao devolver a lista de sistemas, usar `SIGENERGY_SYSTEM_IDS` com os
IDs separados por virgula. Nao enviar App Secret, tokens, `.env`, bases de
dados, logs ou exports para Git.

Na interface, abrir `Integracoes > Sigenergy`, guardar a configuracao, usar
`Testar ligacao` para validar autenticacao/lista/energy flow sem escrever dados
e `Sincronizar agora` para gravar snapshots e registos de monitorizacao.

Para pedir acesso a uma nova instalacao, usar o bloco `Onboarding Sigenergy` na
mesma pagina e enviar um unico System ID. O pedido chama
`POST /openapi/board/onboard` com payload `["SYSTEM_ID"]`; a app guarda o
codigo e mensagem devolvidos pelo provider sem tokens nem secrets. O proprietario
da instalacao podera ter de aprovar o acesso na Sigenergy. Usar `Atualizar
estado` ou uma sincronizacao futura para reconciliar: quando o System ID aparecer
em `/openapi/system`, o pedido passa para `approved`.

Estados de onboarding principais:

- `requested`: pedido enviado.
- `already_requested_or_onboarded`: codigo conservador para respostas como
  `1401` ate o sistema aparecer na lista autorizada.
- `approved`: sistema encontrado na lista Sigenergy.
- `failed`: pedido rejeitado ou erro do provider.

Nao existe suporte a Bearer token estatico (`SIGENERGY_BEARER`); a integracao
usa sempre App Key/App Secret, token temporario em cache e renovacao automatica
apos HTTP 401. Se parte das chamadas `energyFlow` falhar, a sincronizacao fica
com estado `partial`, preserva o ultimo estado valido e guarda o erro sanitizado.
Snapshots Sigenergy sao limpos uma vez por dia conforme
`SIGENERGY_SNAPSHOT_RETENTION_DAYS`, mantendo sempre o snapshot mais recente de
cada sistema.

## Politica de chamadas API das integracoes

O APScheduler corre dentro do processo Flask/Gunicorn e o deployment atual deve
continuar com exatamente 1 worker. Os jobs usam IDs estaveis e `replace_existing`
para evitar duplicados apos restart ou alteracao da configuracao.

FusionSolar:

- `integration-state-fusionsolar-hourly`: estado/monitorizacao, no maximo uma
  vez por hora. So e agendado se a integracao estiver ativa e
  `auto_sync_enabled` estiver ligado na UI.
- `integration-production-fusionsolar-daily`: producao diaria, uma vez por dia,
  sempre para o dia anterior. Controlado por `production_sync_enabled` e
  `production_sync_time`.
- `integration-diagnostics-fusionsolar-daily`: chamadas pesadas de diagnostico,
  como disponibilidade/inversores/device history/alarmes, uma vez por dia e em
  job separado da producao. Controlado por `diagnostics_sync_enabled` e
  `diagnostics_sync_time`.

Sigenergy:

- `integration-state-sigenergy-hourly`: estado atual/`energyFlow`, no maximo uma
  vez por hora. So e agendado se a integracao estiver ativa e
  `auto_sync_enabled` estiver ligado na UI.
- A app nao agenda producao nem historico Sigenergy enquanto nao houver
  endpoints/configuracao validados para isso.

Rate limit e backoff:

- O estado de cooldown fica persistido por `provider` e `api_area`
  (`state`, `production`, `diagnostics`) em `api_call_state`.
- FusionSolar `failCode=407` e HTTP 429 criam cooldown persistente de 60
  minutos por defeito. `failCode=305`/`USER_MUST_RELOGIN` invalida a sessao e
  tenta login uma vez.
- Sigenergy HTTP 401 invalida o token e autentica novamente uma vez. HTTP 429
  cria cooldown persistente.
- HTTP 5xx e falhas de rede usam backoff curto finito em background
  (`15s`, `60s`, `180s`, maximo 3 retries). Requests Flask nao fazem sleeps
  longos.
- Se um background job encontrar cooldown ativo, fica em
  `waiting_rate_limit`, grava `next_attempt_at` e e reagendado para depois do
  cooldown.
- A UI mostra o estado das APIs e jobs em espera, incluindo a proxima tentativa.

Pagina `Integracoes`:

- O separador `Resumo` mostra uma linha por provider com estado enabled,
  auto-sync, frequencia do estado, hora de producao/diagnostics FusionSolar,
  ultima tentativa, ultimo sucesso, proxima tentativa e ultimo erro.
- Os estados API aparecem como `OK`, `Em cooldown`, `Falhou ultima tentativa`,
  `Sem credenciais` ou `Disabled`.
- Os botoes de sync manual apenas criam jobs em background:
  `Estado agora`, `Producao agora` e `Diagnostics agora`. Se existir cooldown,
  o botao indica que o job fica em fila.
- `Limpar cooldown` aparece quando existe cooldown ativo e pede confirmacao no
  browser antes de alterar o estado.
- Passwords e secrets nunca sao mostrados. Quando configurados por variavel de
  ambiente, a UI mostra essa origem e bloqueia a sobrescrita no campo secreto.
- O separador `Historico` junta os ultimos sync runs e os ultimos background
  jobs, incluindo `waiting_rate_limit` e `next_attempt_at`.

## Docker / Raspberry Pi

Deployment previsto para Raspberry Pi 5 com Raspberry Pi OS 64-bit, Docker
Compose e Cloudflare Tunnel. A app nao deve ser exposta diretamente a internet.
O `docker-compose.yml` publica a porta apenas em `127.0.0.1:5000`, para ser
usada como origem local do tunnel.

Guia completo: [docs/raspberry-pi-deployment.md](docs/raspberry-pi-deployment.md).

Criar a configuracao local, se ainda nao existir:

```powershell
Copy-Item .env.example .env
```

Construir a imagem:

```powershell
docker compose build
```

Arrancar:

```powershell
docker compose up -d
```

Parar:

```powershell
docker compose down
```

Ver logs:

```powershell
docker compose logs -f
```

Atualizar depois de `git pull`:

```powershell
git pull
docker compose build
docker compose up -d
```

O container corre com:

```powershell
gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app
```

Usa exatamente 1 worker porque o APScheduler corre dentro do processo da app;
mais workers poderiam duplicar jobs agendados. As threads do Gunicorn sao
aceitaveis neste deployment porque continuam dentro do mesmo processo worker.
Enquanto o scheduler for in-process, nao aumentes `-w`, nao uses atalhos como
`WEB_CONCURRENCY`, nao corras `docker compose up --scale monitoring-board=2` e
nao arranques uma segunda instancia da app contra o mesmo `./data` sem
redesenhar o agendamento.

Os dados persistentes ficam na pasta local `./data`, montada no container como
`/data`. O compose define `DATA_DIR=/data`, por isso a app usa:

- `./data/monitoring_board.db`
- `./data/uploads/`
- `./data/backups/`
- `./data/logs/`

## Configuracao de producao

Para acesso remoto interno, usa Cloudflare Tunnel com Cloudflare Access a
frente da app. Mantem o Docker Compose a publicar apenas em
`127.0.0.1:5000:5000`; nao abras port forwarding no router e nao publiques a
porta Docker em `0.0.0.0`.

No `.env` de producao:

- Define `FLASK_SECRET_KEY` com um valor longo e aleatorio.
- Define `APP_PASSWORD_HASH` ou `APP_PASSWORD`; `APP_PASSWORD_HASH` e preferivel.
- Define as credenciais FusionSolar e Telegram apenas no `.env` ou em variaveis
  de ambiente.
- Mantem `SESSION_COOKIE_SECURE=true` quando o acesso dos utilizadores e por
  HTTPS atraves do Cloudflare Tunnel.
- Ajusta `MAX_UPLOAD_MB` se precisares de contratos PDF maiores que o limite
  por defeito.

Operacao segura:

- Usa `docker compose up -d`; nao arranques Flask com `--debug` em producao.
- Mantem exatamente um processo worker da app enquanto o APScheduler correr
  dentro do Flask/Gunicorn.
- Mantem Cloudflare Access a limitar os emails/utilizadores autorizados.
- Faz backup da pasta `./data`, porque contem SQLite, uploads, backups e logs.
- Nao envies `.env`, base de dados, PDFs, Excels ou logs para Git.
- Roda periodicamente as passwords e tokens se alguem sair da equipa.

## Backups e restore

Os dados de runtime ficam em `DATA_DIR`. No Docker Compose deste projeto, isso
corresponde a `./data` no host e `/data` dentro do container.

O script [scripts/backup.sh](scripts/backup.sh) cria backups simples em
`DATA_DIR/backups`:

- `monitoring_board_YYYYMMDD_HHMMSS.db`
- `uploads_YYYYMMDD_HHMMSS.tar.gz`, se `uploads/` existir e `INCLUDE_UPLOADS=1`

O backup da base de dados usa `sqlite3 ".backup"` para criar uma copia
consistente mesmo com WAL ativo. Por defeito, mantem os ultimos 14 backups e
apaga ficheiros com mais de 30 dias.

Backup manual no Raspberry Pi, a partir da pasta do projeto:

```bash
sudo apt install -y sqlite3 tar
chmod +x scripts/backup.sh
DATA_DIR=./data ./scripts/backup.sh
```

Para nao incluir `uploads/`:

```bash
DATA_DIR=./data INCLUDE_UPLOADS=0 ./scripts/backup.sh
```

Para alterar retencao:

```bash
DATA_DIR=./data KEEP_BACKUPS=30 DELETE_OLDER_THAN_DAYS=60 ./scripts/backup.sh
```

Cron diario, por exemplo todos os dias as 03:15:

```bash
crontab -e
```

Adicionar:

```cron
15 3 * * * cd /home/pi/Nem-sei && DATA_DIR=./data ./scripts/backup.sh >> ./data/logs/backup.log 2>&1
```

Verificar backups:

```bash
ls -lh ./data/backups
sqlite3 ./data/backups/monitoring_board_YYYYMMDD_HHMMSS.db "PRAGMA integrity_check;"
tar -tzf ./data/backups/uploads_YYYYMMDD_HHMMSS.tar.gz | head
```

Restore manual:

1. Parar a app:

```bash
docker compose down
```

2. Fazer uma copia de seguranca do estado atual antes do restore:

```bash
cp ./data/monitoring_board.db ./data/monitoring_board.db.before_restore
```

3. Restaurar a base de dados:

```bash
cp ./data/backups/monitoring_board_YYYYMMDD_HHMMSS.db ./data/monitoring_board.db
```

4. Restaurar uploads, se necessario:

```bash
rm -rf ./data/uploads
tar -C ./data -xzf ./data/backups/uploads_YYYYMMDD_HHMMSS.tar.gz
```

5. Arrancar novamente:

```bash
docker compose up -d
docker compose logs -f
```

Este processo e apenas file-based. Ainda nao ha integracao com cloud backup.

## Ficheiros locais

Estes ficheiros nao devem ir para Git:

- `.env`
- `monitoring_board.db`
- sidecars SQLite como `monitoring_board.db-wal` e `monitoring_board.db-shm`
- ficheiros Excel e PDF
- `uploads/`
- `backups/`
- `logs/`
- `data/`
- `__pycache__/`
- `SolarFusionAPI.txt`

## Diretorio de dados

Por defeito, sem `DATA_DIR`, a app mantem o comportamento local atual e usa a
pasta do projeto para `monitoring_board.db`, `uploads/`, `backups/` e `logs/`.

Em Docker/Raspberry Pi, define `DATA_DIR` para uma pasta persistente montada,
por exemplo `/data`. Nesse modo a app usa:

- `/data/monitoring_board.db`
- `/data/uploads/`
- `/data/backups/`
- `/data/logs/`

Ao arrancar, a app cria automaticamente esses diretorios se ainda nao existirem.
O caminho da base de dados usada fica registado em `monitoring_board.log`.

## Notas

- A app cria `monitoring_board.db` na pasta do projeto, ou em `DATA_DIR` quando
  esta variavel estiver definida.
- Ao arrancar, tenta importar automaticamente o primeiro ficheiro `.xlsx`
  presente na pasta.
- As credenciais FusionSolar devem ser trocadas no portal/fornecedor e depois
  atualizadas em `.env` ou no ecra de Integracoes da app.
