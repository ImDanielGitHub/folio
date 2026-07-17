"""Closed-reference compiler and fail-closed validator for model-written finance prose.

The model may improve wording, but it never becomes an authority for money, evidence,
committed work, tax, investments, or payments.  This module turns deterministic source
data into a small set of human-readable references and validates every model narrative
against those references before the prose can reach the conversation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

_NUMBER_RE = re.compile(
    r"(?<![\w])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?",
    re.IGNORECASE,
)
_FINANCE_AMOUNT_RE = re.compile(
    r"(?:\b[A-Z]{3}\s*|[$£€]\s*)[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"|[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*[A-Z]{3}\b",
    re.IGNORECASE,
)
_SPELLED_FINANCE_AMOUNT_RE = re.compile(
    r"(?:[$£€]\s*)?\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)"
    r"(?:[\s-]+(?:and|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)){0,6}"
    r"[\s-]+(?:dollars?|pounds?|euros?|nzd|aud|usd|gbp|eur)\b",
    re.IGNORECASE,
)
_RAW_IDENTIFIER_RE = re.compile(
    r"\b(?:evd|src|artifact|receipt|txn|transaction|run|event)_[a-z0-9_-]+\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z][a-z0-9'-]*", re.IGNORECASE)
_HUMAN_EVIDENCE_RE = re.compile(r"^[\w][\w .,'()&/\-]{2,119}$", re.UNICODE)
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_PROVENANCE_CUE_RE = re.compile(
    r"\b(?:according to|based on|evidenced by|verified by|source(?:s|-linked)?|"
    r"bank (?:statement|export|feed)|(?:receipt|invoice) (?:shows|states|records)|"
    r"(?:shown|recorded) (?:on|in) (?:the )?(?:receipt|invoice))\b",
    re.IGNORECASE,
)
_GENERIC_PROVENANCE_RE = re.compile(
    r"\b(?:linked sources?|source-linked figures?|provided data|uploaded data)\b",
    re.IGNORECASE,
)

_PROHIBITED_DIRECTIVE_RES = (
    re.compile(
        r"(?:^|[.!?]\s+)(?:please\s+)?(?:pay|send|transfer|wire|remit)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:you|we)\s+(?:should|must|need to|ought to|can|could|may)\s+"
        r"(?:pay|send|transfer|wire|remit|file|lodge|submit|claim|deduct|buy|sell|"
        r"invest|rebalance)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|folio)\s+(?:recommend|advise|suggest)\s+(?:that\s+you\s+)?"
        r"(?:pay|send|transfer|wire|remit|file|lodge|submit|claim|deduct|buy|sell|"
        r"invest|rebalance)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:claim(?:able)?|deductible)\s+(?:for|against|as)\s+(?:tax|gst)|"
        r"\b(?:tax|gst)\s+(?:deductible|claimable)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[.!?]\s+)(?:please\s+)?(?:file|lodge|submit|claim|deduct|buy|sell|"
        r"invest|rebalance)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bconsider\s+(?:paying|sending|transferring|filing|lodging|claiming|deducting|"
        r"buying|selling|investing|rebalancing)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|folio)\s+(?:can|could|will)\s+(?:pay|send|transfer|wire|remit|file|"
        r"lodge|submit|claim|deduct|buy|sell|invest|rebalance)\b",
        re.IGNORECASE,
    ),
)

_CALCULATION_CUE_RE = re.compile(
    r"\b(?:approximately|about|around|roughly|estimate(?:d)?|double|twice|half|"
    r"plus|minus|added|subtracted|more than|less than|at least|at most|not)\b",
    re.IGNORECASE,
)

_ACTION_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    "paid": re.compile(r"\b(?:paid|payment (?:was|has been) made|settled)\b", re.I),
    "sent": re.compile(r"\b(?:sent|emailed|delivered|remitted)\b", re.I),
    "filed": re.compile(r"\b(?:filed|lodged|submitted)\b", re.I),
    "verified": re.compile(r"\b(?:verified|confirmed by|validated)\b", re.I),
    "reconciled": re.compile(r"\b(?:reconciled|matched to the ledger)\b", re.I),
    "transferred": re.compile(r"\b(?:transferred|wired)\b", re.I),
    "approved": re.compile(r"\b(?:approved|authorised|authorized)\b", re.I),
    "posted": re.compile(r"\b(?:posted to|booked to|journalled)\b", re.I),
}

_LABEL_SYNONYMS: dict[str, frozenset[str]] = {
    "balance": frozenset({"cash"}),
    "expense": frozenset({"spend", "spending", "cost", "costs", "outgoing", "outgoings"}),
    "income": frozenset({"revenue", "earned", "earnings", "inflow", "inflows"}),
    "personal": frozenset({"private"}),
    "projected": frozenset({"forecast", "forecasted"}),
    "reserve": frozenset({"buffer"}),
    "shortfall": frozenset({"gap", "below", "under"}),
    "unresolved": frozenset({"uncategorised", "uncategorized", "unclassified"}),
}
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "minor",
        "of",
        "on",
        "or",
        "the",
        "to",
        "total",
        "value",
        "was",
        "were",
        "with",
    }
)
_OWNER_ATTRIBUTION_RE = re.compile(
    r"\b(?:you said|you told me|you described|your (?:note|message|explanation))\b",
    re.IGNORECASE,
)
_COMMITTED_STATUSES = frozenset(
    {"committed", "completed", "filed", "paid", "posted", "reconciled", "sent", "verified"}
)
_MAX_FACT_REFERENCES = 64
_MAX_FACT_CHARACTERS = 600


class NarrativeRejection(StrEnum):
    UNSUPPORTED_NUMBER = "unsupported_number"
    NON_CANONICAL_AMOUNT = "non_canonical_amount"
    AMOUNT_LABEL_MISMATCH = "amount_label_mismatch"
    UNSUPPORTED_ACTION_CLAIM = "unsupported_action_claim"
    INVENTED_PROVENANCE = "invented_provenance"
    RAW_INTERNAL_IDENTIFIER = "raw_internal_identifier"
    PROHIBITED_DIRECTIVE = "prohibited_directive"
    UNSUPPORTED_CALCULATION = "unsupported_calculation"


@dataclass(frozen=True, slots=True)
class AmountReference:
    reference_id: str
    path: str
    label: str
    formatted_value: str
    numeric_token: str
    label_tokens: frozenset[str]

    def as_prompt_value(self) -> dict[str, str]:
        return {
            "referenceId": self.reference_id,
            "label": self.label,
            "formattedValue": self.formatted_value,
        }


@dataclass(frozen=True, slots=True)
class FactReference:
    reference_id: str
    kind: str
    text: str
    committed: bool = False

    @property
    def number_tokens(self) -> frozenset[str]:
        return frozenset(
            _normalise_number(match.group()) for match in _NUMBER_RE.finditer(self.text)
        )

    @property
    def word_tokens(self) -> frozenset[str]:
        return _meaningful_tokens(self.text)

    def as_prompt_value(self) -> dict[str, str]:
        return {
            "referenceId": self.reference_id,
            "kind": self.kind,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class NarrativeReferencePacket:
    amount_references: tuple[AmountReference, ...]
    fact_references: tuple[FactReference, ...]
    evidence_labels: tuple[str, ...]
    has_evidence: bool

    def as_prompt_value(self) -> dict[str, object]:
        return {
            "referenceVersion": "narrative.references@1",
            "amountReferences": [item.as_prompt_value() for item in self.amount_references],
            "factReferences": [item.as_prompt_value() for item in self.fact_references],
            "allowedGenericProvenance": ["linked sources"] if self.has_evidence else [],
        }


@dataclass(frozen=True, slots=True)
class NarrativeValidation:
    accepted: bool
    rejections: tuple[NarrativeRejection, ...]
    used_reference_ids: tuple[str, ...]
    contains_finance_amounts: bool


class NarrativeGuard:
    """Compile a minimal model prompt and reject prose not grounded in it."""

    def compile_references(self, source: Mapping[str, object]) -> NarrativeReferencePacket:
        aggregate = source.get("aggregate_amounts", {})
        amount_references = tuple(self._amount_references(aggregate))
        fact_references = tuple(self._fact_references(source))
        raw_evidence = source.get("evidence_labels", [])
        evidence_values = tuple(self._string_values(raw_evidence))
        evidence_labels = tuple(
            value
            for value in evidence_values
            if _HUMAN_EVIDENCE_RE.fullmatch(value) and not _RAW_IDENTIFIER_RE.search(value)
        )
        return NarrativeReferencePacket(
            amount_references=amount_references,
            fact_references=fact_references,
            evidence_labels=evidence_labels,
            has_evidence=bool(evidence_values),
        )

    def validate(
        self,
        text: str,
        references: NarrativeReferencePacket,
    ) -> NarrativeValidation:
        rejections: set[NarrativeRejection] = set()
        used: set[str] = set()
        contains_amounts = bool(
            _FINANCE_AMOUNT_RE.search(text) or _SPELLED_FINANCE_AMOUNT_RE.search(text)
        )

        has_raw_reference = (
            _RAW_IDENTIFIER_RE.search(text)
            or "http://" in text.lower()
            or "https://" in text.lower()
        )
        if has_raw_reference:
            rejections.add(NarrativeRejection.RAW_INTERNAL_IDENTIFIER)
        if any(pattern.search(text) for pattern in _PROHIBITED_DIRECTIVE_RES):
            rejections.add(NarrativeRejection.PROHIBITED_DIRECTIVE)
        if _SPELLED_FINANCE_AMOUNT_RE.search(text):
            rejections.add(NarrativeRejection.UNSUPPORTED_NUMBER)

        self._validate_action_claims(text, references, rejections, used)
        self._validate_provenance(text, references, rejections, used)

        for sentence in _sentences(text):
            for match in _NUMBER_RE.finditer(sentence):
                token = _normalise_number(match.group())
                matching_amounts = tuple(
                    item for item in references.amount_references if item.numeric_token == token
                )
                if matching_amounts:
                    contains_amounts = True
                    if _CALCULATION_CUE_RE.search(sentence):
                        rejections.add(NarrativeRejection.UNSUPPORTED_CALCULATION)
                    exact = tuple(
                        item
                        for item in matching_amounts
                        if item.formatted_value.casefold() in sentence.casefold()
                    )
                    if not exact:
                        rejections.add(NarrativeRejection.NON_CANONICAL_AMOUNT)
                        continue
                    label_bound = tuple(
                        item for item in exact if item.label_tokens & _meaningful_tokens(sentence)
                    )
                    if not label_bound:
                        rejections.add(NarrativeRejection.AMOUNT_LABEL_MISMATCH)
                        continue
                    used.update(item.reference_id for item in label_bound)
                    continue

                matching_facts = tuple(
                    fact
                    for fact in references.fact_references
                    if token in fact.number_tokens and self._fact_is_bound(sentence, fact)
                )
                if not matching_facts:
                    rejections.add(NarrativeRejection.UNSUPPORTED_NUMBER)
                    continue
                used.update(fact.reference_id for fact in matching_facts)

        return NarrativeValidation(
            accepted=not rejections,
            rejections=tuple(sorted(rejections, key=str)),
            used_reference_ids=tuple(sorted(used)),
            contains_finance_amounts=contains_amounts,
        )

    def _amount_references(self, aggregate: object) -> Iterable[AmountReference]:
        yield from self._walk_amounts(aggregate, path=("aggregate_amounts",), currency=None)

    def _walk_amounts(
        self,
        value: object,
        *,
        path: tuple[str, ...],
        currency: str | None,
    ) -> Iterable[AmountReference]:
        if isinstance(value, Mapping):
            local_currency = currency
            for key, child in value.items():
                if _normalise_key(key) == "currency" and isinstance(child, str):
                    candidate = child.upper()
                    if _CURRENCY_RE.fullmatch(candidate):
                        local_currency = candidate
            for key, child in value.items():
                if _normalise_key(key) in {"currency", "asof", "datathrough"}:
                    continue
                yield from self._walk_amounts(
                    child,
                    path=(*path, str(key)),
                    currency=local_currency,
                )
            return
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for index, child in enumerate(value):
                yield from self._walk_amounts(
                    child,
                    path=(*path, str(index)),
                    currency=currency,
                )
            return
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            return

        raw_key = path[-1]
        label = _humanise_label(raw_key)
        numeric = Decimal(str(value))
        if _normalise_key(raw_key).endswith("minor"):
            numeric /= Decimal(100)
            number_text = f"{numeric:,.2f}"
        else:
            number_text = format(numeric, "f")
        formatted = f"{currency} {number_text}" if currency else number_text
        path_text = ".".join(path)
        yield AmountReference(
            reference_id=_reference_id("amount", path_text, formatted),
            path=path_text,
            label=label,
            formatted_value=formatted,
            numeric_token=_normalise_number(number_text),
            label_tokens=_expanded_label_tokens(label),
        )

    def _fact_references(self, source: Mapping[str, object]) -> Iterable[FactReference]:
        count = 0
        for kind, key in (
            ("finding", "finding_labels"),
            ("forecast_assumption", "forecast_assumptions"),
            ("owner_statement", "owner_claims"),
            ("evidence", "evidence_labels"),
        ):
            for text in self._fact_texts(source.get(key), key=key):
                if count >= _MAX_FACT_REFERENCES:
                    return
                if kind == "evidence" and _RAW_IDENTIFIER_RE.search(text):
                    continue
                bounded = text[:_MAX_FACT_CHARACTERS]
                yield FactReference(
                    reference_id=_reference_id("fact", kind, bounded),
                    kind=kind,
                    text=bounded,
                )
                count += 1

        for raw in self._mapping_values(source.get("committed_actions")):
            if count >= _MAX_FACT_REFERENCES:
                return
            committed = raw.get("committed") is True
            status = str(raw.get("status", "")).casefold()
            if not committed or status not in _COMMITTED_STATUSES:
                continue
            action_text = " ".join(
                str(raw[key])
                for key in ("kind", "action", "label", "status")
                if raw.get(key) is not None
            )[:_MAX_FACT_CHARACTERS]
            if not action_text:
                continue
            yield FactReference(
                reference_id=_reference_id("fact", "committed_action", action_text),
                kind="committed_action",
                text=action_text,
                committed=True,
            )
            count += 1

    @classmethod
    def _fact_texts(cls, value: object, *, key: str) -> Iterable[str]:
        if key == "owner_claims":
            for mapping in cls._mapping_values(value):
                statement = mapping.get("statement")
                if isinstance(statement, str) and statement.strip():
                    yield statement.strip()
            return
        yield from cls._string_values(value)

    @classmethod
    def _string_values(cls, value: object) -> Iterable[str]:
        if isinstance(value, str):
            if value.strip():
                yield value.strip()
            return
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for child in value:
                yield from cls._string_values(child)

    @staticmethod
    def _mapping_values(value: object) -> Iterable[Mapping[str, object]]:
        if isinstance(value, Mapping):
            yield {str(key): child for key, child in value.items()}
            return
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for child in value:
                if isinstance(child, Mapping):
                    yield {str(key): item for key, item in child.items()}

    @staticmethod
    def _fact_is_bound(sentence: str, fact: FactReference) -> bool:
        if fact.kind == "owner_statement" and not _OWNER_ATTRIBUTION_RE.search(sentence):
            return False
        return bool(_meaningful_tokens(sentence) & fact.word_tokens)

    @staticmethod
    def _validate_action_claims(
        text: str,
        references: NarrativeReferencePacket,
        rejections: set[NarrativeRejection],
        used: set[str],
    ) -> None:
        committed = tuple(item for item in references.fact_references if item.committed)
        for action, pattern in _ACTION_CLAIM_PATTERNS.items():
            if not pattern.search(text):
                continue
            matching = tuple(item for item in committed if action in item.text.casefold())
            if not matching:
                rejections.add(NarrativeRejection.UNSUPPORTED_ACTION_CLAIM)
                continue
            used.update(item.reference_id for item in matching)

    @staticmethod
    def _validate_provenance(
        text: str,
        references: NarrativeReferencePacket,
        rejections: set[NarrativeRejection],
        used: set[str],
    ) -> None:
        if not _PROVENANCE_CUE_RE.search(text):
            return
        exact_labels = tuple(
            label for label in references.evidence_labels if label.casefold() in text.casefold()
        )
        generic = references.has_evidence and _GENERIC_PROVENANCE_RE.search(text)
        if not exact_labels and generic is None:
            rejections.add(NarrativeRejection.INVENTED_PROVENANCE)
            return
        for fact in references.fact_references:
            if fact.kind == "evidence" and fact.text in exact_labels:
                used.add(fact.reference_id)


def _reference_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]
    return f"ref_{prefix}_{digest}"


def _normalise_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _normalise_number(value: str) -> str:
    return value.replace(",", "").lstrip("+").casefold()


def _humanise_label(value: str) -> str:
    split_camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    words = split_camel.replace("_", " ").replace("-", " ").split()
    words = [word for word in words if word.casefold() != "minor"]
    return " ".join(words).strip().capitalize() or "Amount"


def _meaningful_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in (match.group().casefold() for match in _WORD_RE.finditer(value))
        if len(token) > 1 and token not in _STOP_WORDS
    )


def _expanded_label_tokens(label: str) -> frozenset[str]:
    tokens = set(_meaningful_tokens(label))
    for token in tuple(tokens):
        tokens.update(_LABEL_SYNONYMS.get(token, ()))
    return frozenset(tokens)


def _sentences(text: str) -> tuple[str, ...]:
    return tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
        if sentence.strip()
    )
