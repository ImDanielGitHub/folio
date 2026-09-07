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


def replace_method(path: str, class_name: str, name: str, replacement: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    candidate = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    if candidate.end_lineno is None:
        raise RuntimeError(f"{path}: method {class_name}.{name} has no end line")
    lines = content.splitlines(keepends=True)
    start = candidate.lineno - 1
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    write(path, "".join(lines[:start]) + replacement.rstrip() + "\n\n" + "".join(lines[candidate.end_lineno:]))


RETRY_MODULE = '''"""Bounded retry policy for idempotent provider HTTP reads."""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})
TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.WriteTimeout,
)
Sleep = Callable[[float], Awaitable[None]]
RandomValue = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 0.25
    maximum_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 8:
            raise ValueError("retry max attempts must be between 1 and 8")
        if self.base_delay_seconds < 0:
            raise ValueError("retry base delay must be non-negative")
        if not 0 < self.maximum_delay_seconds <= 120:
            raise ValueError("retry maximum delay must be between 0 and 120 seconds")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("retry jitter ratio must be between 0 and 1")

    @classmethod
    def from_env(cls, prefix: str) -> RetryPolicy:
        key = prefix.upper().strip()
        return cls(
            max_attempts=int(os.getenv(f"{key}_RETRY_MAX_ATTEMPTS", "4")),
            base_delay_seconds=float(os.getenv(f"{key}_RETRY_BASE_SECONDS", "0.25")),
            maximum_delay_seconds=float(os.getenv(f"{key}_RETRY_MAX_SECONDS", "30")),
            jitter_ratio=float(os.getenv(f"{key}_RETRY_JITTER_RATIO", "0.2")),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "maxAttempts": self.max_attempts,
            "baseDelaySeconds": self.base_delay_seconds,
            "maximumDelaySeconds": self.maximum_delay_seconds,
            "jitterRatio": self.jitter_ratio,
            "retryStatusCodes": sorted(TRANSIENT_STATUS_CODES),
            "idempotentOnly": True,
        }


@dataclass(frozen=True, slots=True)
class ProviderJSONResponse:
    payload: Mapping[str, object]
    attempts: int
    status_code: int
    request_id: str | None


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        operation: str,
        attempts: int,
        retryable: bool,
        status_code: int | None = None,
        request_id: str | None = None,
        cause_type: str | None = None,
    ) -> None:
        super().__init__(f"{provider} {operation} request failed")
        self.provider = provider
        self.operation = operation
        self.attempts = attempts
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id
        self.cause_type = cause_type

    def safe_detail(self) -> str:
        values = [
            f"provider={self.provider}",
            f"operation={self.operation}",
            f"attempts={self.attempts}",
            f"retryable={str(self.retryable).lower()}",
        ]
        if self.status_code is not None:
            values.append(f"status={self.status_code}")
        if self.request_id:
            values.append(f"request_id={self.request_id[:120]}")
        if self.cause_type:
            values.append(f"cause={self.cause_type[:80]}")
        return "provider request failed (" + ", ".join(values) + ")"


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    candidate = value.strip()
    try:
        seconds = float(candidate)
    except ValueError:
        try:
            target = parsedate_to_datetime(candidate)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        seconds = (target - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, seconds)


def _delay(
    policy: RetryPolicy,
    attempt: int,
    retry_after: str | None,
    random_value: RandomValue,
) -> float:
    explicit = retry_after_seconds(retry_after)
    base = explicit if explicit is not None else policy.base_delay_seconds * (2 ** max(0, attempt - 1))
    bounded = min(policy.maximum_delay_seconds, max(0.0, base))
    jitter = bounded * policy.jitter_ratio * max(-1.0, min(1.0, random_value() * 2 - 1))
    return max(0.0, min(policy.maximum_delay_seconds, bounded + jitter))


async def request_json_with_retry(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    provider: str,
    operation: str,
    policy: RetryPolicy,
    idempotent: bool,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, object] | None = None,
    sleep: Sleep = asyncio.sleep,
    random_value: RandomValue = random.random,
) -> ProviderJSONResponse:
    attempts_allowed = policy.max_attempts if idempotent else 1
    for attempt in range(1, attempts_allowed + 1):
        try:
            response = await client.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
            )
        except TRANSIENT_EXCEPTIONS as exc:
            if attempt >= attempts_allowed:
                raise ProviderRequestError(
                    provider=provider,
                    operation=operation,
                    attempts=attempt,
                    retryable=idempotent,
                    cause_type=type(exc).__name__,
                ) from exc
            await sleep(_delay(policy, attempt, None, random_value))
            continue
        request_id = (
            response.headers.get("request-id")
            or response.headers.get("x-request-id")
            or response.headers.get("plaid-request-id")
        )
        if response.status_code in TRANSIENT_STATUS_CODES and idempotent:
            if attempt < attempts_allowed:
                await sleep(
                    _delay(
                        policy,
                        attempt,
                        response.headers.get("retry-after"),
                        random_value,
                    )
                )
                continue
            raise ProviderRequestError(
                provider=provider,
                operation=operation,
                attempts=attempt,
                retryable=True,
                status_code=response.status_code,
                request_id=request_id,
            )
        if response.is_error:
            raise ProviderRequestError(
                provider=provider,
                operation=operation,
                attempts=attempt,
                retryable=False,
                status_code=response.status_code,
                request_id=request_id,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                provider=provider,
                operation=operation,
                attempts=attempt,
                retryable=False,
                status_code=response.status_code,
                request_id=request_id,
                cause_type="InvalidJSON",
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProviderRequestError(
                provider=provider,
                operation=operation,
                attempts=attempt,
                retryable=False,
                status_code=response.status_code,
                request_id=request_id,
                cause_type="NonObjectJSON",
            )
        return ProviderJSONResponse(
            payload=payload,
            attempts=attempt,
            status_code=response.status_code,
            request_id=request_id,
        )
    raise AssertionError("retry loop exhausted without a result")
'''

AKAHU_INIT = '''    def __init__(
        self,
        config: AkahuConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.config = config or AkahuConfig.from_env()
        self.retry_policy = retry_policy or RetryPolicy.from_env("AKAHU")
        headers = {"Accept": "application/json"}
        if self.config.user_token and self.config.app_token:
            headers.update(
                {
                    "Authorization": f"Bearer {self.config.user_token}",
                    "X-Akahu-Id": self.config.app_token,
                }
            )
        self._headers = headers
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            headers=headers,
        )
'''

AKAHU_GET = '''    async def _get(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> Mapping[str, object]:
        if not (self.config.enabled and self.config.app_token and self.config.user_token):
            raise ConnectorError("Akahu is disabled or unconfigured")
        try:
            result = await request_json_with_retry(
                self._client,
                method="GET",
                url=f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}",
                provider="akahu",
                operation=path,
                policy=self.retry_policy,
                idempotent=True,
                params=params,
                headers=self._headers,
            )
        except ProviderRequestError as exc:
            raise ConnectorError(exc.safe_detail()) from exc
        payload = result.payload
        if payload.get("success") is False:
            raise ConnectorError("Akahu returned an unsuccessful read response")
        return payload
'''

PLAID_INIT = '''    def __init__(
        self,
        config: PlaidConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.config = config or PlaidConfig.from_env()
        self.retry_policy = retry_policy or RetryPolicy.from_env("PLAID")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
'''

PLAID_POST = '''    async def _post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        idempotent: bool = True,
    ) -> Mapping[str, object]:
        payload = {**self._auth_body(), **body}
        try:
            result = await request_json_with_retry(
                self._client,
                method="POST",
                url=f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}",
                provider="plaid",
                operation=path,
                policy=self.retry_policy,
                idempotent=idempotent,
                json_body=payload,
            )
        except ProviderRequestError as exc:
            raise ConnectorError(exc.safe_detail()) from exc
        return result.payload
'''

TESTS = '''from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from finance_agent.connectors.provider_http import (
    ProviderRequestError,
    RetryPolicy,
    request_json_with_retry,
    retry_after_seconds,
)


@pytest.mark.asyncio
async def test_idempotent_request_retries_429_and_honours_bounded_retry_after() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "120", "Request-Id": f"req-{attempts}"},
                json={"error": "rate limited"},
            )
        return httpx.Response(200, headers={"Request-Id": "req-ok"}, json={"items": []})

    async def sleep(value: float) -> None:
        delays.append(value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await request_json_with_retry(
            client,
            method="GET",
            url="https://api.akahu.io/v1/accounts",
            provider="akahu",
            operation="accounts",
            policy=RetryPolicy(max_attempts=4, base_delay_seconds=0.1, maximum_delay_seconds=2, jitter_ratio=0),
            idempotent=True,
            sleep=sleep,
            random_value=lambda: 0.5,
        )
    assert result.attempts == 3
    assert result.request_id == "req-ok"
    assert delays == [2, 2]


@pytest.mark.asyncio
async def test_non_idempotent_request_never_retries_ambiguous_transport_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("ambiguous token exchange", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderRequestError) as raised:
            await request_json_with_retry(
                client,
                method="POST",
                url="https://sandbox.plaid.com/item/public_token/exchange",
                provider="plaid",
                operation="item/public_token/exchange",
                policy=RetryPolicy(max_attempts=4),
                idempotent=False,
            )
    assert attempts == 1
    assert raised.value.attempts == 1
    assert raised.value.retryable is False
    assert "ambiguous token exchange" not in raised.value.safe_detail()


@pytest.mark.asyncio
async def test_permanent_400_failure_is_not_retried_or_body_logged() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, headers={"Plaid-Request-Id": "plaid-safe-id"}, json={"secret": "must not escape"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderRequestError) as raised:
            await request_json_with_retry(
                client,
                method="POST",
                url="https://sandbox.plaid.com/accounts/get",
                provider="plaid",
                operation="accounts/get",
                policy=RetryPolicy(max_attempts=4),
                idempotent=True,
            )
    assert attempts == 1
    assert raised.value.status_code == 400
    assert raised.value.request_id == "plaid-safe-id"
    assert "must not escape" not in raised.value.safe_detail()


def test_retry_after_supports_seconds_and_http_dates() -> None:
    now = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("3", now=now) == 3
    target = now + timedelta(seconds=5)
    assert retry_after_seconds(target.strftime("%a, %d %b %Y %H:%M:%S GMT"), now=now) == 5
    assert retry_after_seconds("invalid", now=now) is None
'''

ADAPTER_TESTS = '''from __future__ import annotations

import httpx
import pytest

from finance_agent.connectors.akahu import AkahuConfig, AkahuReadOnlyAdapter
from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.plaid import PlaidConfig, PlaidReadOnlyAdapter
from finance_agent.connectors.provider_http import RetryPolicy


@pytest.mark.asyncio
async def test_akahu_read_retries_transient_gateway_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": "0"}, json={"error": "temporary"})
        return httpx.Response(200, json={"success": True, "items": [], "cursor": {}})

    adapter = AkahuReadOnlyAdapter(
        AkahuConfig(enabled=True, app_token="app", user_token="user"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0, jitter_ratio=0),
    )
    page = await adapter.list_accounts()
    assert page.items == ()
    assert attempts == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_plaid_public_token_exchange_is_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "temporary"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlaidReadOnlyAdapter(
        PlaidConfig(enabled=True, client_id="client", secret="secret"),
        client=client,
        retry_policy=RetryPolicy(max_attempts=4, base_delay_seconds=0, jitter_ratio=0),
    )
    with pytest.raises(ConnectorError):
        await adapter.exchange_public_token("public-token")
    assert attempts == 1
    await client.aclose()
'''


def add_module() -> None:
    write("services/api/src/finance_agent/connectors/provider_http.py", RETRY_MODULE)


def update_akahu() -> None:
    path = "services/api/src/finance_agent/connectors/akahu.py"
    content = read(path)
    marker = "from finance_agent.connectors.base import ConnectorError\n"
    imports = (
        "from finance_agent.connectors.provider_http import (\n"
        "    ProviderRequestError,\n"
        "    RetryPolicy,\n"
        "    request_json_with_retry,\n"
        ")\n"
    )
    if imports not in content:
        if marker not in content:
            raise RuntimeError("Akahu connector import marker missing")
        content = content.replace(marker, marker + imports, 1)
        write(path, content)
    replace_method(path, "AkahuReadOnlyAdapter", "__init__", AKAHU_INIT)
    replace_method(path, "AkahuReadOnlyAdapter", "_get", AKAHU_GET)
    content = read(path)
    capability_marker = '            "goldenPathDependency": False,\n'
    if '"retryPolicy"' not in content:
        if capability_marker not in content:
            raise RuntimeError("Akahu capability marker missing")
        content = content.replace(
            capability_marker,
            capability_marker + '            "retryPolicy": self.retry_policy.as_dict(),\n',
            1,
        )
    write(path, content)


def update_plaid() -> None:
    path = "services/api/src/finance_agent/connectors/plaid.py"
    content = read(path)
    marker = "from finance_agent.connectors.base import ConnectorError\n"
    imports = (
        "from finance_agent.connectors.provider_http import (\n"
        "    ProviderRequestError,\n"
        "    RetryPolicy,\n"
        "    request_json_with_retry,\n"
        ")\n"
    )
    if imports not in content:
        if marker not in content:
            raise RuntimeError("Plaid connector import marker missing")
        content = content.replace(marker, marker + imports, 1)
        write(path, content)
    replace_method(path, "PlaidReadOnlyAdapter", "__init__", PLAID_INIT)
    replace_method(path, "PlaidReadOnlyAdapter", "_post", PLAID_POST)
    content = read(path)
    exchange_marker = '''        response = await self._post(\n            "/item/public_token/exchange",\n            {"public_token": public_token},\n        )\n'''
    exchange_replacement = '''        response = await self._post(\n            "/item/public_token/exchange",\n            {"public_token": public_token},\n            idempotent=False,\n        )\n'''
    if exchange_marker not in content:
        raise RuntimeError("Plaid token exchange marker missing")
    content = content.replace(exchange_marker, exchange_replacement, 1)
    capability_marker = '            "linkTokenPath": "/v1/connectors/plaid/link-token",\n'
    if '"retryPolicy"' not in content:
        if capability_marker not in content:
            raise RuntimeError("Plaid capability marker missing")
        content = content.replace(
            capability_marker,
            '            "retryPolicy": self.retry_policy.as_dict(),\n' + capability_marker,
            1,
        )
    write(path, content)


def add_tests_docs_env() -> None:
    write("services/api/tests/connectors/test_provider_http_retry.py", TESTS)
    write("services/api/tests/connectors/test_provider_retry_integration.py", ADAPTER_TESTS)
    path = ".env.example"
    content = read(path)
    addition = '''
# Bounded retries apply only to idempotent provider reads.
AKAHU_RETRY_MAX_ATTEMPTS=4
AKAHU_RETRY_BASE_SECONDS=0.25
AKAHU_RETRY_MAX_SECONDS=30
AKAHU_RETRY_JITTER_RATIO=0.2
PLAID_RETRY_MAX_ATTEMPTS=4
PLAID_RETRY_BASE_SECONDS=0.25
PLAID_RETRY_MAX_SECONDS=30
PLAID_RETRY_JITTER_RATIO=0.2
'''
    if "AKAHU_RETRY_MAX_ATTEMPTS" not in content:
        write(path, content.rstrip() + "\n" + addition)
    write("docs/PROVIDER_RESILIENCE.md", '''# Provider retry and failure boundary\n\nFolio retries only provider operations marked idempotent. Transient transport errors and HTTP 429, 502, 503 and 504 responses use bounded exponential backoff with bounded jitter. `Retry-After` seconds or HTTP dates are honoured up to the configured maximum. Permanent client errors fail immediately.\n\nPlaid public-token exchange is deliberately non-idempotent and receives one attempt. Folio does not retry an ambiguous token exchange because the provider may have consumed the public token even when the response was lost. Akahu reads, Plaid account reads and transaction synchronisation are idempotent and may retry.\n\nErrors retain only provider, operation, attempt count, retryability, status, request ID and exception type. Response bodies, access tokens, headers and provider error payloads are never copied into Folio errors or logs. Cancellation propagates through HTTPX and is not swallowed by the retry loop.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 23: bounded provider resilience\n\n- Only idempotent provider reads retry transient transport and 429/502/503/504 failures.\n- Retry-After is supported and bounded; exponential delay includes bounded jitter.\n- Plaid public-token exchange remains single-attempt because ambiguous retries are unsafe.\n- Permanent client failures do not retry.\n- Safe errors retain request IDs and attempt metadata without bodies or credentials.\n- Connector capability surfaces expose the active retry policy.\n'''
    if "## Stack 23: bounded provider resilience" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_module()
    update_akahu()
    update_plaid()
    add_tests_docs_env()
    print("provider resilience changes applied")


if __name__ == "__main__":
    main()
