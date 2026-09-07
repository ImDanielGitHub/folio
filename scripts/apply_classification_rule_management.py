from __future__ import annotations

import ast
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


MODULE = '''"""Dry-run impact, conflict inspection and reversible classification-rule management."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from finance_agent.finance.classification import classification_for, rule_matches
from finance_agent.finance.domain import ClassificationRule, Transaction
from finance_agent.finance.service import FinanceEngine
from finance_agent.storage import SQLiteStore

ALLOWED_CLASSIFICATIONS = frozenset({"business", "personal", "unresolved"})


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _normalise(value: str) -> str:
    return " ".join(value.upper().split())


@dataclass(frozen=True, slots=True)
class ProposedRule:
    merchant_contains: str
    maximum_amount_minor: int
    currency: str
    target_classification: str
    target_category: str | None
    effective_from: str
    priority: int

    def as_domain(self) -> ClassificationRule:
        return ClassificationRule(
            rule_id=_stable_id(
                "previewrule",
                self.merchant_contains,
                str(self.maximum_amount_minor),
                self.currency,
                self.target_classification,
                self.target_category or "",
                self.effective_from,
                str(self.priority),
            ),
            merchant_contains=self.merchant_contains,
            maximum_amount_minor=self.maximum_amount_minor,
            currency=self.currency,
            target_classification=self.target_classification,
            target_category=self.target_category,
            effective_from=self.effective_from,
            priority=self.priority,
        )


class ClassificationRuleManagementService:
    def __init__(self, store: SQLiteStore, engine: FinanceEngine) -> None:
        self.store = store
        self.engine = engine

    @staticmethod
    def _transaction(row: Any) -> Transaction:
        return Transaction(
            transaction_id=str(row["transaction_id"]),
            occurred_on=str(row["occurred_on"]),
            description=str(row["description"]),
            amount_minor=int(row["amount_minor"]),
            currency=str(row["currency"]),
            source_status=str(row["source_status"]),
            status=str(row["status"]),
            classification=str(row["classification"]),
            category=str(row["category"]) if row["category"] else None,
            classification_source=str(row["classification_source"]),
            rule_id=str(row["rule_id"]) if row["rule_id"] else None,
            evidence_id=str(row["evidence_id"]),
            duplicate_of_transaction_id=(
                str(row["duplicate_of_transaction_id"])
                if row["duplicate_of_transaction_id"] else None
            ),
        )

    @staticmethod
    def _validate(
        *,
        merchant_contains: str,
        maximum_amount_minor: int,
        currency: str,
        target_classification: str,
        target_category: str | None,
        effective_from: str,
        priority: int,
    ) -> ProposedRule:
        merchant = " ".join(merchant_contains.split())
        if not merchant:
            raise ValueError("merchantContains must not be blank")
        if len(merchant) > 200:
            raise ValueError("merchantContains cannot exceed 200 characters")
        if isinstance(maximum_amount_minor, bool) or not isinstance(maximum_amount_minor, int):
            raise ValueError("maximumAmountMinor must be an integer")
        if maximum_amount_minor < 0:
            raise ValueError("maximumAmountMinor must be non-negative")
        if currency != "NZD":
            raise ValueError("classification rules currently support NZD only")
        if target_classification not in ALLOWED_CLASSIFICATIONS:
            raise ValueError("unsupported targetClassification")
        category = target_category.strip() if target_category else None
        if category and len(category) > 200:
            raise ValueError("targetCategory cannot exceed 200 characters")
        date.fromisoformat(effective_from)
        if not -10000 <= priority <= 10000:
            raise ValueError("priority must be between -10000 and 10000")
        return ProposedRule(
            merchant_contains=merchant,
            maximum_amount_minor=maximum_amount_minor,
            currency=currency,
            target_classification=target_classification,
            target_category=category,
            effective_from=effective_from,
            priority=priority,
        )

    def _active_rules(self, workspace_id: str) -> list[ClassificationRule]:
        rows = self.store.fetch_all(
            """
            SELECT * FROM classification_rules
            WHERE workspace_id = ? AND active = 1
            ORDER BY priority DESC, rule_id
            """,
            (workspace_id,),
        )
        return [self.engine._rule_from_row(row) for row in rows]

    def _transactions(self, workspace_id: str) -> list[Transaction]:
        rows = self.store.fetch_all(
            """
            SELECT * FROM transactions
            WHERE workspace_id = ?
            ORDER BY occurred_on, transaction_id
            """,
            (workspace_id,),
        )
        return [self._transaction(row) for row in rows]

    def _period_status(self, workspace_id: str, occurred_on: str) -> str | None:
        exists = self.store.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounting_period_revisions'"
        )
        if exists is None:
            return None
        row = self.store.fetch_one(
            """
            SELECT p.status FROM accounting_period_revisions p
            WHERE p.workspace_id = ? AND p.period_start <= ? AND p.period_end >= ?
              AND p.revision = (
                SELECT MAX(p2.revision) FROM accounting_period_revisions p2
                WHERE p2.period_id = p.period_id
              )
            ORDER BY CASE p.status WHEN 'hard_locked' THEN 3 WHEN 'soft_locked' THEN 2 ELSE 1 END DESC
            LIMIT 1
            """,
            (workspace_id, occurred_on, occurred_on),
        )
        return str(row["status"]) if row else None

    @staticmethod
    def _conflicts(proposed: ProposedRule, active: list[ClassificationRule]) -> list[dict[str, object]]:
        merchant = _normalise(proposed.merchant_contains)
        conflicts: list[dict[str, object]] = []
        for rule in active:
            existing = _normalise(rule.merchant_contains)
            merchant_overlap = merchant in existing or existing in merchant
            amount_overlap = min(proposed.maximum_amount_minor, rule.maximum_amount_minor) >= 0
            date_overlap = True
            if not (merchant_overlap and amount_overlap and date_overlap and proposed.currency == rule.currency):
                continue
            outcome_differs = (
                proposed.target_classification != rule.target_classification
                or proposed.target_category != rule.target_category
            )
            conflicts.append(
                {
                    "ruleId": rule.rule_id,
                    "merchantContains": rule.merchant_contains,
                    "maximumAmountMinor": rule.maximum_amount_minor,
                    "targetClassification": rule.target_classification,
                    "targetCategory": rule.target_category,
                    "priority": rule.priority,
                    "sameOutcome": not outcome_differs,
                    "winnerIfBothMatch": (
                        "proposed"
                        if proposed.priority > rule.priority
                        else "existing"
                        if proposed.priority < rule.priority
                        else min("preview", rule.rule_id)
                    ),
                }
            )
        return conflicts

    def preview(
        self,
        *,
        workspace_id: str,
        merchant_contains: str,
        maximum_amount_minor: int,
        currency: str,
        target_classification: str,
        target_category: str | None,
        effective_from: str,
        priority: int,
    ) -> dict[str, object]:
        proposed = self._validate(
            merchant_contains=merchant_contains,
            maximum_amount_minor=maximum_amount_minor,
            currency=currency,
            target_classification=target_classification,
            target_category=target_category,
            effective_from=effective_from,
            priority=priority,
        )
        proposed_rule = proposed.as_domain()
        active = self._active_rules(workspace_id)
        matches: list[dict[str, object]] = []
        for transaction in self._transactions(workspace_id):
            if not rule_matches(proposed_rule, transaction):
                continue
            current = classification_for(transaction, active)
            proposed_decision = classification_for(
                transaction,
                [*active, proposed_rule],
            )
            changes = (
                current.classification != proposed_decision.classification
                or current.category != proposed_decision.category
                or current.rule_id != proposed_decision.rule_id
            )
            matches.append(
                {
                    "transactionId": transaction.transaction_id,
                    "occurredOn": transaction.occurred_on,
                    "description": transaction.description,
                    "amountMinor": transaction.amount_minor,
                    "currency": transaction.currency,
                    "before": {
                        "classification": current.classification,
                        "category": current.category,
                        "ruleId": current.rule_id,
                    },
                    "after": {
                        "classification": proposed_decision.classification,
                        "category": proposed_decision.category,
                        "ruleId": proposed_decision.rule_id,
                    },
                    "wouldChange": changes,
                    "periodStatus": self._period_status(workspace_id, transaction.occurred_on),
                    "evidenceIds": [transaction.evidence_id],
                }
            )
        conflicts = self._conflicts(proposed, active)
        evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for match in matches
                for evidence_id in match["evidenceIds"]
            )
        )
        return {
            "previewVersion": "folio.classification-rule-preview@1",
            "workspaceId": workspace_id,
            "proposedRule": {
                "merchantContains": proposed.merchant_contains,
                "maximumAmountMinor": proposed.maximum_amount_minor,
                "currency": proposed.currency,
                "targetClassification": proposed.target_classification,
                "targetCategory": proposed.target_category,
                "effectiveFrom": proposed.effective_from,
                "priority": proposed.priority,
            },
            "matches": matches,
            "matchCount": len(matches),
            "changeCount": sum(bool(match["wouldChange"]) for match in matches),
            "hardLockedMatchCount": sum(match["periodStatus"] == "hard_locked" for match in matches),
            "conflicts": conflicts,
            "conflictCount": len(conflicts),
            "evidenceIds": evidence_ids,
            "committed": False,
        }

    def list_rules(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        transactions = self._transactions(workspace_id)
        rows = self.store.fetch_all(
            """
            SELECT * FROM classification_rules
            WHERE workspace_id = ? ORDER BY active DESC, priority DESC, rule_id
            """,
            (workspace_id,),
        )
        values: list[dict[str, object]] = []
        for row in rows:
            rule = self.engine._rule_from_row(row)
            creation = self.store.fetch_one(
                """
                SELECT e.event_id, e.occurred_at, e.source_turn_id
                FROM event_effects x
                JOIN finance_events e ON e.event_id = x.event_id
                WHERE e.workspace_id = ? AND x.target_type = 'classification_rule'
                  AND x.target_id = ?
                ORDER BY e.occurred_at, e.event_id LIMIT 1
                """,
                (workspace_id, rule.rule_id),
            )
            matched = [transaction for transaction in transactions if rule_matches(rule, transaction)]
            values.append(
                {
                    "ruleId": rule.rule_id,
                    "merchantContains": rule.merchant_contains,
                    "maximumAmountMinor": rule.maximum_amount_minor,
                    "currency": rule.currency,
                    "targetClassification": rule.target_classification,
                    "targetCategory": rule.target_category,
                    "effectiveFrom": rule.effective_from,
                    "priority": rule.priority,
                    "active": bool(row["active"]),
                    "sourceTurnId": str(row["source_turn_id"]) if row["source_turn_id"] else None,
                    "sourceClaimId": str(row["source_claim_id"]) if row["source_claim_id"] else None,
                    "creationEventId": str(creation["event_id"]) if creation else None,
                    "createdAt": str(row["created_at"]),
                    "updatedAt": str(row["updated_at"]),
                    "currentMatchCount": len(matched),
                    "currentMatchedAmountMinor": sum(transaction.amount_minor for transaction in matched),
                    "evidenceIds": list(dict.fromkeys(transaction.evidence_id for transaction in matched)),
                }
            )
        return tuple(values)

    def deactivate(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        request_id: str,
        reason: str,
    ) -> dict[str, object]:
        row = self.store.fetch_one(
            "SELECT active FROM classification_rules WHERE workspace_id = ? AND rule_id = ?",
            (workspace_id, rule_id),
        )
        if row is None:
            raise KeyError(rule_id)
        if not bool(row["active"]):
            return {
                "ruleId": rule_id,
                "status": "inactive",
                "idempotentReplay": True,
            }
        creation = self.store.fetch_one(
            """
            SELECT e.event_id
            FROM event_effects x
            JOIN finance_events e ON e.event_id = x.event_id
            WHERE e.workspace_id = ? AND x.target_type = 'classification_rule'
              AND x.target_id = ? AND e.undone_by_event_id IS NULL
            ORDER BY e.occurred_at, e.event_id LIMIT 1
            """,
            (workspace_id, rule_id),
        )
        if creation is None:
            raise ValueError("rule has no reversible creation event")
        result = self.engine.undo_event(
            str(creation["event_id"]),
            request_id=request_id,
            reason=reason,
        )
        return {
            "ruleId": rule_id,
            "status": "inactive",
            "undoneCreationEventId": str(creation["event_id"]),
            **result,
        }
'''

