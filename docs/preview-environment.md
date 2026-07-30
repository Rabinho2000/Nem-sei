# Ambiente permanente de preview

O preview corre isolado da produção:

- produção: `/opt/server/apps/Nem-sei`, `http://media:5000`;
- preview: `/opt/server/apps/Nem-sei-preview`, `http://media:5002`;
- projeto Compose: `nem-sei-preview`;
- runtime do preview: `/opt/server/apps/Nem-sei-preview/runtime`;
- base do preview: `runtime/monitoring_board.db`.

O Compose usa três redes e três serviços:

- `monitoring-board` liga-se exclusivamente a `preview-internal`, que mantém
  `internal: true`, não publica portas e não tem uma interface com saída para a
  Internet;
- `preview-gateway` é um nginx mínimo ligado a `preview-internal` e
  `preview-edge`; é o único serviço que publica uma porta
  (`0.0.0.0:5002->8080`) e só encaminha pedidos para
  `http://monitoring-board:5000`;
- `sigenergy-preview-sync` é um worker one-shot, ativado apenas pelo perfil
  `sigenergy-preview`, ligado exclusivamente a `preview-egress`, sem portas,
  scheduler ou ações remotas que não sejam as leituras Sigenergy permitidas.
  Monta apenas `runtime` e partilha a imagem da aplicação.

O gateway não recebe `.env.preview`, dados ou volumes. A aplicação mantém
também proteção em código: não inicia APScheduler e bloqueia FusionSolar,
Sigenergy, Telegram, onboarding e OpenRouteService quando `APP_ENV=preview` ou
`PREVIEW_BANNER=true`. A única exceção é o client do worker, que recebe uma
política explícita com a allowlist exata de método, endpoint, Base URL e System
ID. Essa política não é usada pelo `monitoring-board`.

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

Também cria, vazio e com proprietário `root:root` e modo `600`:

```text
/opt/server/apps/Nem-sei-preview/.env.sigenergy-preview
```

O administrador deve preencher apenas `SIGENERGY_APP_KEY` e
`SIGENERGY_APP_SECRET`. Base URL, região, unidade e System ID já ficam fixos à
allowlist da Expertcom. O ficheiro é excluído do Git e do contexto Docker.

Não reutiliza `.env`, SQLite, uploads, containers ou runtime da produção.

## Operação

```bash
sudo deploy-nem-sei-preview update
sudo deploy-nem-sei-preview refresh-data
sudo deploy-nem-sei-preview refresh-data yes
sudo deploy-nem-sei-preview start
sudo deploy-nem-sei-preview stop
sudo deploy-nem-sei-preview status
sudo deploy-nem-sei-preview sigenergy-discover
sudo deploy-nem-sei-preview sigenergy-state
sudo deploy-nem-sei-preview sigenergy-backfill AAAA-MM-DD AAAA-MM-DD
sudo deploy-nem-sei-preview sigenergy-check
```

`refresh-data` para apenas o preview. Cria uma cópia consistente com
`sqlite3 .backup`, sanitiza credenciais e desativa jobs na cópia, executa o
schema idempotente e volta a iniciar o preview. O argumento `yes` sincroniza
também os uploads com `rsync`.

Nenhum comando para, reconstrói ou escreve na produção. A única leitura de
dados de produção acontece durante a cópia explícita.

Os quatro comandos Sigenergy criam um contentor temporário e removem-no no fim.
`sigenergy-discover` atualiza apenas `provider_system_inventory`;
`sigenergy-state` grava apenas o snapshot local; `sigenergy-backfill` aceita
somente dias terminados e materializa factos diários/mensais;
`sigenergy-check` valida autenticação, descoberta e `energyFlow`. Nenhum cria
assets, envia alertas, inicia o scheduler, faz onboarding ou controlo remoto.

## Atualizar a instalação existente

Estes comandos preservam o `.env.preview` existente, incluindo
`APP_USERNAME`, `APP_PASSWORD` e `FLASK_SECRET_KEY`. Não é necessário voltar a
executar o instalador interativo.

```bash
cd /opt/server/apps/Nem-sei-preview
sudo -u stavares git fetch origin codex/server-dev-2026-07-29
sudo -u stavares git merge --ff-only origin/codex/server-dev-2026-07-29
sudo install -m 0755 -o root -g root \
  /opt/server/apps/Nem-sei-preview/scripts/deploy_preview_environment.sh \
  /usr/local/bin/deploy-nem-sei-preview
sudo /usr/local/bin/deploy-nem-sei-preview update
sudo /usr/local/bin/deploy-nem-sei-preview status
```

Antes do `git merge`, `git status --short` deve estar limpo quanto a ficheiros
tracked. O comando `update` verifica primeiro a produção, valida que runtime,
`.env.preview` e bases SQLite são excluídos do contexto, constrói os dois
serviços, para apenas a aplicação de preview para executar o schema e confirma
no fim ambos os healthchecks, a publicação da porta 5002 e a produção.
