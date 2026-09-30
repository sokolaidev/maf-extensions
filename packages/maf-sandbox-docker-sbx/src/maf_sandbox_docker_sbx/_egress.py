"""An ``ALLOWLIST`` spec as ``sbx`` policy rules, and the host posture that makes them exact.

``sbx`` applies global allow rules to every sandbox, and within one sandbox's own rules a deny
beats an allow.  A sandbox's rules override the global ones for the hosts they name.  So a
sandbox gets exactly its spec's hosts only when every global allow is denied for it, and a
global allow that also covers a requested host cannot be denied without denying that host:
such a host posture is refused.  Stored service secrets are injected into requests to domains
``sbx`` does not report, so any of them refuses too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import cast

from maf_sandbox import EgressRule

__all__ = [
    "EgressPlan",
    "PostureRefused",
    "Requested",
    "classify",
    "drift",
    "fingerprint",
    "global_allows",
    "plan_for",
    "posture_refusal",
    "requested",
]


class PostureRefused(Exception):
    """The host's ``sbx`` state makes the requested allowlist inexact."""


@dataclass(frozen=True)
class Requested:
    """One ``egress_allow`` entry: a host or ``*.`` wildcard, and an HTTP scope if any."""

    host: str
    methods: tuple[str, ...] | None
    paths: tuple[str, ...] | None

    @property
    def wildcard(self) -> bool:
        return self.host.startswith("*.")

    @property
    def domain(self) -> str:
        return self.host[2:] if self.wildcard else self.host

    @property
    def host_wide(self) -> bool:
        return self.methods is None and self.paths is None

    def matches(self, host: str) -> bool:
        """Whether this entry admits ``host``: exact, or any-depth subdomain for ``*.``."""
        return host.endswith("." + self.domain) if self.wildcard else host == self.host


def requested(entries: Sequence[str | EgressRule]) -> tuple[Requested, ...]:
    """The spec's allowlist, lowercased; an ``authority`` rule is refused here."""
    out: list[Requested] = []
    for entry in entries:
        if isinstance(entry, EgressRule):
            if entry.authority is not None:
                raise ValueError("this backend attaches no identity, so it refuses authority rules")
            methods = None if entry.methods is None else tuple(str(m) for m in entry.methods)
            out.append(Requested(entry.host.lower(), methods, entry.paths))
        else:
            out.append(Requested(entry.lower(), None, None))
    return tuple(out)


def fingerprint(entries: Iterable[Requested]) -> list[list[object]]:
    """A JSON-able record of the allowlist, so a warm acquire can compare specs."""
    return sorted(
        [
            [e.host, list(e.methods) if e.methods else None, list(e.paths) if e.paths else None]
            for e in entries
        ],
        key=json.dumps,
    )


@dataclass(frozen=True)
class EgressPlan:
    """The ``sbx policy`` rules one sandbox gets, and the global allows they answer for."""

    #: ``allow network`` argument lists, without the ``--sandbox`` part.
    allows: tuple[tuple[str, ...], ...]
    #: Resources denied for the sandbox: every other global allow, and bare names under ``*.``.
    denies: tuple[str, ...]
    #: The active global allows when the plan was made; a later one widens the sandbox.
    globals: frozenset[str]

    def record(self) -> dict[str, object]:
        return {
            "allows": [list(args) for args in self.allows],
            "denies": list(self.denies),
            "globals": sorted(self.globals),
        }

    @classmethod
    def from_record(cls, record: object) -> EgressPlan | None:
        if not isinstance(record, dict):
            return None
        fields = cast("dict[str, list[object]]", record)
        try:
            return cls(
                tuple(
                    tuple(str(a) for a in cast("list[object]", args)) for args in fields["allows"]
                ),
                tuple(str(d) for d in fields["denies"]),
                frozenset(str(g) for g in fields["globals"]),
            )
        except (KeyError, TypeError):
            return None


