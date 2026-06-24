from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


REPORT_TEMPLATE_VERSION = "report-template-v1"
TEMPLATE_TYPES = {"individual", "portfolio"}
PAGE_SIZES = {"A4"}
ORIENTATIONS = {"portrait", "landscape"}
LANGUAGES = {"pt"}
SECTIONS_INDIVIDUAL = (
    "cover",
    "identification",
    "executive_summary",
    "production",
    "comparison",
    "availability",
    "self_consumption",
    "tariffs",
    "financial",
    "invoices",
    "data_quality",
    "warnings",
    "notes",
    "methodology",
    "metadata",
)
SECTIONS_PORTFOLIO = (
    "cover",
    "executive_summary",
    "kpis",
    "comparison",
    "installations_table",
    "top_performers",
    "underperformers",
    "availability",
    "financial",
    "quality",
    "warnings",
    "attachments",
    "metadata",
)


@dataclass(frozen=True)
class TemplateSection:
    key: str
    title: str
    enabled: bool = True
    display_order: int = 0
    compact: bool = False


@dataclass(frozen=True)
class BrandingConfig:
    company_name: str = "Solcoraction"
    client_name: str = ""
    logo_path: str = ""
    primary_color: str = "#0B2D52"
    secondary_color: str = "#4BA52E"
    footer: str = ""
    contacts: str = ""
    disclaimer: str = ""


@dataclass(frozen=True)
class ReportTemplate:
    id: int | None
    name: str
    report_type: str
    description: str
    portfolio_id: int | None
    client_key: str
    language: str
    page_size: str
    orientation: str
    margins_mm: tuple[int, int, int, int]
    title: str
    subtitle: str
    sections: tuple[TemplateSection, ...]
    branding: BrandingConfig
    filename_pattern: str
    show_internal_data: bool = False
    active: bool = True
    is_default: bool = False


DEFAULT_TEMPLATE_NAMES = (
    "Individual padrao",
    "Individual compacto",
    "Portfolio executivo",
    "Portfolio operacional",
    "Portfolio financeiro",
)


def default_template(name: str, *, portfolio_id: int | None = None) -> ReportTemplate:
    if name.startswith("Individual"):
        report_type = "individual"
        sections = SECTIONS_INDIVIDUAL
        compact = name.endswith("compacto")
    else:
        report_type = "portfolio"
        sections = SECTIONS_PORTFOLIO
        compact = name.endswith("executivo")
    return ReportTemplate(
        id=None,
        name=name,
        report_type=report_type,
        description=f"Template {name}",
        portfolio_id=portfolio_id,
        client_key="",
        language="pt",
        page_size="A4",
        orientation="landscape" if report_type == "portfolio" else "portrait",
        margins_mm=(14, 14, 14, 14),
        title="{portfolio} - {period}" if report_type == "portfolio" else "{asset} - {period}",
        subtitle="Relatorio de performance",
        sections=tuple(TemplateSection(key=key, title=section_title(key), enabled=True, display_order=index * 10, compact=compact) for index, key in enumerate(sections, start=1)),
        branding=BrandingConfig(),
        filename_pattern="{portfolio}_{period}" if report_type == "portfolio" else "{asset}_{period}",
        is_default=name in {"Individual padrao", "Portfolio executivo"},
    )


def section_title(key: str) -> str:
    return key.replace("_", " ").title()


def validate_template(template: ReportTemplate) -> ReportTemplate:
    if not template.name.strip():
        raise ValueError("template_name_required")
    if len(template.name) > 120:
        raise ValueError("template_name_too_long")
    if template.report_type not in TEMPLATE_TYPES:
        raise ValueError("invalid_template_type")
    if template.language not in LANGUAGES:
        raise ValueError("invalid_template_language")
    if template.page_size not in PAGE_SIZES:
        raise ValueError("invalid_page_size")
    if template.orientation not in ORIENTATIONS:
        raise ValueError("invalid_orientation")
    if any(margin < 0 or margin > 50 for margin in template.margins_mm):
        raise ValueError("invalid_margins")
    allowed_sections = set(SECTIONS_INDIVIDUAL if template.report_type == "individual" else SECTIONS_PORTFOLIO)
    keys = [section.key for section in template.sections]
    if len(keys) != len(set(keys)) or any(key not in allowed_sections for key in keys):
        raise ValueError("invalid_template_sections")
    validate_branding(template.branding)
    if len(template.filename_pattern) > 120 or any(item in template.filename_pattern for item in ("..", "/", "\\")):
        raise ValueError("invalid_filename_pattern")
    return template


