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
   FusionSolar.

5. Arrancar a app:

```powershell
python app.py
```

6. Abrir no browser:

`http://127.0.0.1:5000`

## Ficheiros locais

Estes ficheiros nao devem ir para Git:

- `.env`
- `monitoring_board.db`
- ficheiros Excel e PDF
- `uploads/`
- `backups/`
- `__pycache__/`
- `SolarFusionAPI.txt`

## Notas

- A app cria `monitoring_board.db` na pasta do projeto.
- Ao arrancar, tenta importar automaticamente o primeiro ficheiro `.xlsx`
  presente na pasta.
- As credenciais FusionSolar devem ser trocadas no portal/fornecedor e depois
  atualizadas em `.env` ou no ecra de Integracoes da app.
