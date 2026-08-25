"""Fase 1 do interface: a shell da marca renderiza em todas as paginas.

Guarda o que a Fase 1 introduziu e que nenhum outro teste cobre: o logotipo
Solcor servido pelo Flask (existia so na pasta estatica legada do V1, nao na
da aplicacao), o nome do produto, e a navegacao em telemovel -- que ate aqui
simplesmente nao existia: abaixo de 680 px a barra lateral desaparecia e nada
a substituia, deixando o utilizador sem forma de sair da primeira pagina.
"""
from __future__ import annotations

import re

from nemsei.app import create_app
from tests_v2.test_migrations import upgrade

SHELL_MARKERS = (
    ("static/solcor-logo.png", "logotipo"),
    ('class="mobile-tabs"', "barra de separacores movel"),
    ("Solcor O&amp;M", "nome do produto"),
    ('class="brand-logo"', "lockup da marca"),
    ("more-sheet", "painel Mais"),
)

PAGES = (
    "/",
    "/assets",
    "/organizations",
    "/provider-connections",
    "/mappings",
    "/source-policies",
    "/reconciliation",
    "/portfolios",
    "/reports",
    "/diagnostics",
    "/diagnostics/incidents",
    # Ecras acrescentados depois: cobertos aqui para que a shell da marca e a
    # navegacao movel nao possam regredir num deles sem ninguem reparar.
    "/automations",
    "/automations/digest-preview",
    "/system",
)


def _token_names(text: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", text))


def _dark_regions(css: str) -> list[tuple[int, int]]:
    """Character ranges of every block whose selector targets dark mode."""
    regions: list[tuple[int, int]] = []
    for match in re.finditer(r"(@media[^{]*prefers-color-scheme:\s*dark[^{]*|:root\s*\[data-theme=\"dark\"\])\s*\{", css):
        depth, index = 1, match.end()
        while index < len(css) and depth:
            depth += 1 if css[index] == "{" else -1 if css[index] == "}" else 0
            index += 1
        regions.append((match.end(), index))
    return regions


def tokens_defined_only_in_dark(css: str) -> set[str]:
    """Custom properties a dark block defines that no light-side rule defines.

    A token whose only definition sits behind a theme block does not apply in
    the default "system" state, and the page then paints one theme's text on
    the other theme's ground.
    """
    regions = _dark_regions(css)
    dark: set[str] = set()
    for start, end in regions:
        dark |= _token_names(css[start:end])
    light_source = css
    for start, end in sorted(regions, reverse=True):
        light_source = light_source[:start] + light_source[end:]
    return dark - _token_names(light_source)


def csrf_token(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_brand_shell_renders_on_every_authenticated_page(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    client = create_app(settings).test_client()
    client.post(
        "/login",
        data={
            "username": "admin",
            "password": "correct-password",
            "csrf_token": csrf_token(client.get("/login")),
        },
    )

    missing = []
    for path in PAGES:
        response = client.get(path)
        if response.status_code != 200:
            missing.append((path, f"HTTP {response.status_code}"))
            continue
        for marker, label in SHELL_MARKERS:
            if marker not in response.text:
                missing.append((path, f"falta {label}"))
    assert not missing, missing


def test_logo_and_stylesheet_are_served_by_the_application(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    client = create_app(settings).test_client()

    logo = client.get("/static/solcor-logo.png")
    assert logo.status_code == 200
    assert logo.data[:8] == b"\x89PNG\r\n\x1a\n"

    stylesheet = client.get("/static/styles.css")
    assert stylesheet.status_code == 200
    body = stylesheet.text
    # O verde da marca so serve de preenchimento (2,16:1 sobre branco); o
    # texto usa --brand, que e legivel, e sobre o verde usa-se --on-brand.
    for token in ("--brand-vivid: #75be43", "--on-brand", "--shell: #333e48"):
        assert token in body, token
    # Modo escuro com degraus proprios, e a barra de separadores movel.
    assert "prefers-color-scheme: dark" in body
    assert ".mobile-tabs" in body
    # A regra que interessa, verificada em vez de aproximada por uma contagem:
    # nenhum token pode existir apenas dentro de um bloco de tema. Um token so
    # definido no escuro nao se aplica no estado "sistema" sem preferencia
    # declarada, e a pagina renderiza o texto de um tema sobre o fundo do outro.
    assert not tokens_defined_only_in_dark(body)


def test_login_page_carries_the_brand_and_a_stylesheet(settings) -> None:
    # Antes da Fase 1 esta pagina era HTML cru, sem folha de estilos nenhuma.
    response = create_app(settings).test_client().get("/login")
    assert response.status_code == 200
    for marker in ('class="brand-logo"', "Solcor O&amp;M", "styles.css", 'name="csrf_token" value="'):
        assert marker in response.text, marker
