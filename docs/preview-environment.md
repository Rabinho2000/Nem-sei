# Ambiente permanente de preview

O preview corre isolado da produção:

- produção: `/opt/server/apps/Nem-sei`, `http://media:5000`;
- preview: `/opt/server/apps/Nem-sei-preview`, `http://media:5002`;
- projeto Compose: `nem-sei-preview`;
- runtime do preview: `/opt/server/apps/Nem-sei-preview/runtime`;
- base do preview: `runtime/monitoring_board.db`.

O Compose usa uma rede Docker dedicada com `internal: true`. A aplicação mantém
também proteção em código: não inicia APScheduler e bloqueia FusionSolar,
Sigenergy, Telegram, onboarding e OpenRouteService quando `APP_ENV=preview` ou
`PREVIEW_BANNER=true`.

O preview não deve ser publicado na Internet. A firewall do host deve permitir
a porta TCP 5002 apenas a partir da LAN e/ou Tailscale.

## Instalação

Depois de publicar a branch, no servidor:

```bash
cd /opt/server/apps/Nem-sei
git fetch origin codex/server-dev-2026-07-29
git show origin/codex/server-dev-2026-07-29:scripts/install_preview_environment.sh \
  > /tmp/install_preview_environment.sh
chmod 700 /tmp/install_preview_environment.sh
sudo /tmp/install_preview_environment.sh
```

O instalador pede utilizador e password do preview, gera `FLASK_SECRET_KEY`,
cria `.env.preview` com modo `600`, instala o comando administrativo, copia e
sanitiza a SQLite e, opcionalmente, copia uploads.

Não reutiliza `.env`, SQLite, uploads, containers ou runtime da produção.

## Operação

```bash
sudo deploy-nem-sei-preview update
sudo deploy-nem-sei-preview refresh-data
sudo deploy-nem-sei-preview refresh-data yes
sudo deploy-nem-sei-preview start
sudo deploy-nem-sei-preview stop
sudo deploy-nem-sei-preview status
```

`refresh-data` para apenas o preview. Cria uma cópia consistente com
`sqlite3 .backup`, sanitiza credenciais e desativa jobs na cópia, executa o
schema idempotente e volta a iniciar o preview. O argumento `yes` sincroniza
também os uploads com `rsync`.

Nenhum comando para, reconstrói ou escreve na produção. A única leitura de
dados de produção acontece durante a cópia explícita.
