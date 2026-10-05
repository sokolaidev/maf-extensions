# Public argument provenance: lifetime and adoption

> The public `rewritten_arguments()` API added by upstream [#8506](https://github.com/microsoft/agent-framework/pull/8506) can replace private inspection for named arguments. This record qualifies a remaining lifetime boundary and prepares a request; it does not migrate the suite to an unpublished API. The implementation order is in [release compatibility](../../release-compatibility.md#maf-adoption-sequence).

## Measured behavior

Measured on 2026-10-05 using CPython 3.13.12 and upstream source commit `b9d24c8fb484c8330abe8bb9e7500ca3c3bbf46c`. Its package metadata says `agent-framework-core` 1.20.0, but this is an unreleased source checkout containing #8506, not the published 1.20.0 wheel. The probe uses the public middleware hook and tool API, no private context variables, model or backend.

| Observation | Call A | Call B |
| --- | --- | --- |
| Active async tool | `{"files": {0}}` | `{"files": {1}}` |
| Active `asyncio.to_thread` worker | `{"files": {0}}` | `{"files": {1}}` |
| Child released after both middleware calls finish | `{"files": {0}}` | `{"files": {1}}` |

The parent sees `{}` after both calls finish. The inherited child contexts still retain each completed call's map. This establishes stale implicit context, not cross-call contamination or an observed disclosure. The current API documents the current execution flow but does not promise completion expiry, so the request below asks for an explicit contract rather than claiming a violated expiry guarantee. Source inspection additionally confirms that `{}` means either no context/record or a tracked call with no rewrites.

## Prepared upstream request

**Title:** `[Feature]: Define lifetime and availability for implicit rewritten_arguments context`

**Description**

A tool can spawn an async child that outlives the call. Resetting a `ContextVar` in the middleware's parent flow leaves the copied child context pointing at the completed call. Consequently `rewritten_arguments()` still supplies a positional map when there is no longer an active tool call. A downstream helper that assumes an implicit map belongs to a current call can apply it to newly generated values.

Please define completion expiry for the implicit accessor, including exceptional completion and cancellation. Preserve maps for active async tools, synchronous tools reached through `asyncio.to_thread`, nested calls and concurrent calls. Provide a supported way to distinguish unavailable/expired tracking from an active call with no rewritten arguments; both currently return `{}`. An additive availability accessor or snapshot with explicit status could preserve the existing dictionary return type.

Explicit `rewritten_arguments(context)` access may be useful for historical inspection; please document its lifetime separately rather than silently discarding deliberately retained snapshots. Our current alternative is a wrapper-owned shared record with a closed flag, which children observe after the call ends. We will retain that guard until the public contract covers this case.

**Code Sample**

Run from beside an upstream checkout named `upstream-maf`, checked out at the commit above:

```text
uv run --no-project --no-sources --python 3.13 --with ./upstream-maf/python/packages/core python probe_maf_provenance.py
```

Save the following as `probe_maf_provenance.py`. Its assertions pin the observed current behavior, including the stale map; they are not proposed passing assertions for the future expiry contract.

```python
import asyncio
import json
import platform
from importlib.metadata import version

from agent_framework import FunctionInvocationContext, FunctionTool
from agent_framework.security import (
    ContentLabel,
    IntegrityLabel,
    LabelTrackingFunctionMiddleware,
    rewritten_arguments,
)


async def main():
    tracker = LabelTrackingFunctionMiddleware()
    variable = tracker.get_variable_store().store(
        "hidden.txt", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
    )
    release = asyncio.Event()
    both_active = asyncio.Barrier(2)
    children = []
    evidence = {}

    async def body(files: list[str]):
        key = files[-1]
        await both_active.wait()
        evidence[key] = {"active": rewritten_arguments()}
        evidence[key]["thread"] = await asyncio.to_thread(rewritten_arguments)

        async def child():
            await release.wait()
            evidence[key]["after_completion"] = rewritten_arguments()

        children.append(asyncio.create_task(child()))
        return "ok"

    tool = FunctionTool(
        name="inspect_files", func=body,
        additional_properties={"accepts_untrusted": True},
    )

    async def run(files):
        context = FunctionInvocationContext(function=tool, arguments={"files": files})

        async def call_next():
            context.result = await tool.invoke(arguments=context.arguments)

        await tracker.process(context, call_next)

    await asyncio.gather(
        run([f"[{variable}]", "literal.txt", "call_a"]),
        run(["literal.txt", f"[{variable}]", "call_b"]),
    )
    evidence["parent_after_completion"] = rewritten_arguments()
    release.set()
    await asyncio.gather(*children)
    for key, position in (("call_a", 0), ("call_b", 1)):
        assert evidence[key]["active"] == {"files": {position}}
        assert evidence[key]["thread"] == {"files": {position}}
        assert evidence[key]["after_completion"] == {"files": {position}}
    assert evidence["parent_after_completion"] == {}
    print(json.dumps({"python": platform.python_version(),
                      "package_metadata": version("agent-framework-core"),
                      "observations": evidence}, default=sorted, indent=2))


asyncio.run(main())
```

**Acceptance criteria**

- After normal return, exception or cancellation, an inherited child reports implicit tracking unavailable/expired rather than a live positional map. An explicit captured snapshot has separately documented semantics.
- Concurrent active calls retain separate maps and nested calls restore the still-active outer call correctly.
- Active synchronous tools and `asyncio.to_thread` retain their call's map; completion revokes implicit authority even in inherited worker contexts.
- A supported signal distinguishes no middleware, no record, completed call and an active call with zero rewrites to the extent needed for safe fallback; document which states are intentionally combined.
- Returned maps remain defensive copies. Preserve validator reorder/filter degradation, aliases, scalar `-1` and duplicate-equal-value behavior.

**Language/SDK:** Python

This request is prepared, not filed. Targeted issue searches found the completed public-API request [#8342](https://github.com/microsoft/agent-framework/issues/8342), but no separate completion-lifetime request. The original API delivery and this additional contract are different milestones.

## Downstream implementation order

1. Wait for a published version containing #8506, then qualify it against the suite's current framework floor before changing package ranges.
2. Keep `positions_holding_hidden_content` as the suite interface. Use the public map only for named arguments whose list corresponds to the current call's argument; translate scalar `-1` to the suite's single-value position and retain conservative behavior for unavailable or mismatched records.
3. Preserve the local completed-call guard and generated-value containment fallback. An empty public map alone is insufficient evidence that tracking was available or that a generated value contains no hidden content.
4. Test the adapter with validators that reorder/filter, aliases, equal values at distinct positions, sync tools, concurrent calls, nested calls and children completing after return, exception or cancellation. The probe above covers only active concurrency, `to_thread`, normal completion and child inheritance; the rest remain qualification work.
5. Remove private argument inspection only after equivalent behavior is demonstrated. Remove the wrapper lifetime guard only after a published expiry contract is qualified, which may be a later release than the public accessor itself.