def _sbx_path(path: str) -> str:
    # The core's `/v1/*` admits `/v1` and everything below it, as sbx's `/v1/**` does.
    return path[:-1] + "*" + "*" if path.endswith("/*") else path


def _allow_args(entry: Requested) -> list[tuple[str, ...]]:
    host = "**." + entry.domain if entry.wildcard else entry.host
    if entry.host_wide:
        return [(host,)]
    methods = "ANY" if entry.methods is None else ",".join(entry.methods)
    return [
        (host, "--method", methods, "--path", _sbx_path(path)) for path in entry.paths or ("/**",)
    ]


def _pattern(resource: str) -> tuple[str, str | None]:
    """A policy resource as ``(host pattern, port)``; IPv6 and CIDR stay whole, with no port."""
    if resource.startswith("[") or "/" in resource or resource.count(":") > 1:
        return resource, None
    host, _, port = resource.partition(":")
    return host.lower(), port or None


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**.", i):
            out += r"(?:[^.]+\.)*"
            i += 3
        elif pattern.startswith("**", i):
            out += r".*"
            i += 2
        elif pattern[i] == "*":
            out += r"[^.]+"
            i += 1
        elif pattern[i] == "?":
            out += r"[^.]"
            i += 1
        elif pattern[i] == "[":
            end = pattern.find("]", i)
            if end < 0:
                out += re.escape(pattern[i:])
                break
            body = pattern[i + 1 : end]
            out += (
                "[^" + re.escape(body[1:]) + "]"
                if body.startswith("!")
                else "[" + re.escape(body) + "]"
            )
            i = end + 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


def _literal_suffix(pattern: str) -> str:
    """What every host a glob matches ends with: the text after its last glob character."""
    cut = max(pattern.rfind(c) for c in "*?]")
    return pattern[cut + 1 :].lstrip(".")


def _covers(entries: Sequence[Requested], host: str) -> bool:
    return any(e.host_wide and e.matches(host) for e in entries)


def classify(resource: str, entries: Sequence[Requested]) -> str:
    """``covered``, ``overlap`` or ``disjoint``: what one global allow is to the allowlist.

    ``covered`` means every host it admits is already admitted host-wide, so leaving it adds
    nothing; ``overlap`` means it admits a requested host and more, so it can be neither left
    nor denied; ``disjoint`` means it can be denied for the sandbox.
    """
    host, _port = _pattern(resource)
    if not any(c in host for c in "*?["):
        if _covers(entries, host):
            return "covered"
        return "overlap" if any(e.matches(host) for e in entries) else "disjoint"
    if host == "**":
        return "overlap" if entries else "disjoint"
    suffix = _literal_suffix(host)
    exact = _glob_regex(host)
    # `**.x` also admits `x`, which the plan denies for the sandbox unless something asks for it.
    if host in ("*." + suffix, "**." + suffix) and any(
        e.host_wide and e.wildcard and (suffix == e.domain or suffix.endswith("." + e.domain))
        for e in entries
    ):
        return "covered"
    for e in entries:
        if e.wildcard:
            if (
                suffix == e.domain
                or suffix.endswith("." + e.domain)
                or e.domain.endswith("." + suffix)
            ):
                return "overlap"
        elif exact.fullmatch(e.host):
            return "overlap"
    return "disjoint"


def global_allows(policy_json: bytes) -> frozenset[str]:
    """Every resource an active global allow rule admits, HTTP rules by their target hosts."""
    rules = cast("list[dict[str, object]]", json.loads(policy_json).get("rules") or [])
    found: set[str] = set()
    for rule in rules:
        if rule.get("scope") != "global" or rule.get("decision") != "allow":
            continue
        if rule.get("status") != "active":
            continue
        if rule.get("resource_type") == "network":
            found.update(str(r) for r in cast("list[object]", rule.get("resources") or []))
        elif rule.get("resource_type") == "http":
            targets = cast("list[dict[str, object]]", rule.get("http_targets") or [])
            found.update(str(t.get("host")) for t in targets if t.get("host"))
    return frozenset(found)


