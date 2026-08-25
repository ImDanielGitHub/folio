from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="sales_invoice_drafts",
        sql="""
        CREATE TABLE sales_invoices (
            invoice_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            invoice_number TEXT NOT NULL CHECK (length(trim(invoice_number)) BETWEEN 1 AND 80),
            seller_name TEXT NOT NULL CHECK (length(trim(seller_name)) BETWEEN 1 AND 200),
            seller_nzbn TEXT,
            buyer_name TEXT NOT NULL CHECK (length(trim(buyer_name)) BETWEEN 1 AND 200),
            buyer_nzbn TEXT,
            issue_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            currency TEXT NOT NULL CHECK (currency = 'NZD'),
            status TEXT NOT NULL CHECK (status IN ('draft', 'issued', 'void')),
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (workspace_id, invoice_number)
        );

        CREATE TABLE sales_invoice_lines (
            line_id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL REFERENCES sales_invoices(invoice_id) ON DELETE CASCADE,
            description TEXT NOT NULL CHECK (length(trim(description)) BETWEEN 1 AND 500),
            quantity_millis INTEGER NOT NULL CHECK (quantity_millis > 0),
            unit_price_minor INTEGER NOT NULL CHECK (unit_price_minor >= 0),
            tax_treatment TEXT NOT NULL CHECK (
                tax_treatment IN ('standard', 'zero_rated', 'exempt', 'out_of_scope')
            ),
            sort_order INTEGER NOT NULL CHECK (sort_order >= 1),
            UNIQUE (invoice_id, sort_order)
        );

        CREATE TABLE sales_invoice_revisions (
            invoice_id TEXT NOT NULL REFERENCES sales_invoices(invoice_id),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            status TEXT NOT NULL CHECK (status IN ('draft', 'issued', 'void')),
            payload_json TEXT NOT NULL,
            ubl_xml BLOB NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY (invoice_id, revision)
        );

        CREATE INDEX sales_invoice_workspace_status
            ON sales_invoices(workspace_id, status, issue_date, invoice_number);
        """,
    ),
