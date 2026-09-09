# ruff: noqa: E501
"""One deterministic owner-pack DTO rendered to HTML and a real PDF."""

from __future__ import annotations

import hashlib
import html
import json
import textwrap
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from finance_agent.storage import canonical_json

PREPARATORY_LANGUAGE = (
    "Preparatory working material only. This pack is source-linked bookkeeping preparation, "
    "not tax or accounting advice and not a filed record."
)


@dataclass(frozen=True, slots=True)
class OwnerPackDTO:
    pack_version: str
    workspace_id: str
    workspace_name: str
    generated_at: str
    data_through: str
    currency: str
    preparatory_language: str
    owner_explanation: str
    totals: dict[str, int]
    unresolved_items: tuple[dict[str, Any], ...]
    forecast: dict[str, Any]
    source_manifest: tuple[dict[str, Any], ...]
    evidence_index: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonical_json(self) -> str:
        return canonical_json(self.as_dict())

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def format_nzd(amount_minor: int) -> str:
    sign = "-" if amount_minor < 0 else ""
    magnitude = abs(amount_minor)
    return f"{sign}NZD {magnitude // 100:,}.{magnitude % 100:02d}"


def owner_pack_text_lines(dto: OwnerPackDTO) -> list[str]:
    totals = dto.totals
    lines = [
        f"{dto.workspace_name} owner pack",
        dto.preparatory_language,
        f"Data through: {dto.data_through}",
        f"Generated: {dto.generated_at}",
        "Owner explanation",
        dto.owner_explanation,
        "Exact totals",
        f"Cleared balance: {format_nzd(totals['currentBalanceMinor'])}",
        f"Business income: {format_nzd(totals['businessIncomeMinor'])}",
        f"Business expenses: {format_nzd(totals['businessExpenseMinor'])}",
        f"Personal expenses: {format_nzd(totals['personalExpenseMinor'])}",
        f"Unresolved expenses: {format_nzd(totals['unresolvedExpenseMinor'])}",
        f"Protected reserve: {format_nzd(totals['protectedReserveMinor'])}",
        f"Projected 30-day low: {format_nzd(totals['projectedLowPointMinor'])}",
        f"Reserve shortfall: {format_nzd(totals['reserveShortfallMinor'])}",
        "Unresolved items",
    ]
    if dto.unresolved_items:
        for item in dto.unresolved_items:
            lines.append(
                f"{item['title']}: {format_nzd(item['amountMinor'])} "
                f"[{', '.join(item['evidenceIds'])}]"
            )
    else:
        lines.append("No unresolved expense items in the current prepared view.")

    lines.append("Forecast assumptions")
    lines.extend(f"- {assumption}" for assumption in dto.forecast["assumptions"])
    lines.append("Forecast event roll-forward")
    for point in dto.forecast["points"]:
        lines.append(
            f"{point['date']} — {point['label']}: "
            f"{format_nzd(point['amountMinor'])}; balance {format_nzd(point['balanceMinor'])}"
        )

    lines.append("Source manifest")
    for source in dto.source_manifest:
        lines.append(
            f"{source['sourceItemId']} — {source['label']} — SHA-256 {source['digest']} — "
            f"{source['rowCount']} rows — mapping {source['mappingVersion']}"
        )

    lines.append("Evidence index")
    for evidence in dto.evidence_index:
        lines.append(f"{evidence['evidenceId']} — {evidence['label']}")
    return lines


