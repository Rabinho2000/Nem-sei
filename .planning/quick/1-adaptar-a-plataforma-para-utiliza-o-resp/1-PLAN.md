---
quick: 1
mode: quick-full
title: "Adaptar a plataforma para utilização responsiva em browsers móveis"
status: planned
autonomous: true
files_modified:
  - templates/base.html
  - static/styles.css
  - tests/test_mobile_ui.py
must_haves:
  truths:
    - "Em ecrãs móveis, a navegação principal começa recolhida, pode ser aberta e fechada por um controlo visível e mantém atributos ARIA coerentes com o seu estado."
    - "Até 700 px, conteúdo, cartões, formulários, ações, tabelas e separadores permanecem legíveis e utilizáveis sem alterar o layout de desktop."
    - "Testes focados detetam a remoção do contrato HTML da navegação móvel ou das regras responsivas essenciais."
  artifacts:
    - path: "templates/base.html"
      provides: "Viewport móvel e navegação principal colapsável e acessível"
      contains: "mobile-nav-toggle"
    - path: "static/styles.css"
      provides: "Breakpoint móvel isolado e regras responsivas para os componentes existentes"
      contains: "@media (max-width: 700px)"
    - path: "tests/test_mobile_ui.py"
      provides: "Testes de contrato do HTML base e da folha de estilos móvel"
      contains: "test_mobile_navigation_contract"
  key_links:
    - from: "templates/base.html"
      to: "static/styles.css"
      via: "classes/IDs partilhados pela navegação e pelo respetivo estado aberto"
      pattern: "mobile-nav-toggle|site-nav|is-open"
    - from: "templates/base.html"
      to: "navegação principal"
      via: "aria-controls e aria-expanded atualizados pelo comportamento de alternância"
      pattern: "aria-controls|aria-expanded"
    - from: "tests/test_mobile_ui.py"
      to: "templates/base.html, static/styles.css"
      via: "asserções de contrato sobre estrutura, acessibilidade e breakpoint"
      pattern: "mobile-nav-toggle|max-width:\\s*700px"
---

# Quick Task 1: Adaptar a plataforma para utilização responsiva em browsers móveis

## Objective

Tornar a interface Flask existente confortável em browsers móveis, introduzindo uma navegação principal colapsável e regras responsivas focadas, sem modificar o comportamento visual do desktop nem refatorar templates de páginas sem necessidade.

## Context

- Preservar os padrões atuais de Flask/Jinja e os nomes/classes já usados sempre que possível.
- Manter as alterações pequenas e limitadas a `templates/base.html`, `static/styles.css` e um ficheiro de testes focado.
- Todas as adaptações de layout devem ficar limitadas ao breakpoint `max-width: 700px`; estilos base existentes continuam a reger o desktop.
- Não adicionar frameworks JavaScript/CSS nem dependências novas.

## Tasks

### Task 1: Criar navegação móvel colapsável e acessível

**files:**
- `templates/base.html`

**action:**
- Confirmar que o `<head>` inclui `meta name="viewport"` com `width=device-width, initial-scale=1`; adicionar apenas se estiver ausente.
- Identificar a navegação principal existente e preservar os seus links, condições Jinja, destinos e ordem.
- Adicionar um botão `.mobile-nav-toggle` associado à navegação por `aria-controls="site-nav"`, com `aria-expanded="false"`, um nome acessível claro e um ícone/indicador que não dependa apenas de imagem.
- Dar à navegação o identificador estável `site-nav` e usar uma única classe de estado, `is-open`, para o modo expandido.
- Implementar, no padrão já usado pelo template, comportamento JavaScript mínimo e sem dependências que alterne `is-open` e sincronize `aria-expanded`; fechar ao premir Escape e ao escolher um link em vista móvel, sem interferir com a navegação de desktop.
- Evitar duplicar menus ou criar versões divergentes da mesma navegação.

**verify:**
- Renderizar/abrir uma página que estenda `base.html` e confirmar que todos os links e condições existentes continuam presentes.
- Em viewport até 700 px, confirmar por teclado e toque que o botão abre e fecha o menu, que `aria-expanded` acompanha o estado e que Escape fecha o menu.
- Em viewport superior a 700 px, confirmar que a navegação continua visível e utilizável e que o botão móvel não altera o fluxo do cabeçalho.

**done:**
- O template contém viewport móvel, um único menu principal, um controlo acessível ligado ao menu e alternância funcional com estado ARIA sincronizado, preservando links e comportamento desktop existentes.

