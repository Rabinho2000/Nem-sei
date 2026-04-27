# Monitoring Board Local

Aplicação local em Python para:

- importar o `Project Overview` do Excel para SQLite
- registar monitorização diária por colar tabela
- manter histórico por data e estado atual por asset
- gerir corretivas com tickets, urgência e visitas

## Arranque

1. Instalar dependências:

```powershell
pip install flask openpyxl
```

2. Arrancar a app:

```powershell
python app.py
```

3. Abrir no browser:

`http://127.0.0.1:5000`

## Notas

- A app cria a base `monitoring_board.db` na pasta do projeto.
- Ao arrancar, tenta importar automaticamente o ficheiro `.xlsx` presente na pasta.
- Se uma instalação diária vier com nome diferente, a linha fica pendente em `Monitorização` até ser associada a um asset. A partir daí fica guardado um alias para futuras importações.