SERVICE_METHODS = '''    async def preview_classification_rule(
        self,
        *,
        workspace_id: str,
        merchant_contains: str,
        maximum_amount_minor: int,
        currency: str,
        target_classification: str,
        target_category: str | None,
        effective_from: str,
        priority: int,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return ClassificationRuleManagementService(
            self.store, self.engine
        ).preview(
            workspace_id=workspace_id,
            merchant_contains=merchant_contains,
            maximum_amount_minor=maximum_amount_minor,
            currency=currency,
            target_classification=target_classification,
            target_category=target_category,
            effective_from=effective_from,
            priority=priority,
        )

    async def list_classification_rules(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return ClassificationRuleManagementService(
            self.store, self.engine
        ).list_rules(workspace_id)

    async def deactivate_classification_rule(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        request_id: str,
        reason: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = ClassificationRuleManagementService(
                self.store, self.engine
            ).deactivate(
                workspace_id=workspace_id,
                rule_id=rule_id,
                request_id=request_id,
                reason=reason,
            )
            self.working_understanding.ensure_current(workspace_id=workspace_id)
        return value
'''

ROUTE_MODELS = '''

class ClassificationRulePreviewRequest(RequestModel):
    merchant_contains: str = Field(alias="merchantContains", min_length=1, max_length=200)
    maximum_amount_minor: int = Field(alias="maximumAmountMinor", ge=0)
    currency: str = Field(default="NZD", pattern=r"^NZD$")
    target_classification: str = Field(
        alias="targetClassification",
        pattern=r"^(business|personal|unresolved)$",
    )
    target_category: str | None = Field(
        default=None, alias="targetCategory", max_length=200
    )
    effective_from: date = Field(alias="effectiveFrom")
    priority: int = Field(default=100, ge=-10000, le=10000)


class ClassificationRuleDeactivateRequest(RequestModel):
    request_id: str = Field(alias="requestId", pattern=IDENTIFIER_PATTERN)
    reason: str = Field(min_length=1, max_length=500)
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/classification-rules/preview")
    async def preview_classification_rule(
        workspace_id: PathIdentifier,
        body: ClassificationRulePreviewRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.preview_classification_rule(
                    workspace_id=workspace_id,
                    merchant_contains=body.merchant_contains,
                    maximum_amount_minor=body.maximum_amount_minor,
                    currency=body.currency,
                    target_classification=body.target_classification,
                    target_category=body.target_category,
                    effective_from=body.effective_from.isoformat(),
                    priority=body.priority,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/workspaces/{workspace_id}/classification-rules")
    async def list_classification_rules(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        rules = await services.list_classification_rules(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "rules": list(rules)}

    @router.post(
        "/v1/workspaces/{workspace_id}/classification-rules/{rule_id}/deactivate"
    )
    async def deactivate_classification_rule(
        workspace_id: PathIdentifier,
        rule_id: PathIdentifier,
        body: ClassificationRuleDeactivateRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.deactivate_classification_rule(
                    workspace_id=workspace_id,
                    rule_id=rule_id,
                    request_id=body.request_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="classification rule not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.rule_management import ClassificationRuleManagementService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    return store, engine, ClassificationRuleManagementService(store, engine)


def preview(service: ClassificationRuleManagementService):
    return service.preview(
        workspace_id="ws_koru_studio",
        merchant_contains="MITRE 10",
        maximum_amount_minor=50000,
        currency="NZD",
        target_classification="business",
        target_category="client_fit_out_materials",
        effective_from="2026-07-01",
        priority=100,
    )


def test_preview_is_non_mutating_and_evidence_linked(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    before_rules = len(store.fetch_all("SELECT * FROM classification_rules"))
    before_events = len(store.fetch_all("SELECT * FROM finance_events"))
    value = preview(service)
    assert value["committed"] is False
    assert value["matchCount"] == 1
    assert value["changeCount"] == 1
    match = value["matches"][0]
    assert match["transactionId"] == "txn_koru_006"
    assert match["before"]["classification"] == "unresolved"
    assert match["after"]["classification"] == "business"
    assert match["evidenceIds"] == ["evd_koru_mitre10_row"]
    assert len(store.fetch_all("SELECT * FROM classification_rules")) == before_rules
    assert len(store.fetch_all("SELECT * FROM finance_events")) == before_events


def test_preview_reports_overlap_and_priority_after_existing_rule(tmp_path: Path) -> None:
    _store, engine, service = setup(tmp_path)
    engine.create_classification_rule(
        merchant_contains="MITRE 10",
        maximum_amount_minor=60000,
        target_classification="personal",
        target_category="personal_materials",
        effective_from="2026-07-01",
        source_turn_id="turn_rule_conflict",
        owner_statement="Treat this merchant as personal below NZD 600.",
    )
    value = preview(service)
    assert value["conflictCount"] == 1
    conflict = value["conflicts"][0]
    assert conflict["sameOutcome"] is False
    assert conflict["winnerIfBothMatch"] in {"existing", "proposed"}


def test_rule_listing_exposes_reversible_event_and_current_evidence(tmp_path: Path) -> None:
    _store, engine, service = setup(tmp_path)
    result = engine.create_classification_rule(
        merchant_contains="MITRE 10",
        maximum_amount_minor=50000,
        target_classification="business",
        target_category="client_fit_out_materials",
        effective_from="2026-07-01",
        source_turn_id="turn_rule_list",
        owner_statement="Client materials.",
    )
    rules = service.list_rules("ws_koru_studio")
    value = next(rule for rule in rules if rule["ruleId"] == result.event["scopeJson"]["ruleIds"][0])
    assert value["active"] is True
    assert value["creationEventId"]
    assert value["currentMatchCount"] == 1
    assert value["evidenceIds"] == ["evd_koru_mitre10_row"]


def test_deactivation_uses_existing_undo_event_and_is_idempotent(tmp_path: Path) -> None:
    store, engine, service = setup(tmp_path)
    result = engine.create_classification_rule(
        merchant_contains="MITRE 10",
        maximum_amount_minor=50000,
        target_classification="business",
        target_category="client_fit_out_materials",
        effective_from="2026-07-01",
        source_turn_id="turn_rule_disable",
        owner_statement="Client materials.",
    )
    rule_id = result.event["scopeJson"]["ruleIds"][0]
    value = service.deactivate(
        workspace_id="ws_koru_studio",
        rule_id=rule_id,
        request_id="undo_rule_management_001",
        reason="Owner disabled this rule after review.",
    )
    assert value["status"] == "inactive"
    rule = store.fetch_one("SELECT active FROM classification_rules WHERE rule_id = ?", (rule_id,))
    assert int(rule["active"]) == 0
    transaction = store.fetch_one(
        "SELECT classification, rule_id FROM transactions WHERE transaction_id = 'txn_koru_006'"
    )
    assert str(transaction["classification"]) == "unresolved"
    assert transaction["rule_id"] is None
    replay = service.deactivate(
        workspace_id="ws_koru_studio",
        rule_id=rule_id,
        request_id="undo_rule_management_002",
        reason="Repeated click.",
    )
    assert replay["idempotentReplay"] is True
'''


