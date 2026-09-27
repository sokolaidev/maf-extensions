"""Qualify ACAS method rules against two host-controlled recording origins."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import platform
import shlex
import uuid
from collections import Counter
from dataclasses import asdict, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from azure.containerapps.sandbox import (
    EgressHostRule,
    EgressPolicy,
    EgressRule,
    EgressRuleAction,
    EgressRuleMatch,
)
from azure.containerapps.sandbox.aio import SandboxGroupClient
from azure.identity.aio import AzureCliCredential
from maf_sandbox import Egress, SandboxKey, SandboxSpec
from maf_sandbox import EgressRule as CoreEgressRule
from maf_sandbox.conformance import ExecEgressMethodsSubject, assert_egress_methods_conformance
from maf_sandbox_acas import AcasEgressPolicyConflict, AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_acas._credentials import AcasCredentialBinding

METHODS = (
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "TRACE",
    "PROPFIND",
    "X-CUSTOM",
    "*",
)
GUEST = """
import http.client, json, ssl
from urllib.parse import urlsplit
requests = json.loads(PAYLOAD)
for item in requests:
    method, url = item['method'], item['url']
    hops = []
    try:
        for _ in range(4):
            target = urlsplit(url)
            cls = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
            connection = cls(target.hostname, target.port, timeout=12)
            connection.request(method, target.path + '?' + target.query)
            response = connection.getresponse()
            body = response.read(4096)
            hops.append({'method': method, 'status': response.status,
                         'denial': response.getheader('x-deny-reason')})
            location = response.getheader('Location')
            connection.close()
            if not item.get('follow') or response.status not in (301,302,303,307,308):
                break
            if not location:
                break
            if response.status == 303 or response.status in (301,302) and method == 'POST':
                method = 'GET'
            url = location
        print(json.dumps({'id':item['id'], 'hops':hops}), flush=True)
    except Exception as exc:
        print(json.dumps({'id':item['id'], 'hops':hops, 'error':type(exc).__name__}), flush=True)