def render_owner_pack_html(dto: OwnerPackDTO) -> bytes:
    totals = dto.totals
    total_rows = (
        ("Cleared balance", totals["currentBalanceMinor"]),
        ("Business income", totals["businessIncomeMinor"]),
        ("Business expenses", totals["businessExpenseMinor"]),
        ("Personal expenses", totals["personalExpenseMinor"]),
        ("Unresolved expenses", totals["unresolvedExpenseMinor"]),
        ("Protected reserve", totals["protectedReserveMinor"]),
        ("Projected 30-day low", totals["projectedLowPointMinor"]),
        ("Reserve shortfall", totals["reserveShortfallMinor"]),
    )
    unresolved = (
        "".join(
            "<li><strong>{}</strong>: {} <code>{}</code></li>".format(
                html.escape(str(item["title"])),
                html.escape(format_nzd(int(item["amountMinor"]))),
                html.escape(", ".join(item["evidenceIds"])),
            )
            for item in dto.unresolved_items
        )
        or "<li>No unresolved expense items in the current prepared view.</li>"
    )
    forecast_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(point["date"]),
            html.escape(point["label"]),
            html.escape(format_nzd(point["amountMinor"])),
            html.escape(format_nzd(point["balanceMinor"])),
        )
        for point in dto.forecast["points"]
    )
    source_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
            html.escape(source["sourceItemId"]),
            html.escape(source["label"]),
            html.escape(source["digest"]),
            source["rowCount"],
            html.escape(source["mappingVersion"]),
        )
        for source in dto.source_manifest
    )
    evidence_rows = "".join(
        "<li><code>{}</code> — {}</li>".format(
            html.escape(evidence["evidenceId"]), html.escape(evidence["label"])
        )
        for evidence in dto.evidence_index
    )
    totals_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(format_nzd(value))}</td></tr>"
        for label, value in total_rows
    )
    assumptions = "".join(
        f"<li>{html.escape(assumption)}</li>" for assumption in dto.forecast["assumptions"]
    )
    document = f"""<!doctype html>
<html lang="en-NZ">
<head><meta charset="utf-8"><title>{html.escape(dto.workspace_name)} owner pack</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:40px auto;color:#17211b}}
table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}th,td{{border:1px solid #ccd4ce;padding:8px;text-align:left}}
.notice{{padding:12px;border-left:4px solid #ad7b00;background:#fff8df}}code{{font-size:12px;word-break:break-all}}</style></head>
<body data-pack-version="{html.escape(dto.pack_version)}" data-dto-hash="{dto.content_hash()}">
<h1>{html.escape(dto.workspace_name)} owner pack</h1>
<p class="notice">{html.escape(dto.preparatory_language)}</p>
<p><strong>Data through:</strong> {html.escape(dto.data_through)}<br>
<strong>Generated:</strong> {html.escape(dto.generated_at)}</p>
<h2>Owner explanation</h2><p>{html.escape(dto.owner_explanation)}</p>
<h2>Exact totals</h2><table><tbody>{totals_html}</tbody></table>
<h2>Unresolved items</h2><ul>{unresolved}</ul>
<h2>30-day forecast</h2><ul>{assumptions}</ul>
<table><thead><tr><th>Date</th><th>Event</th><th>Amount</th><th>Balance</th></tr></thead><tbody>{forecast_rows}</tbody></table>
<h2>Source manifest</h2><table><thead><tr><th>ID</th><th>Source</th><th>SHA-256</th><th>Rows</th><th>Mapping</th></tr></thead><tbody>{source_rows}</tbody></table>
<h2>Evidence index</h2><ul>{evidence_rows}</ul>
</body></html>"""
    return document.encode("utf-8")


def render_owner_pack_pdf(dto: OwnerPackDTO) -> bytes:
    """Render deterministic PDF bytes with ReportLab's invariant mode."""

    buffer = BytesIO()
    page_width, page_height = A4
    pdf = Canvas(
        buffer,
        pagesize=A4,
        invariant=1,
        pageCompression=0,
        pdfVersion=(1, 4),
    )
    pdf.setTitle(f"{dto.workspace_name} owner pack")
    pdf.setAuthor("Standalone Finance Agent")
    x = 52
    y = page_height - 52
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(x, y, f"{dto.workspace_name} owner pack")
    y -= 26
    pdf.setFont("Helvetica", 9)
    for source_line in owner_pack_text_lines(dto)[1:]:
        wrapped = textwrap.wrap(source_line, width=100, break_long_words=True) or [""]
        for line in wrapped:
            if y < 52:
                pdf.showPage()
                pdf.setFont("Helvetica", 9)
                y = page_height - 52
            pdf.drawString(x, y, line)
            y -= 13
        y -= 2
    pdf.setSubject(f"Deterministic DTO SHA-256 {dto.content_hash()}")
    pdf.save()
    return buffer.getvalue()


def render_owner_pack(dto: OwnerPackDTO) -> tuple[bytes, bytes]:
    return render_owner_pack_html(dto), render_owner_pack_pdf(dto)


def parse_owner_pack_dto(value: str | bytes) -> OwnerPackDTO:
    parsed = json.loads(value)
    return OwnerPackDTO(
        pack_version=parsed["pack_version"],
        workspace_id=parsed["workspace_id"],
        workspace_name=parsed["workspace_name"],
        generated_at=parsed["generated_at"],
        data_through=parsed["data_through"],
        currency=parsed["currency"],
        preparatory_language=parsed["preparatory_language"],
        owner_explanation=parsed["owner_explanation"],
        totals=parsed["totals"],
        unresolved_items=tuple(parsed["unresolved_items"]),
        forecast=parsed["forecast"],
        source_manifest=tuple(parsed["source_manifest"]),
        evidence_index=tuple(parsed["evidence_index"]),
    )
