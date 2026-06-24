from __future__ import annotations

import hashlib
import html
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from monitoring_board.reporting.portfolio import METRIC_CATALOG, PortfolioReportResult
from monitoring_board.reporting.templates import ReportTemplate
from monitoring_board.services.portfolio_reporting import export_portfolio_result_workbook, format_cell


MAX_BATCH_ASSETS = 25
MAX_BATCH_PERIODS = 12
MAX_ZIP_FILES = 80
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}


@dataclass(frozen=True)
class RenderedFile:
    filename: str
    content: bytes
    mimetype: str
    fmt: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def safe_filename(value: str, *, extension: str = "") -> str:
    if any(item in (value or "") for item in ("..", "/", "\\")) or ":" in (value or ""):
        raise ValueError("unsafe_filename")
    normalized = unicodedata.normalize("NFKD", value or "relatorio")
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_text).strip("._-") or "relatorio"
    if cleaned.upper() in RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    cleaned = cleaned[:100]
    suffix = extension if extension.startswith(".") or not extension else f".{extension}"
    if suffix and not cleaned.lower().endswith(suffix.lower()):
        cleaned += suffix
    if any(item in cleaned for item in ("..", "/", "\\")) or ":" in cleaned:
        raise ValueError("unsafe_filename")
    return cleaned


def render_portfolio_html(result: PortfolioReportResult, template: ReportTemplate) -> str:
    title = html.escape(expand_pattern(template.title, result=result) or f"{result.portfolio_name} - {result.period.label}")
    parts = [
        "<article class='report-preview'>",
        f"<h1>{title}</h1>",
        f"<p>{html.escape(template.subtitle)}</p>",
        f"<p>{html.escape(template.branding.company_name)} · {html.escape(result.period.label)}</p>",
    ]
    enabled = {section.key for section in template.sections if section.enabled}
    if "kpis" in enabled or "executive_summary" in enabled:
        parts.append("<section><h2>Resumo</h2><dl>")
        for key, value in result.summary.values.items():
            label = METRIC_CATALOG[key].label if key in METRIC_CATALOG else key
            parts.append(f"<dt>{html.escape(label)}</dt><dd>{html.escape(str(format_cell(value)))}</dd>")
        parts.append("</dl></section>")
    if result.comparison and "comparison" in enabled:
        parts.append("<section><h2>Comparacao</h2><table><thead><tr><th>Metrica</th><th>Atual</th><th>Anterior</th><th>Diferenca</th></tr></thead><tbody>")
        for key, item in result.comparison.values.items():
            label = METRIC_CATALOG[key].label if key in METRIC_CATALOG else key
            parts.append(f"<tr><td>{html.escape(label)}</td><td>{html.escape(str(item.get('current')))}</td><td>{html.escape(str(item.get('previous')))}</td><td>{html.escape(str(item.get('delta')))}</td></tr>")
        parts.append("</tbody></table></section>")
    if "installations_table" in enabled:
        parts.append("<section><h2>Instalacoes</h2><table><thead><tr>")
        for column in result.columns:
            parts.append(f"<th>{html.escape(column.label)}</th>")
        parts.append("</tr></thead><tbody>")
        for row in result.rows:
            parts.append("<tr>")
            for column in result.columns:
                parts.append(f"<td>{html.escape(str(format_cell(row.values.get(column.metric_key)) or '-'))}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table></section>")
    if "quality" in enabled or "warnings" in enabled:
        parts.append(f"<section><h2>Qualidade</h2><p>Cobertura global: {result.coverage.global_pct}%</p>")
        if result.warnings:
            parts.append(f"<p>Warnings: {html.escape(', '.join(result.warnings))}</p>")
        parts.append("</section>")
    parts.append("</article>")
    return "".join(parts)