'''

INVOICE_MODULE = '''"""Exact-money sales invoice drafts and deterministic UBL 2.1 XML."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from xml.etree import ElementTree as ET

from finance_agent.storage import SQLiteStore, canonical_json

UBL_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
ET.register_namespace("", UBL_NS)
ET.register_namespace("cbc", CBC_NS)
ET.register_namespace("cac", CAC_NS)
NZBN_PATTERN = re.compile(r"^\\d{13}$")
GST_RATE_BASIS_POINTS = 1500
DRAFT_CUSTOMISATION_ID = "folio.nz.ubl-draft@1"
DRAFT_NOTICE = (
    "UBL 2.1 invoice draft generated locally. Folio has not transmitted, delivered, "
    "registered, paid or verified this invoice through an eInvoicing network."
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _money(value: int) -> str:
    return f"{Decimal(value) / Decimal(100):.2f}"


def _round_minor(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _line_net_minor(quantity_millis: int, unit_price_minor: int) -> int:
    return _round_minor(
        Decimal(quantity_millis) * Decimal(unit_price_minor) / Decimal(1000)
    )


def _gst_minor(net_minor: int, treatment: str) -> int:
    if treatment != "standard":
        return 0
    return _round_minor(
        Decimal(net_minor) * Decimal(GST_RATE_BASIS_POINTS) / Decimal(10000)
    )


def _text(parent: ET.Element, namespace: str, name: str, value: object) -> ET.Element:
    element = ET.SubElement(parent, f"{{{namespace}}}{name}")
    element.text = str(value)
    return element


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    line_id: str
    description: str
    quantity_millis: int
    unit_price_minor: int
    tax_treatment: str
    sort_order: int

    @property
    def net_minor(self) -> int:
        return _line_net_minor(self.quantity_millis, self.unit_price_minor)

    @property
    def gst_minor(self) -> int:
        return _gst_minor(self.net_minor, self.tax_treatment)

    @property
    def gross_minor(self) -> int:
        return self.net_minor + self.gst_minor

    def as_dict(self) -> dict[str, object]:
        return {
            "lineId": self.line_id,
            "description": self.description,
            "quantityMillis": self.quantity_millis,
            "unitPriceMinor": self.unit_price_minor,
            "taxTreatment": self.tax_treatment,
            "sortOrder": self.sort_order,
            "netMinor": self.net_minor,
            "gstMinor": self.gst_minor,
            "grossMinor": self.gross_minor,
        }


@dataclass(frozen=True, slots=True)
class InvoiceDocument:
    invoice_id: str
    workspace_id: str
    invoice_number: str
    seller_name: str
    seller_nzbn: str | None
    buyer_name: str
    buyer_nzbn: str | None
    issue_date: str
    due_date: str
    status: str
    notes: str | None
    lines: tuple[InvoiceLine, ...]

    @property
    def net_minor(self) -> int:
        return sum(line.net_minor for line in self.lines)

    @property
    def gst_minor(self) -> int:
        return sum(line.gst_minor for line in self.lines)

    @property
    def gross_minor(self) -> int:
        return self.net_minor + self.gst_minor

    def as_dict(self) -> dict[str, object]:
        return {
            "invoiceVersion": "sales.invoice@1",
            "invoiceId": self.invoice_id,
            "workspaceId": self.workspace_id,
            "invoiceNumber": self.invoice_number,
            "seller": {"name": self.seller_name, "nzbn": self.seller_nzbn},
            "buyer": {"name": self.buyer_name, "nzbn": self.buyer_nzbn},
            "issueDate": self.issue_date,
            "dueDate": self.due_date,
            "currency": "NZD",
            "status": self.status,
            "notes": self.notes,
            "lines": [line.as_dict() for line in self.lines],
            "totals": {
                "netMinor": self.net_minor,
                "gstMinor": self.gst_minor,
                "grossMinor": self.gross_minor,
            },
            "customisationId": DRAFT_CUSTOMISATION_ID,
            "notice": DRAFT_NOTICE,
            "transmitted": False,
            "delivered": False,
            "paid": False,
        }


def render_ubl(invoice: InvoiceDocument) -> bytes:
    root = ET.Element(f"{{{UBL_NS}}}Invoice")
    _text(root, CBC_NS, "CustomizationID", DRAFT_CUSTOMISATION_ID)
    _text(root, CBC_NS, "ProfileID", "folio.invoice-draft")
    _text(root, CBC_NS, "ID", invoice.invoice_number)
    _text(root, CBC_NS, "IssueDate", invoice.issue_date)
    _text(root, CBC_NS, "DueDate", invoice.due_date)
    _text(root, CBC_NS, "InvoiceTypeCode", "380")
    _text(root, CBC_NS, "DocumentCurrencyCode", "NZD")
    if invoice.notes:
        _text(root, CBC_NS, "Note", invoice.notes)

    supplier = ET.SubElement(root, f"{{{CAC_NS}}}AccountingSupplierParty")
    supplier_party = ET.SubElement(supplier, f"{{{CAC_NS}}}Party")
    supplier_name = ET.SubElement(supplier_party, f"{{{CAC_NS}}}PartyName")
    _text(supplier_name, CBC_NS, "Name", invoice.seller_name)
    supplier_legal = ET.SubElement(supplier_party, f"{{{CAC_NS}}}PartyLegalEntity")
    _text(supplier_legal, CBC_NS, "RegistrationName", invoice.seller_name)
    if invoice.seller_nzbn:
        company = _text(supplier_legal, CBC_NS, "CompanyID", invoice.seller_nzbn)
        company.set("schemeID", "NZBN")

    customer = ET.SubElement(root, f"{{{CAC_NS}}}AccountingCustomerParty")
    customer_party = ET.SubElement(customer, f"{{{CAC_NS}}}Party")
    customer_name = ET.SubElement(customer_party, f"{{{CAC_NS}}}PartyName")
    _text(customer_name, CBC_NS, "Name", invoice.buyer_name)
    customer_legal = ET.SubElement(customer_party, f"{{{CAC_NS}}}PartyLegalEntity")
    _text(customer_legal, CBC_NS, "RegistrationName", invoice.buyer_name)
    if invoice.buyer_nzbn:
        company = _text(customer_legal, CBC_NS, "CompanyID", invoice.buyer_nzbn)
        company.set("schemeID", "NZBN")

    tax_total = ET.SubElement(root, f"{{{CAC_NS}}}TaxTotal")
    tax_amount = _text(tax_total, CBC_NS, "TaxAmount", _money(invoice.gst_minor))
    tax_amount.set("currencyID", "NZD")

    monetary = ET.SubElement(root, f"{{{CAC_NS}}}LegalMonetaryTotal")
    for name, amount in (
        ("LineExtensionAmount", invoice.net_minor),
        ("TaxExclusiveAmount", invoice.net_minor),
        ("TaxInclusiveAmount", invoice.gross_minor),
        ("PayableAmount", invoice.gross_minor),
    ):
        element = _text(monetary, CBC_NS, name, _money(amount))
        element.set("currencyID", "NZD")

    for line in invoice.lines:
        value = ET.SubElement(root, f"{{{CAC_NS}}}InvoiceLine")
        _text(value, CBC_NS, "ID", line.sort_order)
        quantity = _text(
            value,
            CBC_NS,
            "InvoicedQuantity",
            Decimal(line.quantity_millis) / Decimal(1000),
        )
        quantity.set("unitCode", "EA")
        extension = _text(value, CBC_NS, "LineExtensionAmount", _money(line.net_minor))
        extension.set("currencyID", "NZD")
        item = ET.SubElement(value, f"{{{CAC_NS}}}Item")
        _text(item, CBC_NS, "Description", line.description)
        category = ET.SubElement(item, f"{{{CAC_NS}}}ClassifiedTaxCategory")
        tax_code = {
            "standard": "S",
            "zero_rated": "Z",
            "exempt": "E",
            "out_of_scope": "O",
        }[line.tax_treatment]
        _text(category, CBC_NS, "ID", tax_code)
        _text(
            category,
            CBC_NS,
            "Percent",
            "15.00" if line.tax_treatment == "standard" else "0.00",
        )
        scheme = ET.SubElement(category, f"{{{CAC_NS}}}TaxScheme")
        _text(scheme, CBC_NS, "ID", "GST")
        price = ET.SubElement(value, f"{{{CAC_NS}}}Price")
        price_amount = _text(price, CBC_NS, "PriceAmount", _money(line.unit_price_minor))
        price_amount.set("currencyID", "NZD")
        _text(price, CBC_NS, "BaseQuantity", "1")

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class SalesInvoiceService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def _load(self, invoice_id: str) -> InvoiceDocument:
        invoice = self.store.fetch_one(
            "SELECT * FROM sales_invoices WHERE invoice_id = ?", (invoice_id,)
        )
        if invoice is None:
            raise KeyError(invoice_id)
        rows = self.store.fetch_all(
            "SELECT * FROM sales_invoice_lines WHERE invoice_id = ? ORDER BY sort_order",
            (invoice_id,),
        )
        return InvoiceDocument(
            invoice_id=str(invoice["invoice_id"]),
            workspace_id=str(invoice["workspace_id"]),
            invoice_number=str(invoice["invoice_number"]),
            seller_name=str(invoice["seller_name"]),
            seller_nzbn=str(invoice["seller_nzbn"]) if invoice["seller_nzbn"] else None,
            buyer_name=str(invoice["buyer_name"]),
            buyer_nzbn=str(invoice["buyer_nzbn"]) if invoice["buyer_nzbn"] else None,
            issue_date=str(invoice["issue_date"]),
            due_date=str(invoice["due_date"]),
            status=str(invoice["status"]),
            notes=str(invoice["notes"]) if invoice["notes"] else None,
            lines=tuple(
                InvoiceLine(
                    line_id=str(row["line_id"]),
                    description=str(row["description"]),
                    quantity_millis=int(row["quantity_millis"]),
                    unit_price_minor=int(row["unit_price_minor"]),
                    tax_treatment=str(row["tax_treatment"]),
                    sort_order=int(row["sort_order"]),
                )
                for row in rows
            ),
        )

    def save_draft(
        self,
        *,
        workspace_id: str,
        invoice_id: str | None,
        invoice_number: str,
        seller_name: str,
        seller_nzbn: str | None,
        buyer_name: str,
        buyer_nzbn: str | None,
        issue_date: str,
        due_date: str,
        notes: str | None,
        lines: tuple[dict[str, object], ...],
    ) -> InvoiceDocument:
        number = invoice_number.strip()
        seller = seller_name.strip()
        buyer = buyer_name.strip()
        if not number or not seller or not buyer:
            raise ValueError("invoice number, seller and buyer are required")
        try:
            issue = date.fromisoformat(issue_date)
            due = date.fromisoformat(due_date)
        except ValueError as exc:
            raise ValueError("invoice dates must use YYYY-MM-DD") from exc
        if due < issue:
            raise ValueError("invoice due date must be on or after issue date")
        for nzbn, label in ((seller_nzbn, "sellerNzbn"), (buyer_nzbn, "buyerNzbn")):
            if nzbn and not NZBN_PATTERN.fullmatch(nzbn):
                raise ValueError(f"{label} must contain 13 digits")
        if not lines or len(lines) > 200:
            raise ValueError("invoice must contain between 1 and 200 lines")
        now = datetime.now(UTC).isoformat()
        value_id = invoice_id or _stable_id(
            "invoice", workspace_id, number, issue.isoformat(), now
        )
        parsed_lines: list[InvoiceLine] = []
        for index, raw in enumerate(lines, start=1):
            description = str(raw.get("description") or "").strip()
            quantity = raw.get("quantityMillis")
            unit_price = raw.get("unitPriceMinor")
            treatment = str(raw.get("taxTreatment") or "standard")
            if not description:
                raise ValueError(f"invoice line {index} description is required")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError(f"invoice line {index} quantityMillis must be positive")
            if isinstance(unit_price, bool) or not isinstance(unit_price, int) or unit_price < 0:
                raise ValueError(f"invoice line {index} unitPriceMinor must be non-negative")
            if treatment not in {"standard", "zero_rated", "exempt", "out_of_scope"}:
                raise ValueError(f"invoice line {index} tax treatment is unsupported")
            parsed_lines.append(
                InvoiceLine(
                    line_id=_stable_id("invoiceline", value_id, str(index)),
                    description=description[:500],
                    quantity_millis=quantity,
                    unit_price_minor=unit_price,
                    tax_treatment=treatment,
                    sort_order=index,
                )
            )
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT workspace_id, status FROM sales_invoices WHERE invoice_id = ?",
                (value_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["workspace_id"]) != workspace_id:
                    raise ValueError("invoice belongs to another workspace")
                if str(existing["status"]) != "draft":
                    raise ValueError("only draft invoices can be edited")
            connection.execute(
                """
                INSERT INTO sales_invoices(
                    invoice_id, workspace_id, invoice_number, seller_name,
                    seller_nzbn, buyer_name, buyer_nzbn, issue_date, due_date,
                    currency, status, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'NZD', 'draft', ?, ?, ?)
                ON CONFLICT(invoice_id) DO UPDATE SET
                    invoice_number = excluded.invoice_number,
                    seller_name = excluded.seller_name,
                    seller_nzbn = excluded.seller_nzbn,
                    buyer_name = excluded.buyer_name,
                    buyer_nzbn = excluded.buyer_nzbn,
                    issue_date = excluded.issue_date,
                    due_date = excluded.due_date,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    value_id,
                    workspace_id,
                    number[:80],
                    seller[:200],
                    seller_nzbn,
                    buyer[:200],
                    buyer_nzbn,
                    issue.isoformat(),
                    due.isoformat(),
                    notes.strip()[:2000] if notes else None,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM sales_invoice_lines WHERE invoice_id = ?",
                (value_id,),
            )
            connection.executemany(
                """
                INSERT INTO sales_invoice_lines(
                    line_id, invoice_id, description, quantity_millis,
                    unit_price_minor, tax_treatment, sort_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        line.line_id,
                        value_id,
                        line.description,
                        line.quantity_millis,
                        line.unit_price_minor,
                        line.tax_treatment,
                        line.sort_order,
                    )
                    for line in parsed_lines
                ],
            )
        value = self._load(value_id)
        self._record_revision(value)
        return value

    def _record_revision(self, invoice: InvoiceDocument) -> tuple[int, str]:
        payload = invoice.as_dict()
        xml = render_ubl(invoice)
        encoded = canonical_json(payload)
        content_hash = hashlib.sha256(xml + b"\\0" + encoded.encode()).hexdigest()
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM sales_invoice_revisions WHERE invoice_id = ?
                """,
                (invoice.invoice_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO sales_invoice_revisions(
                    invoice_id, revision, status, payload_json, ubl_xml,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice.invoice_id,
                    revision,
                    invoice.status,
                    encoded,
                    xml,
                    content_hash,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return revision, content_hash

    def issue(self, *, workspace_id: str, invoice_id: str) -> dict[str, object]:
        invoice = self._load(invoice_id)
        if invoice.workspace_id != workspace_id:
            raise KeyError(invoice_id)
        if invoice.status != "draft":
            raise ValueError("only a draft invoice can be issued")
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE sales_invoices SET status = 'issued', updated_at = ?
                WHERE invoice_id = ? AND workspace_id = ? AND status = 'draft'
                """,
                (now, invoice_id, workspace_id),
            )
        issued = self._load(invoice_id)
        revision, content_hash = self._record_revision(issued)
        return {
            **issued.as_dict(),
            "revision": revision,
            "contentHash": content_hash,
            "issuedLocally": True,
            "transmitted": False,
            "delivered": False,
        }

    def void(self, *, workspace_id: str, invoice_id: str) -> dict[str, object]:
        invoice = self._load(invoice_id)
        if invoice.workspace_id != workspace_id:
            raise KeyError(invoice_id)
        if invoice.status == "void":
            return {**invoice.as_dict(), "status": "void"}
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE sales_invoices SET status = 'void', updated_at = ? WHERE invoice_id = ? AND workspace_id = ?",
                (datetime.now(UTC).isoformat(), invoice_id, workspace_id),
            )
        voided = self._load(invoice_id)
        revision, content_hash = self._record_revision(voided)
        return {**voided.as_dict(), "revision": revision, "contentHash": content_hash}

    def ubl_payload(self, *, workspace_id: str, invoice_id: str) -> tuple[InvoiceDocument, bytes, str]:
        invoice = self._load(invoice_id)
        if invoice.workspace_id != workspace_id:
            raise KeyError(invoice_id)
        xml = render_ubl(invoice)
        return invoice, xml, hashlib.sha256(xml).hexdigest()

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        rows = self.store.fetch_all(
            """
            SELECT invoice_id FROM sales_invoices
            WHERE workspace_id = ? ORDER BY issue_date DESC, invoice_number DESC
            """,
            (workspace_id,),
        )
        return tuple(self._load(str(row["invoice_id"])).as_dict() for row in rows)
'''

SERVICE_METHODS = '''    async def save_invoice_draft(
        self,
        *,
        workspace_id: str,
        invoice_id: str | None,
        invoice_number: str,
        seller_name: str,
        seller_nzbn: str | None,
        buyer_name: str,
        buyer_nzbn: str | None,
        issue_date: str,
        due_date: str,
        notes: str | None,
        lines: tuple[Mapping[str, object], ...],
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            invoice = SalesInvoiceService(self.store).save_draft(
                workspace_id=workspace_id,
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                seller_name=seller_name,
                seller_nzbn=seller_nzbn,
                buyer_name=buyer_name,
                buyer_nzbn=buyer_nzbn,
                issue_date=issue_date,
                due_date=due_date,
                notes=notes,
                lines=tuple(dict(line) for line in lines),
            )
        return invoice.as_dict()

    async def list_invoices(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return SalesInvoiceService(self.store).list(workspace_id)

    async def issue_invoice(
        self, *, workspace_id: str, invoice_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return SalesInvoiceService(self.store).issue(
                workspace_id=workspace_id, invoice_id=invoice_id
            )

    async def void_invoice(
        self, *, workspace_id: str, invoice_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return SalesInvoiceService(self.store).void(
                workspace_id=workspace_id, invoice_id=invoice_id
            )

    async def invoice_ubl_payload(
        self, *, workspace_id: str, invoice_id: str
    ) -> ArtifactPayload:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        invoice, content, content_hash = SalesInvoiceService(self.store).ubl_payload(
            workspace_id=workspace_id, invoice_id=invoice_id
        )
        return ArtifactPayload(
            content=content,
            media_type="application/xml; charset=utf-8",
            filename=f"{invoice.invoice_number}.xml",
            content_hash=content_hash,
        )
'''

ROUTE_MODELS = '''

class InvoiceLineRequest(RequestModel):
    description: str = Field(min_length=1, max_length=500)
    quantity_millis: int = Field(alias="quantityMillis", gt=0, le=1_000_000_000)
    unit_price_minor: int = Field(alias="unitPriceMinor", ge=0)
    tax_treatment: str = Field(
        default="standard",
        alias="taxTreatment",
        pattern=r"^(standard|zero_rated|exempt|out_of_scope)$",
    )


class InvoiceDraftRequest(RequestModel):
    invoice_id: str | None = Field(
        default=None, alias="invoiceId", pattern=IDENTIFIER_PATTERN
    )
    invoice_number: str = Field(alias="invoiceNumber", min_length=1, max_length=80)
    seller_name: str = Field(alias="sellerName", min_length=1, max_length=200)
    seller_nzbn: str | None = Field(default=None, alias="sellerNzbn", pattern=r"^\\d{13}$")
    buyer_name: str = Field(alias="buyerName", min_length=1, max_length=200)
    buyer_nzbn: str | None = Field(default=None, alias="buyerNzbn", pattern=r"^\\d{13}$")
    issue_date: date = Field(alias="issueDate")
    due_date: date = Field(alias="dueDate")
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[InvoiceLineRequest] = Field(min_length=1, max_length=200)
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/invoices")
    async def list_invoices(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        invoices = await services.list_invoices(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "invoices": list(invoices)}

    @router.post("/v1/workspaces/{workspace_id}/invoices", status_code=201)
    async def save_invoice_draft(
        workspace_id: PathIdentifier,
        body: InvoiceDraftRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.save_invoice_draft(
                    workspace_id=workspace_id,
                    invoice_id=body.invoice_id,
                    invoice_number=body.invoice_number,
                    seller_name=body.seller_name,
                    seller_nzbn=body.seller_nzbn,
                    buyer_name=body.buyer_name,
                    buyer_nzbn=body.buyer_nzbn,
                    issue_date=body.issue_date.isoformat(),
                    due_date=body.due_date.isoformat(),
                    notes=body.notes,
                    lines=tuple(line.model_dump(by_alias=True) for line in body.lines),
                )
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/invoices/{invoice_id}/issue")
    async def issue_invoice(
        workspace_id: PathIdentifier,
        invoice_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.issue_invoice(
                    workspace_id=workspace_id,
                    invoice_id=invoice_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="invoice not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/invoices/{invoice_id}/void")
    async def void_invoice(
        workspace_id: PathIdentifier,
        invoice_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.void_invoice(
                    workspace_id=workspace_id,
                    invoice_id=invoice_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="invoice not found") from exc

    @router.get("/v1/workspaces/{workspace_id}/invoices/{invoice_id}.xml")
    async def invoice_ubl_xml(
        workspace_id: PathIdentifier,
        invoice_id: PathIdentifier,
        services: Services,
    ) -> Response:
        try:
            value = await services.invoice_ubl_payload(
                workspace_id=workspace_id,
                invoice_id=invoice_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="invoice not found") from exc
        return Response(
            content=value.content,
            media_type=value.media_type,
            headers={
                "Content-Disposition": content_disposition(
                    value.filename, disposition="attachment"
                ),
                "ETag": f'"{value.content_hash}"',
                "Cache-Control": "no-store",
            },
        )

'''

TESTS = '''from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.finance.invoices import (
    CAC_NS,
    CBC_NS,
    UBL_NS,
    SalesInvoiceService,
    render_ubl,
)
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def service(tmp_path: Path) -> SalesInvoiceService:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    return SalesInvoiceService(store)


def draft(value: SalesInvoiceService):
    return value.save_draft(
        workspace_id="ws_koru_studio",
        invoice_id=None,
        invoice_number="INV-2026-001",
        seller_name="Koru Studio",
        seller_nzbn="9429041234567",
        buyer_name="Acme Limited",
        buyer_nzbn="9429047654321",
        issue_date="2026-08-26",
        due_date="2026-09-09",
        notes="Thank you for your business.",
        lines=(
            {
                "description": "Design services",
                "quantityMillis": 1500,
                "unitPriceMinor": 10000,
                "taxTreatment": "standard",
            },
            {
                "description": "Zero-rated service",
                "quantityMillis": 1000,
                "unitPriceMinor": 5000,
                "taxTreatment": "zero_rated",
            },
        ),
    )


def test_invoice_totals_use_exact_minor_units_and_explicit_tax_treatment(tmp_path: Path) -> None:
    invoice = draft(service(tmp_path))
    assert invoice.lines[0].net_minor == 15000
    assert invoice.lines[0].gst_minor == 2250
    assert invoice.lines[1].net_minor == 5000
    assert invoice.lines[1].gst_minor == 0
    assert invoice.net_minor == 20000
    assert invoice.gst_minor == 2250
    assert invoice.gross_minor == 22250
    payload = invoice.as_dict()
    assert payload["transmitted"] is False
    assert payload["delivered"] is False
    assert payload["paid"] is False


def test_ubl_contains_required_invoice_parties_totals_and_line_values(tmp_path: Path) -> None:
    invoice = draft(service(tmp_path))
    xml = render_ubl(invoice)
    root = ET.fromstring(xml)
    namespaces = {"ubl": UBL_NS, "cbc": CBC_NS, "cac": CAC_NS}
    assert root.findtext("cbc:CustomizationID", namespaces=namespaces) == "folio.nz.ubl-draft@1"
    assert root.findtext("cbc:ID", namespaces=namespaces) == "INV-2026-001"
    assert root.findtext("cbc:DocumentCurrencyCode", namespaces=namespaces) == "NZD"
    assert root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=namespaces) == "222.50"
    lines = root.findall("cac:InvoiceLine", namespaces=namespaces)
    assert len(lines) == 2
    assert lines[0].findtext("cac:Item/cbc:Description", namespaces=namespaces) == "Design services"


def test_issued_invoice_is_immutable_and_never_claims_network_delivery(tmp_path: Path) -> None:
    value = service(tmp_path)
    invoice = draft(value)
    issued = value.issue(
        workspace_id="ws_koru_studio",
        invoice_id=invoice.invoice_id,
    )
    assert issued["status"] == "issued"
    assert issued["issuedLocally"] is True
    assert issued["transmitted"] is False
    assert issued["delivered"] is False
    with pytest.raises(ValueError, match="only draft"):
        value.save_draft(
            workspace_id="ws_koru_studio",
            invoice_id=invoice.invoice_id,
            invoice_number="INV-2026-001",
            seller_name="Changed",
            seller_nzbn=None,
            buyer_name="Acme",
            buyer_nzbn=None,
            issue_date="2026-08-26",
            due_date="2026-09-09",
            notes=None,
            lines=({"description": "Changed", "quantityMillis": 1000, "unitPriceMinor": 1, "taxTreatment": "standard"},),
        )
    rows = value.store.fetch_all(
        "SELECT revision, status FROM sales_invoice_revisions WHERE invoice_id = ? ORDER BY revision",
        (invoice.invoice_id,),
    )
    assert [(int(row["revision"]), str(row["status"])) for row in rows] == [(1, "draft"), (2, "issued")]


def test_duplicate_invoice_number_and_invalid_nzbn_fail_closed(tmp_path: Path) -> None:
    value = service(tmp_path)
    draft(value)
    with pytest.raises(Exception):
        draft(value)
    with pytest.raises(ValueError, match="13 digits"):
        value.save_draft(
            workspace_id="ws_koru_studio",
            invoice_id=None,
            invoice_number="INV-INVALID",
            seller_name="Koru",
            seller_nzbn="123",
            buyer_name="Acme",
            buyer_nzbn=None,
            issue_date="2026-08-26",
            due_date="2026-09-09",
            notes=None,
            lines=({"description": "Work", "quantityMillis": 1000, "unitPriceMinor": 100, "taxTreatment": "standard"},),
        )
'''


def add_migration() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])


def add_module_and_service() -> None:
    write("services/api/src/finance_agent/finance/invoices.py", INVOICE_MODULE)
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.accountant_export import AccountantExportService\n"
    import_line = "from finance_agent.finance.invoices import SalesInvoiceService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("accountant export import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "workspace_directory_entries", SERVICE_METHODS)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def workspace_directory_entries(\n"
    addition = '''    async def save_invoice_draft(\n        self, *, workspace_id: str, invoice_id: str | None, invoice_number: str,\n        seller_name: str, seller_nzbn: str | None, buyer_name: str,\n        buyer_nzbn: str | None, issue_date: str, due_date: str,\n        notes: str | None, lines: tuple[Mapping[str, object], ...]\n    ) -> Mapping[str, object]: ...\n\n    async def list_invoices(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def issue_invoice(\n        self, *, workspace_id: str, invoice_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def void_invoice(\n        self, *, workspace_id: str, invoice_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def invoice_ubl_payload(\n        self, *, workspace_id: str, invoice_id: str\n    ) -> ArtifactPayload: ...\n\n'''
    if marker not in content:
        raise RuntimeError("workspace directory protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass AccountingMappingRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("AccountingMappingRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get(\n        "/v1/workspace-directory",\n'
    if route_marker not in content:
        raise RuntimeError("workspace directory route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/finance/test_sales_invoice_drafts.py", TESTS)
    write("docs/EINVOICE_DRAFTS.md", '''# Invoice and UBL draft boundary\n\nFolio prepares exact-money NZD sales invoices with explicit line quantities, unit prices and GST treatments. Draft revisions are append-only. Issuing is a deliberate local lifecycle transition; issued invoices cannot be silently edited and can later be voided with another revision. Payment is never inferred from an invoice lifecycle state.\n\nThe XML output uses the OASIS UBL 2.1 Invoice namespace and includes supplier, customer, dates, currency, tax total, monetary totals and invoice lines. It carries `folio.nz.ubl-draft@1` as its customisation identifier. This deliberately avoids claiming Peppol, MBIE network or recipient conformance without a current profile validator and observed transmission.\n\nAn XML download proves only that Folio generated deterministic bytes. It does not prove network registration, transmission, delivery, acceptance, payment or accounting-system posting.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 24: exact invoice and UBL draft preparation\n\n- Sales invoices use exact NZD minor units, explicit quantity and explicit GST treatment.\n- Draft, issued and void states are separate and revisions are append-only.\n- Issued invoices cannot be silently edited.\n- Deterministic UBL 2.1 XML includes parties, dates, tax, totals and lines.\n- Customisation is labelled as a Folio draft rather than Peppol conformance.\n- Transmission, delivery, acceptance, payment and external posting remain unclaimed.\n'''
    if "## Stack 24: exact invoice and UBL draft preparation" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_module_and_service()
    update_protocol_and_routes()
    add_tests_and_docs()
    print("invoice draft changes applied")


if __name__ == "__main__":
    main()
