from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(content: str, old: str, new: str, *, label: str) -> str:
    if content.count(old) != 1:
        raise RuntimeError(f"{label}: expected one match, found {content.count(old)}")
    return content.replace(old, new, 1)


def update_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    content = replace_once(
        content,
        "from finance_agent.connectors.base import ConnectorError\n",
        "from finance_agent.connectors.base import ConnectorError, ConnectorErrorCode\n",
        label="services connector error import",
    )
    content = replace_once(
        content,
        "from finance_agent.connectors.plaid_fixture import PlaidFixtureIngestor\n",
        (
            "from finance_agent.connectors.plaid_fixture import PlaidFixtureIngestor\n"
            "from finance_agent.connectors.provider_events import record_plaid_provider_events\n"
        ),
        label="services provider event import",
    )
    content = replace_once(
        content,
        'PLAID_MAPPING_VERSION = "plaid_live@1"',
        'PLAID_MAPPING_VERSION = "plaid_live@2"',
        label="Plaid mapping version",
    )

    start = content.index("    async def sync_plaid(\n")
    end = content.index("    async def ingest_telegram_fixture(\n", start)
    method = '''    async def sync_plaid(
        self,
        *,
        public_token: str | None = None,
    ) -> Mapping[str, object]:
        """Record a complete immutable Plaid lifecycle page without currency relabelling."""

        access_token = await self.plaid.resolve_access_token(public_token)
        account_items = await self.plaid.list_accounts(access_token=access_token)
        accounts = normalise_plaid_accounts(account_items)
        if not accounts:
            raise ConnectorError(
                "Plaid returned no accounts",
                code=ConnectorErrorCode.INVALID_RESPONSE,
            )

        added_items: list[Mapping[str, object]] = []
        modified_items: list[Mapping[str, object]] = []
        removed_items = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(PLAID_MAX_PAGES):
            page = await self.plaid.sync_transactions(
                access_token=access_token,
                cursor=cursor,
            )
            added_items.extend(page.added)
            modified_items.extend(page.modified)
            removed_items.extend(page.removed)
            item_count = len(added_items) + len(modified_items) + len(removed_items)
            if item_count > PLAID_MAX_ITEMS:
                raise ConnectorError(
                    "Plaid transaction sync exceeded the local item limit",
                    code=ConnectorErrorCode.LIMIT_EXCEEDED,
                )
            if not page.has_more:
                break
            if page.next_cursor is None:
                raise ConnectorError(
                    "Plaid pagination reported more pages without a cursor",
                    code=ConnectorErrorCode.INVALID_RESPONSE,
                )
            if page.next_cursor in seen_cursors:
                raise ConnectorError(
                    "Plaid transaction pagination repeated a cursor",
                    code=ConnectorErrorCode.REPEATED_CURSOR,
                )
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            raise ConnectorError(
                "Plaid transaction sync exceeded the page limit",
                code=ConnectorErrorCode.LIMIT_EXCEEDED,
            )

        added = normalise_plaid_transactions(tuple(added_items), accounts)
        modified = normalise_plaid_transactions(tuple(modified_items), accounts)
        synced_at = _now().isoformat()
        primary = accounts[0]
        async with self._lock:
            commit = record_plaid_provider_events(
                self.store,
                workspace_id=WORKSPACE_ID,
                account_label=primary.label,
                default_account_id=primary.account_id,
                default_currency=primary.currency,
                added=added,
                modified=modified,
                removed=tuple(removed_items),
                recorded_at=synced_at,
                mapping_version=PLAID_MAPPING_VERSION,
            )
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)

        return {
            "sourceItemId": commit.source_item_id,
            "status": commit.status,
            "sourceSha256": commit.source_sha256,
            "accountCount": len(accounts),
            "transactionCount": len(added) + len(modified) + len(removed_items),
            "rowCount": 0,
            "providerEventCount": commit.event_count,
            "addedCount": commit.added_count,
            "modifiedCount": commit.modified_count,
            "removedCount": commit.removed_count,
            "providerCurrency": primary.currency,
            "ledgerCommitted": False,
            "quarantineReason": "workspace_currency_mismatch",
            "settledOnly": True,
            "liveSyncAttempted": True,
            "externalCallsMade": True,
        }

'''
    write(path, content[:start] + method + content[end:])


