# Conversor falha com PowerPoint aberto

Status: resolvido
Data: 2026-07-20

## Sintomas

- Esperado: converter PPTX para PDF mesmo quando o utilizador tem o PowerPoint aberto.
- Observado: 7/7 conversões falharam antes de abrir os ficheiros.
- Erro: `O PowerPoint está aberto. Feche-o e tente novamente.`
- Script real: `C:\Users\Sérgio\Documents\Projetos\scripts\converter_documentos_pdf.py`.

## Hipóteses e evidência

1. Falha de abertura ou exportação do PPTX: rejeitada. O erro era gerado antes de `Presentations.Open`.
2. Bloqueio preventivo por processo aberto: confirmada. O auxiliar PowerShell fazia `Get-Process POWERPNT` e lançava imediatamente a mensagem de erro.
3. Uma nova chamada COM poderia fechar a sessão do utilizador: confirmada como risco. Com o PowerPoint aberto no PID 30848, `New-Object -ComObject PowerPoint.Application` devolveu a aplicação com o mesmo `HWND`, o mesmo PID e uma apresentação já aberta. Portanto, chamar sempre `Quit()` fecharia a sessão existente.

## Causa raiz

O conversor tinha uma proteção demasiado ampla: recusava qualquer conversão quando `POWERPNT.exe` existia. Esta proteção evitava o `Quit()` perigoso, mas também impedia o uso normal. A automação não distinguia entre uma aplicação PowerPoint emprestada da sessão do utilizador e uma aplicação criada pelo conversor.

## Correção

- Mantida a rejeição conservadora existente para Word e Excel.
- Permitida a ligação ao PowerPoint já aberto.
- Identificada a posse pelo PID da aplicação correspondente ao `HWND` COM e pela lista de PIDs anterior à criação.
- `Quit()` e a terminação por timeout só podem atingir um PID criado pelo conversor.
- A apresentação aberta pelo conversor continua a ser fechada individualmente.
- `DisplayAlerts` e `AutomationSecurity` são restaurados quando a aplicação pertence ao utilizador.
- O fallback LibreOffice e a lógica geral de conversão foram preservados.

## Validação

- Conversão real de `Sticker quadros e inversores_ES.pptx`: PDF com 229238 bytes e cabeçalho `%PDF-`.
- PowerPoint antes/depois: PID inalterado, título da janela inalterado e contagem de apresentações `1 -> 1`.
- Definições globais antes/depois: `ppAlertsAll -> ppAlertsAll` e `msoAutomationSecurityByUI -> msoAutomationSecurityByUI`.
- Testes: `4 passed`.
- Compilação Python: concluída sem erros.

## ROOT CAUSE FOUND

A deteção de processo aberto era usada como substituto de controlo de posse da aplicação. O controlo correto é acompanhar o PID efetivamente associado ao objeto COM e fechar apenas quando esse PID não existia antes da conversão.
