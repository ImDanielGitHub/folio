"""Product-owned bounded controller for one continuing finance thread."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from finance_agent.agent.business_discovery import decide_business_discovery
from finance_agent.agent.catalogue import ControllerState, validate_plan_for_intent
from finance_agent.agent.dialogue import (
    ActiveQuestion,
    ClaimBasis,
    ClaimScope,
    ClaimStatus,
    ContextAssembler,
    ConversationStore,
    InquiryPolicy,
    TemporalClaim,
    TranscriptTurn,
    WorkingUnderstandingPort,
)
from finance_agent.agent.executor import ExecutionReceipt, FinancePlanExecutor
from finance_agent.agent.harness import HarnessRequest, ModelHarness
from finance_agent.agent.narrative import compose_execution_narrative
from finance_agent.agent.plan import (
    CreateClassificationRuleAction,
    QuerySummaryAction,
    QueryTransactionsAction,
    RecordBusinessClaimAction,
)
from finance_agent.agent.ports import FinanceCorePort
from finance_agent.models.base import ModelMode
from finance_agent.models.projection import EgressReceipt, ModelReceipt, receipt_id


@dataclass(frozen=True, slots=True)
class TurnRequest:
    workspace_id: str
    thread_id: str
    run_id: str
    turn_id: str
    content: str
    mode: ModelMode


@dataclass(frozen=True, slots=True)
class WorkReceipt:
    receipt_id: str
    run_id: str
    content_hash: str
    evidence_ids: tuple[str, ...]
    status: str


class ReceiptSink(Protocol):
    async def commit(self, receipt: WorkReceipt) -> None: ...


@dataclass(slots=True)
class InMemoryReceiptSink:
    receipts: list[WorkReceipt] = field(default_factory=list)

    async def commit(self, receipt: WorkReceipt) -> None:
        self.receipts.append(receipt)


@dataclass(frozen=True, slots=True)
class ControllerResult:
    run_id: str
    narrative: str
    question: str | None
    plan_source: str
    state_trace: tuple[ControllerState, ...]
    execution: ExecutionReceipt | None
    model_receipts: tuple[ModelReceipt, ...]
    egress_receipts: tuple[EgressReceipt, ...]
    work_receipt: WorkReceipt


class FinanceAgentController:
    def __init__(
        self,
        *,
        finance_core: FinanceCorePort,
        conversations: ConversationStore,
        harness: ModelHarness,
        receipt_sink: ReceiptSink,
        inquiry: InquiryPolicy | None = None,
        context_assembler: ContextAssembler | None = None,
        working_understanding: WorkingUnderstandingPort | None = None,
    ) -> None:
        self.finance_core = finance_core
        self.conversations = conversations
        self.harness = harness
        self.receipt_sink = receipt_sink
        self.inquiry = inquiry or InquiryPolicy()
        self.context_assembler = context_assembler or ContextAssembler()
        self.working_understanding = working_understanding
        self.executor = FinancePlanExecutor(finance_core)

    async def run_turn(self, request: TurnRequest) -> ControllerResult:
        trace: list[ControllerState] = [ControllerState.LOAD_CONTEXT]
        context = await self.finance_core.load_context(request.workspace_id, request.thread_id)
        frame = self.conversations.get_frame(request.thread_id)
        frame_for_context = frame
        owner_turn = TranscriptTurn(
            turn_id=request.turn_id,
            role="owner",
            content=request.content,
            occurred_at=datetime.now(UTC),
            mode=request.mode.value,
        )
        self.conversations.append_turn(request.thread_id, owner_turn)
        if self.working_understanding is not None:
            self.working_understanding.ingest_owner_turn(
                workspace_id=request.workspace_id,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
                content=request.content,
                occurred_at=owner_turn.occurred_at,
            )
        had_active_question = frame.active_question is not None
        if self.inquiry.is_stop(request.content):
            frame = self.inquiry.stop(frame)
        elif had_active_question:
            frame = replace(
                frame,
                active_question=None,
                stopped=False,
                updated_at=owner_turn.occurred_at,
            )
        recent = self.conversations.recent_turns(request.thread_id, 4)
        working_understanding = (
            self.working_understanding.context_for(
                workspace_id=request.workspace_id,
                thread_id=request.thread_id,
                run_id=request.run_id,
                query=request.content,
                max_characters=min(1800, self.context_assembler.max_characters // 3),
            )
            if self.working_understanding is not None
            else None
        )
        context_packet = self.context_assembler.assemble(
            frame_for_context,
            recent,
            dict(context.projection),
            working_understanding=working_understanding,
        )
        self.conversations.save_frame(frame)

        trace.append(ControllerState.COMPILE_PLAN)
        outcome = await self.harness.compile_plan(
            HarnessRequest(
                workspace_id=request.workspace_id,
                thread_id=request.thread_id,
                run_id=request.run_id,
                turn_id=request.turn_id,
                content=request.content,
                mode=request.mode,
                context_packet=context_packet,
                finance_context=context,
            )
        )
        trace.append(ControllerState.VALIDATE_PLAN)
        if outcome.plan is None:
            trace.extend([ControllerState.EXECUTE_READS, ControllerState.ASK_ONE_QUESTION])
            prompt = outcome.question or "What detail should I use to continue?"
            question = ActiveQuestion(
                question_id=receipt_id("question", request.run_id, prompt),
                prompt=prompt,
                reason="One missing scope value prevents safe execution.",
                asked_at=datetime.now(UTC),
            )
            frame = self.inquiry.ask(frame, question)
            self.conversations.save_frame(frame)
            narrative = f"{self.inquiry.acknowledge(request.content)} {prompt}"
            self.conversations.append_turn(
                request.thread_id,
                TranscriptTurn(
                    turn_id=receipt_id("turn", request.run_id, "question"),
                    role="agent",
                    content=narrative,
                    occurred_at=datetime.now(UTC),
                    mode=request.mode.value,
                ),
            )
            trace.extend([ControllerState.EXPLAIN, ControllerState.COMMIT_RECEIPT])
            work_receipt = self._work_receipt(request.run_id, (), trace, status="question")
            await self.receipt_sink.commit(work_receipt)
            return ControllerResult(
                run_id=request.run_id,
                narrative=narrative,
                question=prompt,
                plan_source=outcome.source,
                state_trace=tuple(trace),
                execution=None,
                model_receipts=outcome.model_receipts,
                egress_receipts=outcome.egress_receipts,
                work_receipt=work_receipt,
            )

        plan = validate_plan_for_intent(
            outcome.plan,
            intent_class=outcome.intent_class,
            thread_id=request.thread_id,
            run_id=request.run_id,
        )
        execution = await self.executor.execute(plan, source_turn_id=request.turn_id)
        trace.extend(execution.state_trace)
        self._commit_explicit_claims(frame, plan, request.turn_id)

        write_kinds = {
            "create_classification_rule",
            "record_business_claim",
            "undo_event",
        }
        has_committed_write = any(
            result.kind in write_kinds and result.status in {"completed", "no_op"}
            for result in execution.results
        )
        plan_was_read = any(
            isinstance(action, (QuerySummaryAction, QueryTransactionsAction))
            for action in plan.actions
        )

        trace.append(ControllerState.EXPLAIN)
        if self.inquiry.is_stop(request.content):
            narrative = self.inquiry.synthesise()
            narrative_receipt = None
            narrative_egress = None
        else:
            deterministic_narrative = compose_execution_narrative(
                plan=plan,
                execution=execution,
            )
            # Prefer evidence-backed wording for writes and deterministic_fallback —
            # judges must see what changed, not a generic acknowledgement.
            if outcome.source == "deterministic_fallback" or has_committed_write:
                narrative = deterministic_narrative
                narrative_receipt = None
                narrative_egress = None
            else:
                refreshed = await self.finance_core.load_context(
                    request.workspace_id, request.thread_id
                )
                narrative_outcome = await self.harness.explain(
                    workspace_id=request.workspace_id,
                    thread_id=request.thread_id,
                    run_id=request.run_id,
                    mode=request.mode,
                    source=dict(refreshed.projection),
                    fallback_text=deterministic_narrative,
                )
                narrative = narrative_outcome.text
                narrative_receipt = narrative_outcome.model_receipt
                narrative_egress = narrative_outcome.egress_receipt

        discovery_question: str | None = None
        if not self.inquiry.is_stop(request.content) and frame.active_question is None:
            asked_prompts = tuple(
                turn.content
                for turn in self.conversations.recent_turns(request.thread_id, 12)
                if turn.role == "agent"
            )
            discovery = decide_business_discovery(
                content=request.content,
                working_understanding=working_understanding,
                has_active_question=False,
                had_committed_write=has_committed_write,
                plan_was_read=plan_was_read,
                asked_at=datetime.now(UTC),
                question_id=receipt_id("question", request.run_id, "discovery"),
                asked_prompts=asked_prompts,
                just_answered_question=had_active_question,
            )
            if discovery.question is not None:
                trace.append(ControllerState.ASK_ONE_QUESTION)
                frame = self.inquiry.ask(frame, discovery.question)
                self.conversations.save_frame(frame)
                discovery_question = discovery.question.prompt
                narrative = f"{narrative.rstrip()} {discovery.question.prompt}"

        model_receipts = outcome.model_receipts + (
            (narrative_receipt,) if narrative_receipt else ()
        )
        egress_receipts = outcome.egress_receipts + (
            (narrative_egress,) if narrative_egress else ()
        )
        trace.append(ControllerState.COMMIT_RECEIPT)
        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id for result in execution.results for evidence_id in result.evidence_ids
            )
        )
        work_receipt = self._work_receipt(
            request.run_id,
            evidence_ids,
            trace,
            status="question" if discovery_question else "completed",
        )
        await self.receipt_sink.commit(work_receipt)
        agent_turn = TranscriptTurn(
            turn_id=receipt_id("turn", request.run_id, "agent"),
            role="agent",
            content=narrative,
            occurred_at=datetime.now(UTC),
            mode=request.mode.value,
        )
        self.conversations.append_turn(request.thread_id, agent_turn)
        return ControllerResult(
            run_id=request.run_id,
            narrative=narrative,
            question=discovery_question,
            plan_source=outcome.source,
            state_trace=tuple(trace),
            execution=execution,
            model_receipts=model_receipts,
            egress_receipts=egress_receipts,
            work_receipt=work_receipt,
        )

    def _commit_explicit_claims(self, frame: object, plan: object, source_turn_id: str) -> None:
        from finance_agent.agent.dialogue import DialogueFrame
        from finance_agent.agent.plan import FinancePlan

        if not isinstance(frame, DialogueFrame) or not isinstance(plan, FinancePlan):
            raise TypeError("internal frame/plan type mismatch")
        rule = next(
            (
                action
                for action in plan.actions
                if isinstance(action, CreateClassificationRuleAction)
            ),
            None,
        )
        updated = frame
        for action in plan.actions:
            if not isinstance(action, RecordBusinessClaimAction):
                continue
            supersedes = next(
                (
                    claim.claim_id
                    for claim in reversed(updated.claims)
                    if claim.claim_type == action.claim_type and claim.status is ClaimStatus.ACTIVE
                ),
                None,
            )
            claim = TemporalClaim(
                claim_id=receipt_id("claim", source_turn_id, action.action_id),
                claim_type=action.claim_type,
                statement=action.statement,
                source_turn_id=source_turn_id,
                scope=ClaimScope(
                    merchant_contains=rule.merchant_contains if rule else None,
                    maximum_amount_minor=rule.maximum_amount_minor if rule else None,
                    currency=rule.currency if rule else None,
                    workspace_id=updated.workspace_id,
                ),
                basis=ClaimBasis.EXPLICIT,
                effective_from=action.effective_date,
                effective_until=None,
                confidence=1.0,
                recorded_at=datetime.now(UTC),
                supersedes_claim_id=supersedes,
            )
            updated = updated.with_claim(claim)
        self.conversations.save_frame(updated)

    @staticmethod
    def _work_receipt(
        run_id: str,
        evidence_ids: tuple[str, ...],
        trace: list[ControllerState],
        *,
        status: str,
    ) -> WorkReceipt:
        payload = json.dumps(
            {
                "runId": run_id,
                "status": status,
                "trace": [state.value for state in trace],
                "evidenceIds": evidence_ids,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return WorkReceipt(
            receipt_id=receipt_id("agentrcpt", run_id, status),
            run_id=run_id,
            content_hash=hashlib.sha256(payload.encode()).hexdigest(),
            evidence_ids=evidence_ids,
            status=status,
        )
