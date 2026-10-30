"""Typed pricing queries through the sandbox protocol and core's provenance checks."""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

from maf_sandbox import CallerContext, SandboxRouter, SourceIntegrity
from maf_sandbox.bounded_exec import BoundedExec
from maf_sandbox.maf import SandboxResult, SandboxToolSession, sandboxed_tool

from ._report import parse_report
from ._spec import AZURE_PRICING_TOOL_NAME, PriceType, azure_pricing_sandbox_spec

_GUIDANCE = (
    "These are public retail prices, not negotiated account prices. Reservation retailPrice "
    "is the whole-term price even when unitOfMeasure says '1 Hour'; do not multiply it by "
    "monthly hours. Savings-plan terms are separate. Azure MCP omits tierMinimumUnits, so "
    "these rows cannot establish usage-tier thresholds. A truncated result is not exhaustive."
)


def _text(value: str | None, limit: int = 1024) -> str | None:
    if value is not None and (
        type(value) is not str
        or not value.strip()
        or len(value) > limit
        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("Invalid filter text")
    return value


def _argv(
    sku: str | None,
    service: str | None,
    region: str | None,
    service_family: str | None,
    price_type: PriceType | None,
    currency: str,
    include_savings_plan: bool,
    filter: str | None,
) -> list[str]:
    fields = {
        "sku": _text(sku),
        "service": _text(service),
        "region": _text(region),
        "service-family": _text(service_family),
        "price-type": PriceType(price_type).value if price_type is not None else None,
        "filter": _text(filter, 4096),
    }
    if not any(fields.values()) or (service is not None and sku is None):
        raise ValueError("A filter is required; service also requires SKU")
    if type(currency) is not str or not re.fullmatch("[A-Z]{3}", currency):
        raise ValueError("Invalid currency")
    if type(include_savings_plan) is not bool:
        raise ValueError("Invalid savings plan flag")
    args = ["/usr/local/bin/azmcp", "pricing", "get", f"--currency={currency}"]
    for name, value in fields.items():
        if value is not None:
            # Group raw OData so an OR cannot escape the other selected filters.
            value = f"({value})" if name == "filter" else value
            args.append(f"--{name}={value}")
    if include_savings_plan:
        args.append("--include-savings-plan")
    return args


def _incomplete(message: str) -> SandboxResult:
    return SandboxResult(completed=False, trusted_output=(message,))


def _build_tool(
    session: SandboxToolSession,
    timeout: float,
    max_rows: int,
    max_output_bytes: int,
) -> Callable[..., Awaitable[SandboxResult]]:
    async def pricing_get(
        sku: str | None = None,
        service: str | None = None,
        region: str | None = None,
        service_family: str | None = None,
        price_type: PriceType | None = None,
        currency: str = "USD",
        include_savings_plan: bool = False,
        filter: str | None = None,
    ) -> SandboxResult:
        """Look up current Azure public retail prices for explicit filters.

        At least one filter is required; service also requires an ARM SKU. SavingsPlan is
        not a price type: set include_savings_plan instead. Use filter for raw OData.
        Narrow queries when truncated. Reservation prices cover the whole reservation term.
        """
        try:
            args = _argv(
                sku,
                service,
                region,
                service_family,
                price_type,
                currency,
                include_savings_plan,
                filter,
            )
        except (TypeError, ValueError):
            return _incomplete(
                "Pricing query refused: provide bounded nonempty filters, a three-letter uppercase "
                "currency and a valid price type. Service queries also require a SKU."
            )
        key = session.key()
        if isinstance(key, str):
            return _incomplete("Pricing lookup incomplete: caller context is unavailable.")
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return _incomplete("Pricing lookup incomplete: the sandbox could not be acquired.")
        if not isinstance(sandbox, BoundedExec):
            return _incomplete("Pricing lookup incomplete: bounded execution is required.")
        try:
            async with asyncio.timeout(timeout):
                result = await sandbox.exec_bounded(
                    args,
                    working_directory=".",
                    timeout=timeout,
                    max_output_bytes=max_output_bytes,
                )
            if result.exit_code != 0 or result.producer_owns_stderr or result.stderr_bytes:
                return _incomplete(
                    "Pricing lookup incomplete: the CLI did not return a clean result."
                )
            if len(result.stdout_bytes) > max_output_bytes:
                return _incomplete(
                    "Pricing lookup incomplete: the response exceeds the byte limit."
                )
            report, truncated = parse_report(result.stdout_bytes, max_rows)
        except Exception:
            return _incomplete(
                "Pricing lookup incomplete: execution timed out, exceeded its byte limit, "
                "or returned an invalid response."
            )
        return SandboxResult(
            completed=True,
            trusted_output=("Pricing results may be incomplete; narrow the filters.",)
            if truncated
            else (),
            output=(report,),
        )

    return pricing_get


def make_azure_pricing_tools(
    router: SandboxRouter | None,
    agent_id: str,
    context: CallerContext,
    *,
    image: str | None = None,
    image_id: str | None = None,
    egress_integrity: SourceIntegrity | None = None,
    exec_timeout_seconds: float = 60,
    max_rows: int = 100,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> list[Any]:
    """Attach a pricing tool with immutable egress and host-selected output budgets.

    The host may vouch for prices.azure.com with egress_integrity=TRUSTED. Price rows still
    need trusted framework call evidence for promotion; without it they remain untrusted.
    """
    if not math.isfinite(exec_timeout_seconds) or exec_timeout_seconds <= 0:
        raise ValueError("exec_timeout_seconds must be finite and positive")
    if type(max_rows) is not int or not 1 <= max_rows <= 5000:
        raise ValueError("max_rows must be between 1 and 5000")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 64 * 1024 * 1024:
        raise ValueError("max_output_bytes must be between 1 KiB and 64 MiB")
    return sandboxed_tool(
        lambda session: _build_tool(session, exec_timeout_seconds, max_rows, max_output_bytes),
        router=router,
        context=context,
        agent_id=agent_id,
        spec=azure_pricing_sandbox_spec(image, image_id),
        name=AZURE_PRICING_TOOL_NAME,
        approval_mode="never_require",
        source_integrity=SourceIntegrity.UNTRUSTED,
        egress_integrity=egress_integrity,
        result_contract=True,
        standing_guidance=(_GUIDANCE,),
    )