def render_portfolio_pdf(result: PortfolioReportResult, template: ReportTemplate) -> RenderedFile:
    buffer = io.BytesIO()
    pagesize = landscape(A4) if template.orientation == "landscape" else A4
    doc = SimpleDocTemplate(buffer, pagesize=pagesize, leftMargin=32, rightMargin=32, topMargin=32, bottomMargin=28)
    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph(expand_pattern(template.title, result=result) or f"{result.portfolio_name} - {result.period.label}", styles["Title"]),
        Paragraph(template.subtitle or template.branding.company_name, styles["Normal"]),
        Spacer(1, 12),
    ]
    enabled = {section.key for section in template.sections if section.enabled}
    if "kpis" in enabled or "executive_summary" in enabled:
        story.append(Paragraph("Resumo", styles["Heading2"]))
        story.append(Table([["Metrica", "Valor"], *[[METRIC_CATALOG[key].label if key in METRIC_CATALOG else key, str(format_cell(value))] for key, value in result.summary.values.items()]], hAlign="LEFT"))
        story.append(Spacer(1, 10))
    if result.comparison and "comparison" in enabled:
        story.append(Paragraph("Comparacao", styles["Heading2"]))
        story.append(Table([["Metrica", "Atual", "Anterior", "Diferenca"], *[[METRIC_CATALOG[key].label if key in METRIC_CATALOG else key, str(item.get("current")), str(item.get("previous")), str(item.get("delta"))] for key, item in result.comparison.values.items()]], hAlign="LEFT"))
        story.append(Spacer(1, 10))
    if "installations_table" in enabled:
        table_rows = [[column.label for column in result.columns[:10]]]
        for row in result.rows:
            table_rows.append([str(format_cell(row.values.get(column.metric_key)) or "-") for column in result.columns[:10]])
        table = Table(table_rows, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(template.branding.primary_color)), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("FONTSIZE", (0, 0), (-1, -1), 7)]))
        story.append(Paragraph("Instalacoes", styles["Heading2"]))
        story.append(table)
    if "quality" in enabled or "warnings" in enabled:
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"Cobertura global: {result.coverage.global_pct}%", styles["Normal"]))
        if result.warnings:
            story.append(Paragraph("Warnings: " + ", ".join(result.warnings), styles["Normal"]))
    doc.build(story)
    return RenderedFile(filename=safe_filename(expand_pattern(template.filename_pattern, result=result), extension="pdf"), content=buffer.getvalue(), mimetype="application/pdf", fmt="pdf")


def render_individual_pdf(report: dict[str, Any], template: ReportTemplate) -> RenderedFile:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=30)
    styles = getSampleStyleSheet()
    asset = report.get("asset") or {}
    title = (template.title or "{asset} - {period}").format(asset=asset.get("project_name") or asset.get("name") or "Instalacao", period=report.get("period_label") or report.get("report_month") or "")
    rows = [["Metrica", "Valor"]]
    for label, key in (("Producao", "production_kwh"), ("Autoconsumo", "self_use_kwh"), ("Excedente", "export_kwh"), ("Beneficio liquido", "net_benefit_eur")):
        rows.append([label, str(report.get(key, "-"))])
    doc.build([Paragraph(title, styles["Title"]), Paragraph(template.subtitle, styles["Normal"]), Spacer(1, 12), Table(rows, hAlign="LEFT")])
    return RenderedFile(filename=safe_filename(f"{asset.get('project_name') or 'Instalacao'}_{report.get('period_label') or 'periodo'}", extension="pdf"), content=buffer.getvalue(), mimetype="application/pdf", fmt="pdf")


def render_portfolio_excel(result: PortfolioReportResult, template: ReportTemplate) -> RenderedFile:
    workbook = export_portfolio_result_workbook(result)
    workbook["Resumo"].insert_rows(1)
    workbook["Resumo"]["A1"] = template.branding.company_name
    buffer = io.BytesIO()
    workbook.save(buffer)
    return RenderedFile(filename=safe_filename(expand_pattern(template.filename_pattern, result=result), extension="xlsx"), content=buffer.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", fmt="xlsx")


def render_zip(files: list[RenderedFile], filename: str = "reports.zip") -> RenderedFile:
    if len(files) > MAX_ZIP_FILES:
        raise ValueError("too_many_zip_files")
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            name = unique_name(file.filename, used)
            archive.writestr(name, file.content)
    return RenderedFile(filename=safe_filename(filename, extension="zip"), content=buffer.getvalue(), mimetype="application/zip", fmt="zip")


def store_rendered_file(base_dir: Path, run_id: int, file: RenderedFile) -> tuple[Path, str]:
    run_dir = (base_dir / str(run_id)).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    target = (run_dir / file.filename).resolve()
    if run_dir not in target.parents:
        raise ValueError("unsafe_output_path")
    target.write_bytes(file.content)
    return target, str(target)


def expand_pattern(pattern: str, *, result: PortfolioReportResult) -> str:
    return (pattern or "{portfolio}_{period}").format(
        portfolio=result.portfolio_name,
        period=result.period.label.replace(" ", "-"),
        profile=result.profile.name,
        engine=result.engine_version,
    )


def unique_name(filename: str, used: set[str]) -> str:
    if filename not in used:
        used.add(filename)
        return filename
    stem, dot, suffix = filename.rpartition(".")
    stem = stem or filename
    for index in range(2, 1000):
        candidate = f"{stem}_{index}{dot}{suffix}" if dot else f"{stem}_{index}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise ValueError("too_many_duplicate_filenames")