def update_akahu() -> None:
    path = "services/api/src/finance_agent/connectors/akahu.py"
    content = read(path)
    content = replace_once(
        content,
        "from finance_agent.connectors.base import ConnectorError\n",
        "from finance_agent.connectors.base import ConnectorError, ConnectorErrorCode\n",
        label="Akahu connector error import",
    )
    content = replace_once(
        content,
        '            raise ConnectorError("Akahu is disabled or unconfigured")\n',
        (
            '            raise ConnectorError(\n'
            '                "Akahu is disabled or unconfigured",\n'
            '                code=ConnectorErrorCode.UNCONFIGURED,\n'
            '            )\n'
        ),
        label="Akahu unconfigured error",
    )
    content = replace_once(
        content,
        '''        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError("Akahu read request failed") from exc
        if not isinstance(payload, Mapping) or payload.get("success") is False:
            raise ConnectorError("Akahu returned an invalid read response")
''',
        '''        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise ConnectorError(
                "Akahu read request failed",
                code=ConnectorErrorCode.UPSTREAM_FAILURE,
                retryable=status == 429 or status >= 500,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError(
                "Akahu read request failed",
                code=ConnectorErrorCode.UPSTREAM_FAILURE,
                retryable=True,
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("success") is False:
            raise ConnectorError(
                "Akahu returned an invalid read response",
                code=ConnectorErrorCode.INVALID_RESPONSE,
            )
''',
        label="Akahu upstream error mapping",
    )
    content = replace_once(
        content,
        '''        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ConnectorError("Akahu response did not contain an item list")
        items = tuple(item for item in raw_items if isinstance(item, Mapping))
''',
        '''        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ConnectorError(
                "Akahu response did not contain an item list",
                code=ConnectorErrorCode.INVALID_RESPONSE,
            )
        if any(not isinstance(item, Mapping) for item in raw_items):
            raise ConnectorError(
                "Akahu response contained a malformed item",
                code=ConnectorErrorCode.INVALID_RESPONSE,
            )
        items = tuple(raw_items)
''',
        label="Akahu strict item list",
    )
    write(path, content)


def update_router() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    content = replace_once(
        content,
        "from finance_agent.connectors.base import ConnectorError\n",
        "from finance_agent.connectors.base import ConnectorError, ConnectorErrorCode\n",
        label="router connector import",
    )
    marker = "\n\ndef create_router() -> APIRouter:\n"
    helper = '''

def connector_http_status(error: ConnectorError) -> int:
    """Map stable connector codes to HTTP semantics without inspecting prose."""

    if error.code in {ConnectorErrorCode.UNCONFIGURED, ConnectorErrorCode.CONFLICT}:
        return 409
    if error.code is ConnectorErrorCode.INVALID_REQUEST:
        return 422
    if error.code is ConnectorErrorCode.LIMIT_EXCEEDED:
        return 429
    return 502


def create_router() -> APIRouter:
'''
    content = replace_once(content, marker, helper, label="router status helper")
    content = replace_once(
        content,
        '''        except ConnectorError as exc:
            status = 409 if str(exc) == "Akahu is disabled or unconfigured" else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
''',
        '''        except ConnectorError as exc:
            raise HTTPException(
                status_code=connector_http_status(exc), detail=str(exc)
            ) from exc
''',
        label="Akahu route error mapping",
    )
    content = replace_once(
        content,
        '''        except ConnectorError as exc:
            status = 409 if "disabled or unconfigured" in str(exc) else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
''',
        '''        except ConnectorError as exc:
            raise HTTPException(
                status_code=connector_http_status(exc), detail=str(exc)
            ) from exc
''',
        label="Plaid link route error mapping",
    )
    content = replace_once(
        content,
        '''        except ConnectorError as exc:
            status = 409 if "disabled or unconfigured" in str(exc) else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
''',
        '''        except ConnectorError as exc:
            raise HTTPException(
                status_code=connector_http_status(exc), detail=str(exc)
            ) from exc
''',
        label="Plaid sync route error mapping",
    )
    write(path, content)


def main() -> None:
    update_services()
    update_akahu()
    update_router()
    print("Connector correctness transformations applied")


if __name__ == "__main__":
    main()