def add_module() -> None:
    write("services/api/src/finance_agent/finance/rule_management.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.accounting_periods import AccountingPeriodService\n"
    import_line = "from finance_agent.finance.rule_management import ClassificationRuleManagementService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("accounting-period import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "list_accounting_periods", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def list_accounting_periods(\n"
    addition = '''    async def preview_classification_rule(\n        self, *, workspace_id: str, merchant_contains: str,\n        maximum_amount_minor: int, currency: str,\n        target_classification: str, target_category: str | None,\n        effective_from: str, priority: int\n    ) -> Mapping[str, object]: ...\n\n    async def list_classification_rules(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def deactivate_classification_rule(\n        self, *, workspace_id: str, rule_id: str, request_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("accounting-period protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass AccountingPeriodRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("AccountingPeriodRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/accounting-periods")\n'
    if route_marker not in content:
        raise RuntimeError("accounting-period route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_classification_rule_management.py", TESTS)
    write("docs/CLASSIFICATION_RULES.md", '''# Classification rule management\n\nFolio can preview a proposed merchant rule before any write. A preview applies the exact production matcher to current transactions, returns before/after classification and category, linked evidence, period-lock state, current overlap/conflict rules and the deterministic priority winner. It explicitly reports `committed: false`.\n\nThe rule list exposes active state, current match count and amount, source turn/claim references, creation event and evidence. Deactivation does not directly edit history. It invokes the existing reversible creation event, so affected transactions, rule state, artefacts and receipts follow the same Undo path as the original correction. Repeating deactivation is an idempotent no-op.\n\nA preview indicates impact, not accounting correctness. Hard-locked transactions remain protected by accounting-period triggers, and a conflicting rule requires owner judgement rather than a hidden model choice.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 37: classification rule preview and lifecycle management\n\n- Proposed rules dry-run against the production matcher without writes.\n- Before/after meaning, exact amounts, evidence and lock state remain visible.\n- Overlap, conflicting outcomes and deterministic priority winners are reported.\n- Rule inventory links matches to source claims and reversible creation events.\n- Deactivation reuses the existing Undo event rather than rewriting history.\n- Preview impact is not treated as accounting correctness or owner approval.\n'''
    if "## Stack 37: classification rule preview and lifecycle management" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_module()
    update_service_protocol_routes()
    tests_docs()
    print("classification rule management changes applied")


if __name__ == "__main__":
    main()