def validate_template_scope(
    template: ReportTemplate,
    report_type: str,
    *,
    portfolio_id: int | None = None,
    client_key: str | None = None,
    allow_inactive: bool = False,
) -> None:
    if template.report_type != report_type:
        raise ValueError("template_type_mismatch")
    if not allow_inactive and not template.active:
        raise ValueError("template_archived")
    if template.portfolio_id is not None and template.portfolio_id != portfolio_id:
        raise ValueError("template_scope_mismatch")
    if template.client_key and template.client_key != (client_key or ""):
        raise ValueError("template_client_mismatch")


def enabled_sections_in_order(template: ReportTemplate) -> tuple[TemplateSection, ...]:
    return tuple(section for section in sorted(template.sections, key=lambda item: item.display_order) if section.enabled)


def validate_branding(branding: BrandingConfig) -> None:
    for color in (branding.primary_color, branding.secondary_color):
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color or ""):
            raise ValueError("invalid_brand_color")
    if len(branding.company_name) > 120 or len(branding.client_name) > 120:
        raise ValueError("branding_text_too_long")
    if len(branding.footer) > 300 or len(branding.contacts) > 300 or len(branding.disclaimer) > 500:
        raise ValueError("branding_text_too_long")
    if branding.logo_path and (PathLike.is_absolute(branding.logo_path) or any(item in branding.logo_path for item in ("..", ":", "\\"))):
        raise ValueError("invalid_logo_path")


class PathLike:
    @staticmethod
    def is_absolute(value: str) -> bool:
        return bool(re.match(r"^[A-Za-z]:", value or "") or str(value or "").startswith("/"))


def template_to_config(template: ReportTemplate) -> dict[str, Any]:
    return {
        "name": template.name,
        "report_type": template.report_type,
        "description": template.description,
        "portfolio_id": template.portfolio_id,
        "client_key": template.client_key,
        "language": template.language,
        "page_size": template.page_size,
        "orientation": template.orientation,
        "margins_mm": list(template.margins_mm),
        "title": template.title,
        "subtitle": template.subtitle,
        "sections": [section.__dict__ for section in template.sections],
        "branding": template.branding.__dict__,
        "filename_pattern": template.filename_pattern,
        "show_internal_data": template.show_internal_data,
        "version": REPORT_TEMPLATE_VERSION,
    }


def template_from_config(config: dict[str, Any], *, template_id: int | None = None, portfolio_id: int | None = None) -> ReportTemplate:
    branding = BrandingConfig(**{**BrandingConfig().__dict__, **dict(config.get("branding") or {})})
    sections = tuple(
        TemplateSection(
            key=str(item.get("key")),
            title=str(item.get("title") or section_title(str(item.get("key")))),
            enabled=bool(item.get("enabled", True)),
            display_order=int(item.get("display_order") or index * 10),
            compact=bool(item.get("compact", False)),
        )
        for index, item in enumerate(config.get("sections") or (), start=1)
    )
    report_type = str(config.get("report_type") or "portfolio")
    if not sections:
        sections = default_template("Portfolio executivo" if report_type == "portfolio" else "Individual padrao").sections
    margins = tuple(int(item) for item in (config.get("margins_mm") or (14, 14, 14, 14))[:4])
    return validate_template(
        ReportTemplate(
            id=template_id,
            name=str(config.get("name") or "Template"),
            report_type=report_type,
            description=str(config.get("description") or ""),
            portfolio_id=portfolio_id if portfolio_id is not None else config.get("portfolio_id"),
            client_key=str(config.get("client_key") or ""),
            language=str(config.get("language") or "pt"),
            page_size=str(config.get("page_size") or "A4"),
            orientation=str(config.get("orientation") or "portrait"),
            margins_mm=margins if len(margins) == 4 else (14, 14, 14, 14),
            title=str(config.get("title") or ""),
            subtitle=str(config.get("subtitle") or ""),
            sections=tuple(sorted(sections, key=lambda section: section.display_order)),
            branding=branding,
            filename_pattern=str(config.get("filename_pattern") or "{portfolio}_{period}"),
            show_internal_data=bool(config.get("show_internal_data", False)),
        )
    )
