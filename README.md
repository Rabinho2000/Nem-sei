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
- Os logs ficam em `logs/monitoring_board.log`, fora do Git.

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

## Ficheiros locais

Estes ficheiros nao devem ir para Git:

- `.env`
- `monitoring_board.db`
- ficheiros Excel e PDF
- `uploads/`
- `backups/`
- `logs/`
- `__pycache__/`
- `SolarFusionAPI.txt`

## Notas

- A app cria `monitoring_board.db` na pasta do projeto.
- Ao arrancar, tenta importar automaticamente o primeiro ficheiro `.xlsx`
  presente na pasta.
- As credenciais FusionSolar devem ser trocadas no portal/fornecedor e depois
  atualizadas em `.env` ou no ecra de Integracoes da app.