def posture_refusal(
    entries: Sequence[Requested], secrets_json: bytes, sandbox: str, governed: bool
) -> str | None:
    """Why the host's secrets or governance make an allowlist inexact, or ``None``."""
    if governed:
        return (
            "organization governance is active, so this host's local allow rules are inactive "
            "and an allowlist cannot be set per sandbox"
        )
    payload = cast("dict[str, object]", json.loads(secrets_json))

    def reaches(secret: dict[str, object]) -> bool:
        scope = str(secret.get("scope"))
        return scope == "global" or sandbox in scope

    services = cast("list[dict[str, object]]", payload.get("secrets") or [])
    mine = [s for s in services if reaches(s)]
    if mine:
        names = ", ".join(sorted({str(s.get("name")) for s in mine}))
        return (
            f"service secrets are stored ({names}); sbx injects them into requests to domains it "
            "does not report, so an allowlist could carry them. Remove them with `sbx secret rm`"
        )
    if payload.get("env_only_count"):
        return (
            "sbx reports secrets taken from the host environment, whose domains it does not report"
        )
    customs = cast("list[dict[str, object]]", payload.get("custom_secrets") or [])
    for custom in filter(reaches, customs):
        for target in cast("list[object]", custom.get("targets") or []):
            if classify(str(target), entries) != "disjoint":
                return (
                    f"custom secret {custom.get('env')!r} is injected into requests to {target!r}, "
                    "which the allowlist reaches"
                )
    return None


def plan_for(entries: Sequence[Requested], globals_: frozenset[str]) -> EgressPlan:
    """The rules for one sandbox, or ``PostureRefused`` naming the global allow in the way."""
    denies: list[str] = []
    for resource in sorted(globals_):
        verdict = classify(resource, entries)
        if verdict == "overlap":
            raise PostureRefused(
                f"the global allow {resource!r} admits a requested host and more, and denying it "
                "for this sandbox would deny that host too. Narrow or remove the global rule"
            )
        if verdict == "disjoint":
            denies.append(resource)
    for entry in entries:
        # `*.x` is subdomains only; sbx's `**.x` also admits `x`, unless something else asks.
        if entry.wildcard and not any(other.matches(entry.domain) for other in entries):
            denies.append(entry.domain)
    allows = tuple(args for entry in entries for args in _allow_args(entry))
    return EgressPlan(allows, tuple(dict.fromkeys(denies)), globals_)


def drift(
    entries: Sequence[Requested], plan: EgressPlan, policy_json: bytes, sandbox: str
) -> str | None:
    """Why the sandbox's rules no longer admit exactly the allowlist, or ``None``."""
    for resource in sorted(global_allows(policy_json) - plan.globals):
        if classify(resource, entries) != "covered":
            return f"the global allow {resource!r} was added after this sandbox opened"
    rules = cast("list[dict[str, object]]", json.loads(policy_json).get("rules") or [])
    own = [r for r in rules if r.get("scope") == f"sandbox:{sandbox}"]
    if inactive := [str(r.get("id")) for r in own if r.get("status") != "active"]:
        return f"the sandbox's rules {inactive} are no longer active"
    denied: set[str] = set()
    allowed: set[str] = set()
    for rule in own:
        resources = {str(r) for r in cast("list[object]", rule.get("resources") or [])}
        targets = cast("list[dict[str, object]]", rule.get("http_targets") or [])
        resources |= {str(t.get("host")) for t in targets if t.get("host")}
        (denied if rule.get("decision") == "deny" else allowed).update(resources)
    if missing := sorted(set(plan.denies) - denied):
        return f"the sandbox's deny rules for {missing[:5]} were removed"
    if extra := sorted(allowed - {args[0] for args in plan.allows}):
        return f"allow rules for {extra[:5]} were added to the sandbox"
    return None
