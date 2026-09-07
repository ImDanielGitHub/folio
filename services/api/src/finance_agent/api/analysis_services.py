"""Compose bounded cash analysis into the existing finance and model boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Path, Request

from finance_agent.agent.ports import FinanceContext
from finance_agent.api.http_security import IDENTIFIER_PATTERN
from finance_agent.api.services import FinanceCoreAdapter, LocalRouteServices
from finance_agent.finance.analysis import analysis_projection, load_cash_analysis


def local_today(timezone: str) -> date:
    return datetime.now(UTC).astimezone(ZoneInfo(timezone)).date()


class AnalysisFinanceCore(FinanceCoreAdapter):
    async def load_context(self, workspace_id: str, thread_id: str) -> FinanceContext:
        context = await super().load_context(workspace_id, thread_id)
        row = self.engine.store.fetch_one(
            "SELECT timezone FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        )
        if row is None:
            raise KeyError("unknown workspace")
        end = local_today(str(row["timezone"]))
        analysis = load_cash_analysis(
            self.engine.store, workspace_id=workspace_id, start=end - timedelta(days=89), end=end
        )
        projection = dict(context.projection)
        added = analysis_projection(analysis)
        aggregate = projection.get("aggregate_amounts", {})
        if not isinstance(aggregate, Mapping):
            raise ValueError("Invalid financial projection")
        projection["aggregate_amounts"] = {**dict(aggregate), **added["aggregate_amounts"]}
        existing = projection.get("finding_labels", [])
        projection["finding_labels"] = [
            *added["finding_labels"],
            *(existing if isinstance(existing, list) else []),
        ]
        return replace(context, projection=projection)


class AnalysisRouteServices(LocalRouteServices):
    def _compose_controller(self) -> None:
        self.finance_core = AnalysisFinanceCore(self.engine)
        super()._compose_controller()


def create_analysis_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/workspaces/{workspace_id}/analysis")
    async def cash_analysis(
        request: Request,
        workspace_id: Annotated[str, Path(pattern=IDENTIFIER_PATTERN)],
        start: date,
        end: date,
    ) -> dict[str, Any]:
        services: LocalRouteServices = request.app.state.finance_route_services
        try:
            return load_cash_analysis(
                services.store, workspace_id=workspace_id, start=start, end=end
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