### Task 2: Adicionar estilos responsivos focados até 700 px

**files:**
- `static/styles.css`

**action:**
- Acrescentar estilos do controlo/estado da navegação de forma compatível com o cabeçalho atual: o botão fica oculto no desktop e visível até 700 px; no móvel, o menu fica recolhido por defeito e apenas `.is-open` o expande.
- Concentrar as adaptações móveis dentro de `@media (max-width: 700px)` para preservar as regras existentes acima do breakpoint.
- Ajustar contentores e conteúdo principal para largura fluida, margens/padding compactos e ausência de overflow horizontal causado pelo layout.
- Fazer grelhas e cartões passarem para uma coluna, com largura máxima disponível e espaçamento consistente.
- Empilhar grupos de formulário, labels/inputs e linhas multi-coluna; manter campos e controlos com `width: 100%`/`max-width: 100%` quando adequado e alvos de toque utilizáveis.
- Permitir que grupos de ações/botões quebrem linha ou empilhem, sem sobreposição nem corte de texto.
- Tornar tabelas largas deslocáveis horizontalmente dentro da própria tabela/contentor, preservando cabeçalhos, células e leitura dos dados em vez de ocultar colunas.
- Tornar barras de tabs/separadores deslocáveis horizontalmente ou com quebra controlada, mantendo cada separador alcançável e legível.
- Reutilizar os seletores reais já existentes; agrupar aliases apenas quando componentes equivalentes usam nomes distintos, evitando regras globais que afetem elementos fora do breakpoint.

**verify:**
- Executar os testes focados: `pytest -q tests/test_mobile_ui.py`.
- Inspecionar pelo menos 320 px, 375 px e 700 px, confirmando navegação, cartões, formulários, ações, tabelas e tabs sem conteúdo inacessível ou sobreposições.
- Inspecionar uma largura de desktop acima de 700 px e confirmar que disposição, espaçamentos, menu e tabelas permanecem regidos pelos estilos anteriores.

**done:**
- Todos os grupos de componentes pedidos têm comportamento móvel explícito no breakpoint de 700 px, tabelas/tabs continuam utilizáveis e nenhuma regra responsiva altera o layout de desktop.

### Task 3: Fixar o contrato responsivo com testes focados

**files:**
- `tests/test_mobile_ui.py`
- `templates/base.html`
- `static/styles.css`

**action:**
- Criar testes pequenos, seguindo as convenções pytest já existentes, que leiam/renderizem `base.html` conforme o padrão de fixtures do projeto e validem o contrato sem depender de serviços externos, base de dados real ou browser completo.
- Adicionar `test_mobile_navigation_contract` para verificar viewport, existência única do botão `.mobile-nav-toggle`, `aria-controls="site-nav"`, estado inicial `aria-expanded="false"`, alvo `#site-nav` e classe de estado `is-open` usada pela alternância.
- Adicionar um teste de acessibilidade/comportamento estrutural que confirme a sincronização prevista de `aria-expanded` e que os links do menu permanecem dentro da navegação principal, sem menu duplicado.
- Adicionar teste de contrato CSS que confirme a existência de `@media (max-width: 700px)` e regras dentro desse bloco para navegação, conteúdo/cartões, formulários, ações, tabelas e tabs; verificar também que o botão móvel permanece oculto no estilo de desktop/base.
- Fazer as asserções incidirem em contratos estáveis (classes, IDs, atributos e breakpoint), não em texto visual, ordem exata de propriedades ou formatação da folha de estilos.

**verify:**
- Executar `pytest -q tests/test_mobile_ui.py` e confirmar que todos os testes passam.
- Executar a suite existente relevante (ou `pytest -q` se tiver duração razoável) para confirmar que o novo contrato não quebra renderização ou testes anteriores.
- Alterar temporariamente, durante validação local, um atributo/selector essencial e confirmar que o teste correspondente falha; repor a alteração antes de concluir.

**done:**
- Os testes passam, cobrem os contratos essenciais da navegação e do breakpoint móvel, falham perante regressões deliberadas desses contratos e não exigem infraestrutura externa.

## Success Criteria

- A navegação é colapsável, operável por toque/teclado e acessível em browsers móveis.
- Conteúdo, cartões, formulários, ações, tabelas e tabs são utilizáveis entre 320 px e 700 px.
- O layout acima de 700 px preserva o comportamento desktop existente.
- Os testes focados protegem a ligação entre HTML, comportamento da navegação e CSS responsivo.
- Nenhuma dependência nova, refatoração alheia ou alteração de dados/backend é introduzida.
