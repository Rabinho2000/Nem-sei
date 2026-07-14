from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = PROJECT_ROOT / "templates" / "base.html"
STYLESHEET = PROJECT_ROOT / "static" / "styles.css"


class MobileNavigationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.mobile_toggles: list[dict[str, str | None]] = []
        self.site_navs: list[dict[str, str | None]] = []
        self.site_nav_link_count = 0
        self._inside_site_nav = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "button" and "mobile-nav-toggle" in classes:
            self.mobile_toggles.append(attributes)
        if tag == "nav" and attributes.get("id") == "site-nav":
            self.site_navs.append(attributes)
            self._inside_site_nav = True
        elif tag == "a" and self._inside_site_nav:
            self.site_nav_link_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav" and self._inside_site_nav:
            self._inside_site_nav = False


def _read_mobile_navigation() -> tuple[str, MobileNavigationParser]:
    source = BASE_TEMPLATE.read_text(encoding="utf-8")
    parser = MobileNavigationParser()
    parser.feed(source)
    return source, parser


def _media_blocks(css: str, max_width: int) -> list[str]:
    start_pattern = re.compile(
        rf"@media\s*\(\s*max-width\s*:\s*{max_width}px\s*\)\s*\{{",
        re.IGNORECASE,
    )
    blocks: list[str] = []
    for match in start_pattern.finditer(css):
        depth = 1
        position = match.end()
        while position < len(css) and depth:
            if css[position] == "{":
                depth += 1
            elif css[position] == "}":
                depth -= 1
            position += 1
        assert depth == 0, "Unclosed responsive media block"
        blocks.append(css[match.end() : position - 1])
    return blocks


def test_mobile_navigation_contract() -> None:
    source, navigation = _read_mobile_navigation()

    assert re.search(
        r'<meta\s+name=["\']viewport["\']\s+'
        r'content=["\']width=device-width,\s*initial-scale=1["\']',
        source,
        re.IGNORECASE,
    )
    assert len(navigation.mobile_toggles) == 1
    assert navigation.mobile_toggles[0]["type"] == "button"
    assert navigation.mobile_toggles[0]["aria-controls"] == "site-nav"
    assert navigation.mobile_toggles[0]["aria-expanded"] == "false"
    assert navigation.mobile_toggles[0].get("aria-label")
    assert len(navigation.site_navs) == 1
    assert "nav" in (navigation.site_navs[0].get("class") or "").split()
    assert 'nav.classList.toggle("is-open")' in source


def test_mobile_navigation_accessibility_behavior() -> None:
    source, navigation = _read_mobile_navigation()

    assert navigation.site_nav_link_count > 0
    assert 'toggle.setAttribute("aria-expanded", String(isOpen))' in source
    assert 'toggle.setAttribute("aria-expanded", "false")' in source
    assert 'event.key === "Escape"' in source
    assert 'event.target.closest("a")' in source
    assert 'window.matchMedia("(max-width: 700px)")' in source
    assert source.count('id="site-nav"') == 1


def test_mobile_css_contract() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    blocks = _media_blocks(css, 700)

    assert blocks, "The mobile breakpoint at 700px is required"
    mobile_css = "\n".join(blocks)
    for selector in (
        ".mobile-nav-toggle",
        ".sidebar .nav",
        ".sidebar .nav.is-open",
        ".content",
        ".card",
        ".cards",
        ".filters",
        ".compact-ticket-form",
        ".hero-actions",
        ".row-actions",
        ".table-wrap",
        ".table-wrapper",
        ".tab-nav",
        ".tab-bar",
    ):
        assert selector in mobile_css

    assert re.search(r"\.sidebar\s+\.nav\s*\{[^}]*display\s*:\s*none", mobile_css, re.DOTALL)
    assert re.search(r"\.sidebar\s+\.nav\.is-open\s*\{[^}]*display\s*:\s*grid", mobile_css, re.DOTALL)
    assert mobile_css.count("overflow-x: auto") >= 2

    base_toggle = re.search(r"\.mobile-nav-toggle\s*\{([^}]*)\}", css, re.DOTALL)
    assert base_toggle
    assert re.search(r"display\s*:\s*none", base_toggle.group(1))