"""


def rule(host: str, methods: list[str] | None, action: str = "Allow") -> EgressRule:
    """Build one advanced rule without a host-wide allow alongside it."""
    return EgressRule(
        match=EgressRuleMatch(host=host, methods=methods),
        action=EgressRuleAction(type=action),  # pyright: ignore[reportArgumentType]
    )


def policies(hosts: list[str]) -> dict[str, EgressPolicy]:
    """Exercise token matching, wildcard union and first-match precedence separately."""
    a, b = hosts
    wildcard = "*." + a.split(".", 1)[1]
    rows = {
        "control": [rule(a, None), rule(b, None)],
        "get": [rule(a, ["GET"])],
        "post": [rule(a, ["POST"])],
        "custom": [rule(a, ["PROPFIND", "X-CUSTOM"])],
        "literal-star": [rule(a, ["*"])],
        "standard": [rule(a, list(METHODS[:8]))],
        "wildcard-get": [rule(wildcard, ["GET"])],
        "overlap-exact-first": [rule(a, ["POST"]), rule(wildcard, ["GET"])],
        "overlap-wildcard-first": [rule(wildcard, ["GET"]), rule(a, ["POST"])],
        "overlap-all-first": [rule(wildcard, None), rule(a, ["GET"])],
        "overlap-all-last": [rule(a, ["GET"]), rule(wildcard, None)],
        "deny-first": [rule(a, ["POST"], "Deny"), rule(a, None)],
        "deny-last": [rule(a, None), rule(a, ["POST"], "Deny")],
        "connect": [rule(a, ["CONNECT"])],
    }
    result = {
        name: EgressPolicy(default_action="Deny", traffic_inspection="Full", rules=rules)
        for name, rules in rows.items()
    }
    for name, host_rules, advanced in (
        ("host-control", [EgressHostRule(pattern=a), EgressHostRule(pattern=b)], []),
        ("mixed", [EgressHostRule(pattern=b)], [rule(a, ["GET"])]),
        ("mixed-wildcard", [EgressHostRule(pattern=wildcard)], [rule(a, ["GET"])]),
        ("mixed-exact", [EgressHostRule(pattern=b)], [rule(wildcard, ["GET"])]),
    ):
        result[name] = EgressPolicy(
            default_action="Deny", traffic_inspection="Full", host_rules=host_rules, rules=advanced
        )
    return result


def requests_for(hosts: list[str], name: str) -> list[dict[str, Any]]:
    """Give each request and redirect destination its own origin receipt identity."""
    a, b = hosts
    cases: list[dict[str, Any]] = []

    def add(method: str, url: str, *, follow: bool = False) -> None:
        identifier = uuid.uuid4().hex
        cases.append(
            {"id": identifier, "method": method, "url": url + "&id=" + identifier, "follow": follow}
        )

    for scheme in ("http", "https"):
        methods = (
            (*METHODS, "CONNECT")
            if name in {"control", "get", "custom", "literal-star", "standard", "connect"}
            else ("GET", "POST", "PUT")
        )
        for method in methods:
            add(method, f"{scheme}://{a}/probe?case=direct")
        for method in ("GET", "POST"):
            add(method, f"{scheme}://{b}/probe?case=other-host")
        if name in {"control", "get", "post"}:
            for destination in (a, b):
                for status in (302, 303, 307, 308):
                    target = f"{scheme}://{destination}/probe?id=" + uuid.uuid4().hex
                    query = urlencode({"to": target})
                    add(
                        "POST" if name != "get" else "GET",
                        f"{scheme}://{a}/redirect/{status}?{query}",
                        follow=True,
                    )
            target = (
                f"{'http' if scheme == 'https' else 'https'}://{a}/probe?id=" + uuid.uuid4().hex
            )
            add("GET", f"{scheme}://{a}/redirect/302?" + urlencode({"to": target}), follow=True)
    return cases


def receipts(config: dict[str, Any]) -> list[dict[str, str]]:
    """Read origin logs over authenticated host-side HTTPS."""
    events: list[dict[str, str]] = []
    for index, host in enumerate(config["hosts"]):
        request = Request(
            f"https://{host}/receipts", headers={"Authorization": "Bearer " + config["token"]}
        )
        with urlopen(request, timeout=30) as response:
            events.extend({**event, "origin": str(index)} for event in json.load(response))
    return events


def verify_case(case: dict[str, Any], policy: EgressPolicy, hosts: list[str]) -> int:
    """Require origin receipts for allowed TLS hops and service denials for restricted hops."""

    def matches(pattern: str, host: str) -> bool:
        return host.endswith(pattern[1:]) if pattern.startswith("*.") else host == pattern

    def allows(host: str, method: str) -> bool:
        for entry in policy.rules:
            match = entry.match
            assert match is not None and entry.action is not None
            if matches(match.host, host) and (match.methods is None or method in match.methods):
                return entry.action.type == "Allow"
        return any(
            matches(entry.pattern, host) and entry.action == "Allow" for entry in policy.host_rules
        )

    results = {row["id"]: row for row in case["results"]}
    assert len(results) == len(case["requests"]), "missing or duplicate guest results"
    expected: Counter[tuple[str, str, str]] = Counter()
    checked = 0
    for request in case["requests"]:
        if request["method"] == "CONNECT":
            continue
        result = results[request["id"]]
        method, url = request["method"], request["url"]
        expected_hops = 0
        for index in range(2):
            target = urlsplit(url)
            if target.scheme != "https":
                break
            assert "error" not in result, (case["name"], method, result)
            assert len(result["hops"]) > index, "missing redirect hop"
            hop = result["hops"][index]
            assert hop["method"] == method
            expected_hops += 1
            checked += 1
            if not allows(target.hostname or "", method):
                assert hop["status"] == 403 and hop["denial"], (case["name"], method, hop)
                assert len(result["hops"]) == expected_hops
                break
            redirect = target.path.startswith("/redirect/")
            status = int(target.path.rsplit("/", 1)[1]) if redirect else 200
            assert hop["status"] == status, (case["name"], method, hop)
            identifier = parse_qs(target.query)["id"][0]
            expected[(identifier, method, str(hosts.index(target.hostname)))] += 1
            if not redirect or not request["follow"]:
                assert len(result["hops"]) == expected_hops
                break
            url = parse_qs(target.query)["to"][0]
            if status == 303 or status in (301, 302) and method == "POST":
                method = "GET"
    actual = Counter((event["id"], event["method"], event["origin"]) for event in case["receipts"])
    assert actual == expected, (
        f"{case['name']}: missing allowed receipts or unexpected origin traffic"
    )
    return checked


async def measure(config: dict[str, Any], output: Path, selected: set[str]) -> None:
    """Create an isolated sandbox per policy and verify the probe label is empty on exit."""
    label = "methods-" + uuid.uuid4().hex[:12]
    report: dict[str, Any] = {
        "sdk": version("azure-containerapps-sandbox"),
        "python": platform.python_version(),
        "api": "2026-02-01-preview",
        "image": "python-3.13",
        "cases": [],
        "cleanup": False,
    }

    def save() -> None:
        text = json.dumps(report, indent=2)
        for index, host in enumerate(config["hosts"]):
            text = text.replace(host, f"origin-{index}.probe.example")
        text = text.replace(config["hosts"][0].split(".", 1)[1], "probe.example")
        output.write_text(text + "\n", encoding="utf-8")

    async with (
        AzureCliCredential() as credential,
        SandboxGroupClient(
            config["endpoint"],
            credential,
            subscription_id=config["subscription_id"],
            resource_group=config["resource_group"],
            sandbox_group=config["sandbox_group"],
        ) as group,
    ):
        try:
            for name, policy in policies(config["hosts"]).items():
                if selected and name not in selected:
                    continue
                print(f"Creating {name}", flush=True)
                case: dict[str, Any] = {"name": name, "policy": asdict(policy)}
                report["cases"].append(case)
                save()
                poller = await group.begin_create_sandbox(
                    disk="python-3.13", labels={"probe": label}, egress_policy=policy
                )
                sandbox = await poller.result()
                try:
                    case["effective_policy"] = asdict(await sandbox.get_egress_policy())
                    inputs = requests_for(config["hosts"], name)
                    case["requests"] = inputs
                    program = GUEST.replace("PAYLOAD", repr(json.dumps(inputs)))
                    encoded = base64.b64encode(program.encode()).decode()
                    command = shlex.join(
                        ["python3", "-c", f"import base64; exec(base64.b64decode({encoded!r}))"]
                    )
                    result = await sandbox.exec(command)
                    if result.exit_code:
                        raise RuntimeError(
                            f"Guest probe exited {result.exit_code}: {result.stderr}"
                        )
                    case["results"] = [json.loads(line) for line in result.stdout.splitlines()]
                    events = await asyncio.to_thread(receipts, config)
                    ids = {item["id"] for item in inputs}
                    for item in inputs:
                        target = parse_qs(urlsplit(item["url"]).query).get("to", [])
                        if target:
                            ids.update(parse_qs(urlsplit(target[0]).query).get("id", []))
                    case["receipts"] = [event for event in events if event["id"] in ids]
                    case["verified_https_hops"] = verify_case(case, policy, config["hosts"])
                    print(
                        f"{name}: {len(case['results'])} responses, {len(case['receipts'])} origin receipts",
                        flush=True,
                    )
                    save()
                finally:
                    await group.delete_sandbox(sandbox.sandbox_id)
                    await sandbox.close()
        finally:
            remaining = [item async for item in group.list_sandboxes(labels={"probe": label})]
            for item in remaining:
                await group.delete_sandbox(item.id)
            report["cleanup"] = not [
                item async for item in group.list_sandboxes(labels={"probe": label})
            ]
            save()


async def measure_adapter(config: dict[str, Any], output: Path, selected: set[str]) -> None:
    """Run the qualified policy matrix, conformance and warm-reuse checks through the adapter."""

    async def binding(_request):
        return AcasCredentialBinding("egress-probe", "1", AzureCliCredential)

    backend = AcasSandboxBackend(
        AcasSandboxConfig(
            **{
                name: config[name]
                for name in ("endpoint", "subscription_id", "resource_group", "sandbox_group")
            },
            credential_resolver=binding,
        )
    )
    scope = "methods-" + uuid.uuid4().hex[:12]
    report: dict[str, Any] = {
        "sdk": version("azure-containerapps-sandbox"),
        "adapter": version("maf-sandbox-acas"),
        "cases": [],
        "cleanup": False,
    }
    control_spec = SandboxSpec(
        kind="methods",
        image="python-3.13",
        egress=Egress.ALLOWLIST,
        egress_allow=tuple(config["hosts"]),
    )

    def save() -> None:
        text = json.dumps(report, indent=2)
        for index, host in enumerate(config["hosts"]):
            text = text.replace(host, f"origin-{index}.probe.example")
        text = text.replace(config["hosts"][0].split(".", 1)[1], "probe.example")
        output.write_text(text + "\n", encoding="utf-8")

    try:
        control_key = SandboxKey(scope, "probe", "conformance-control")
        await backend.acquire(control_key, control_spec)
        for name, policy in policies(config["hosts"]).items():
            if name in {"deny-first", "deny-last", "connect"} or selected and name not in selected:
                continue
            entries: list[str | CoreEgressRule] = [entry.pattern for entry in policy.host_rules]
            for entry in policy.rules:
                assert entry.match is not None
                methods = tuple(entry.match.methods) if entry.match.methods is not None else None
                entries.append(CoreEgressRule(entry.match.host, methods))
            spec = replace(control_spec, egress_allow=tuple(entries))
            key = SandboxKey(scope, "probe", name)
            print(f"Adapter {name}", flush=True)
            sandbox = await backend.acquire(key, spec)
            try:
                if name == "get":
                    control = await backend.acquire(control_key, control_spec)
                    nonce = uuid.uuid4().hex
                    await assert_egress_methods_conformance(
                        ExecEgressMethodsSubject(sandbox, backend.declarations.capabilities),
                        ExecEgressMethodsSubject(control, backend.declarations.capabilities),
                        allowed_url=f"https://{config['hosts'][0]}/probe?id={nonce}",
                    )
                    hits = [
                        event
                        for event in await asyncio.to_thread(receipts, config)
                        if event["id"] == nonce
                    ]
                    assert sorted(event["method"] for event in hits) == ["GET", "POST"]
                    report["shared_conformance"] = (
                        "passed with exactly GET and control POST at origin"
                    )
                equivalent = replace(
                    spec,
                    egress_allow=tuple(
                        CoreEgressRule(e.host.upper(), tuple(reversed(e.methods)))
                        if isinstance(e, CoreEgressRule) and e.methods
                        else str(e).upper()
                        for e in reversed(spec.egress_allow)
                    ),
                )
                assert (await backend.acquire(key, equivalent)).instance_id == sandbox.instance_id
                changed = replace(
                    spec, egress_allow=(CoreEgressRule(config["hosts"][0], ("HEAD",)),)
                )
                try:
                    await backend.acquire(key, changed)
                except AcasEgressPolicyConflict:
                    pass
                else:
                    raise AssertionError("changed methods reused a held sandbox")
                assert (await backend.acquire(key, spec)).instance_id == sandbox.instance_id
                inputs = requests_for(config["hosts"], name)
                program = GUEST.replace("PAYLOAD", repr(json.dumps(inputs)))
                encoded = base64.b64encode(program.encode()).decode()
                result = await sandbox.exec(
                    ["python3", "-c", f"import base64; exec(base64.b64decode({encoded!r}))"],
                    working_directory="/",
                    timeout=120,
                )
                assert result.exit_code == 0, result.stderr
                rows = [json.loads(line) for line in result.stdout.splitlines()]
                assert len(rows) == len(inputs)
                ids = {item["id"] for item in inputs}
                for item in inputs:
                    target = parse_qs(urlsplit(item["url"]).query).get("to", [])
                    if target:
                        ids.update(parse_qs(urlsplit(target[0]).query).get("id", []))
                hits = [
                    event
                    for event in await asyncio.to_thread(receipts, config)
                    if event["id"] in ids
                ]
                case = {
                    "name": name,
                    "requests": inputs,
                    "results": rows,
                    "receipts": hits,
                    "reuse": "equivalent accepted; changed refused; original preserved",
                }
                case["verified_https_hops"] = verify_case(case, policy, config["hosts"])
                report["cases"].append(case)
                save()
            finally:
                assert await backend.dispose(key, kind=spec.kind) is None
    finally:
        purge = await backend.dispose_scope(scope, "probe")
        assert purge.undisposed is None
        async with (
            AzureCliCredential() as credential,
            SandboxGroupClient(
                config["endpoint"],
                credential,
                subscription_id=config["subscription_id"],
                resource_group=config["resource_group"],
                sandbox_group=config["sandbox_group"],
            ) as group,
        ):
            report["cleanup"] = not [
                item
                async for item in group.list_sandboxes(labels={"scope": scope, "thread": "probe"})
            ]
        await backend.aclose()
        save()
        assert report["cleanup"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument(
        "--adapter",
        action="store_true",
        help="Exercise the backend instead of raw SDK policy creation",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Create and delete billable test sandboxes",
    )
    args = parser.parse_args()
    operation = measure_adapter if args.adapter else measure
    asyncio.run(
        operation(
            json.loads(args.config.read_text(encoding="utf-8-sig")), args.output, set(args.case)
        )
    )
