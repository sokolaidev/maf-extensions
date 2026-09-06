"""Attribute names, and what a recorder is allowed to put on a wire.

Every attribute this package writes is under ``maf_sandbox.``, so a query can select this
suite's records out of a pipeline carrying everyone else's, and nothing here collides with the
GenAI conventions the agent framework already emits around the same call.

The redaction rule is one sentence: **shape and policy always, content only when asked.** What
a sandbox's posture was — the egress mode, the isolation rung, the integrity label, the counts
and the sizes — is what a security question is actually asked in, and none of it is chosen by a
guest.  Names and sentences are the other half: an artifact name is written by the model
(a channel this suite measures rather than closes), a host-tool refusal quotes a bounded copy
of what the guest asked for, and a store file name is the host's own vocabulary about its own
data.  Those cross only under :attr:`Redaction.sensitive`.

A :class:`~maf_sandbox.SandboxKey` is the third case and gets its own treatment: it is what
every other record joins on, so it cannot simply be dropped, and it carries a scope and a
thread id that may name a person.  It is hashed by default — stable across processes, so
grouping still works, and not reversible by reading.  It is **not** a secret: an id drawn from
a small space can be recovered by hashing the candidates, and a deployment that needs the key
withheld from a pipeline should not send it rather than trust this.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata

from maf_sandbox import SandboxKey
from opentelemetry.util.types import AttributeValue

#: Everything this package writes lives under this prefix.
NAMESPACE = "maf_sandbox"

# The instrumentation scope, which is what a backend shows as the source of a span or a log
# record. Its version is passed beside it (see `_observer`) and is this package's rather than
# the core's: it names what did the recording, so records from two releases are tellable apart.
INSTRUMENTATION_SCOPE = "maf_sandbox_otel"


def instrumentation_version() -> str | None:
    """This package's installed version, or ``None`` where it cannot be read.

    ``None`` rather than a guess: a scope with no version is a known shape to a backend, and a
    made-up one would be worse than an absent one for telling two releases apart.
    """
    try:
        return metadata.version("maf-sandbox-otel")
    except metadata.PackageNotFoundError:
        return None


KEY = f"{NAMESPACE}.sandbox.key"
CONVERSATION = f"{NAMESPACE}.sandbox.conversation"
SCOPE = f"{NAMESPACE}.sandbox.scope"
THREAD_ID = f"{NAMESPACE}.sandbox.thread_id"
AGENT_DIR = f"{NAMESPACE}.sandbox.agent_dir"
CALL_ID = f"{NAMESPACE}.sandbox.call_id"

KIND = f"{NAMESPACE}.sandbox.kind"
BACKEND = f"{NAMESPACE}.sandbox.backend"
IMAGE = f"{NAMESPACE}.sandbox.image"
ISOLATION = f"{NAMESPACE}.sandbox.isolation"
ISOLATION_SCOPE = f"{NAMESPACE}.sandbox.isolation_scope"
CAPABILITIES = f"{NAMESPACE}.sandbox.capabilities"
BACKEND_CAPABILITIES = f"{NAMESPACE}.backend.capabilities"
BACKEND_EGRESS_MODES = f"{NAMESPACE}.backend.egress_modes"

EGRESS_MODE = f"{NAMESPACE}.egress.mode"
EGRESS_ALLOW = f"{NAMESPACE}.egress.allow"
EGRESS_ALLOW_COUNT = f"{NAMESPACE}.egress.allow_count"

# The sealed host-tool *surface* a sandbox was served with, which is a different subject from
# one call through it: `surface.` describes what could be called, `host_tool.` what was. Under
# their own prefix rather than a plural of that one, because two names differing by an `s`
# would be a query somebody writes wrong once and never notices.
SURFACE_IDENTITIES = f"{NAMESPACE}.surface.identities"
SURFACE_UNDECLARED = f"{NAMESPACE}.surface.undeclared"
SURFACE_INTEGRITY = f"{NAMESPACE}.surface.result_integrity"
SURFACE_CALL_CAP = f"{NAMESPACE}.surface.call_cap"

TOOL = f"{NAMESPACE}.call.tool"
FAILURE = f"{NAMESPACE}.call.failure"
UNCLEAN = f"{NAMESPACE}.call.unclean"
REFUSAL = f"{NAMESPACE}.refusal"
DURATION = f"{NAMESPACE}.duration"

HOST_TOOL_RUN_ID = f"{NAMESPACE}.host_tool.run_id"
HOST_TOOL_NAME = f"{NAMESPACE}.host_tool.name"
HOST_TOOL_DECLARED = f"{NAMESPACE}.host_tool.declared"
HOST_TOOL_SOURCE = f"{NAMESPACE}.host_tool.source_integrity"
HOST_TOOL_SINK = f"{NAMESPACE}.host_tool.sink"
HOST_TOOL_IDENTITY = f"{NAMESPACE}.host_tool.identity"
HOST_TOOL_OUTCOME = f"{NAMESPACE}.host_tool.outcome"
HOST_TOOL_RESPONSE_BYTES = f"{NAMESPACE}.host_tool.response_bytes"
HOST_TOOL_CALLS = f"{NAMESPACE}.host_tool.calls"
HOST_TOOL_REFUSAL = f"{NAMESPACE}.host_tool.refusal"

STORE_FILE = f"{NAMESPACE}.store.file"
STORE_INTEGRITY = f"{NAMESPACE}.store.integrity"
STORE_CHARACTERS = f"{NAMESPACE}.store.characters"
STORE_OUTCOME = f"{NAMESPACE}.store.outcome"

OUTPUTS_DECLARED = f"{NAMESPACE}.outputs.declared"
OUTPUTS_LANDED = f"{NAMESPACE}.outputs.landed"
OUTPUTS_LANDED_BYTES = f"{NAMESPACE}.outputs.landed_bytes"
OUTPUTS_NAMES = f"{NAMESPACE}.outputs.names"
OUTPUTS_MAX_FILES = f"{NAMESPACE}.outputs.limit.max_files"
OUTPUTS_MAX_BYTES_PER_FILE = f"{NAMESPACE}.outputs.limit.max_bytes_per_file"
OUTPUTS_MAX_TOTAL_BYTES = f"{NAMESPACE}.outputs.limit.max_total_bytes"

DISPOSAL_OUTCOME = f"{NAMESPACE}.disposal.outcome"
DISPOSAL_CODE = f"{NAMESPACE}.disposal.code"
DISPOSAL_DETAIL = f"{NAMESPACE}.disposal.detail"


def _digest(*parts: str) -> str:
    """A stable, non-reading-reversible name for an ordered tuple of strings.

    Length-prefixed rather than delimited.  ``SandboxKey`` puts no constraint on what a scope
    or a thread id may contain, so *any* delimiter is one a part can hold — and two different
    keys rendering to one string would merge two conversations into a single record.

    The **whole** digest, because a truncation is a collision budget and this package cannot
    know the number it would be spent against: the deployment picks the key count, not us.
    These names are join keys an audit groups a conversation's records by, so a collision does
    not blur a statistic — it splices two conversations into one trail, which is the reading
    this package exists to make impossible.  An OpenTelemetry string attribute has no length
    limit to trade against, so there is nothing on the other side of that budget.

    ``surrogatepass`` because a key part is an unvalidated host string, and a lone surrogate is
    what ``json`` gives for a literal ``"\\ud800"``.  Plain UTF-8 raises on one, and this
    package's failures are contained by core — so the record would be dropped with a warning
    while the call succeeded, which is the one outcome a telemetry package must not have.  The
    encoding stays injective, so the collision argument above is unchanged.
    """
    joined = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(joined.encode("utf-8", errors="surrogatepass")).hexdigest()


def hashed_key(key: SandboxKey) -> str:
    """A stable name for one sandbox key — every part of it, the call included."""
    return _digest(key.scope, key.thread_id, key.agent_dir, key.call_id)


def hashed_conversation(key: SandboxKey) -> str:
    """A stable name for the conversation a key belongs to, which the call cannot change.

    Separate from :func:`hashed_key` because a per-call workload puts a fresh ``call_id`` in
    every key, so the key's own hash differs per call and cannot group a conversation's records
    — which is the query this package exists for, and the scope and thread that would answer it
    directly are redacted by default.
    """
    return _digest(key.scope, key.thread_id)


@dataclass(frozen=True)
class Redaction:
    """Whether guest-chosen and host-identifying strings may cross to an exporter.

    One flag rather than several, mirroring the agent framework's own
    ``ENABLE_SENSITIVE_DATA``: a deployment that has decided its telemetry pipeline may hold
    conversation content has decided it for all of it, and a per-field matrix would read as a
    guarantee this package cannot keep.
    """

    sensitive: bool = False

    def key(self, key: SandboxKey | None) -> dict[str, AttributeValue]:
        """The join columns for one key, plus its parts where the host allowed them."""
        if key is None:
            return {}
        recorded: dict[str, AttributeValue] = {
            KEY: hashed_key(key),
            # Beside the key, not instead of it: a per-call workload changes the key every
            # call, so this is the only column a conversation's records can be grouped by
            # once the scope and thread themselves are redacted.
            CONVERSATION: hashed_conversation(key),
        }
        # The call id is not held back with the rest. It is generated per call by the framework
        # rather than drawn from anyone's vocabulary, it names nobody on its own, and it is what
        # joins a record to the folder a sink landed that call's artifacts in — withholding it
        # would cost the correlation and protect nothing.
        if key.call_id:
            recorded[CALL_ID] = key.call_id
        if self.sensitive:
            recorded[SCOPE] = key.scope
            recorded[THREAD_ID] = key.thread_id
            recorded[AGENT_DIR] = key.agent_dir
        return recorded

    def keys(self, keys: Sequence[SandboxKey]) -> dict[str, AttributeValue]:
        """The join columns for a call, which may have touched more than one sandbox.

        One key renders exactly as :meth:`key` does, so the ordinary call is queryable the same
        way as every other event.  Two or more render as **aligned lists** — one entry per key,
        in order, on every attribute a single key would have carried — because a call reaching
        two sandboxes has no single key, and dropping the parts here would quietly suspend both
        redaction guarantees for exactly the calls that touched the most.
        """
        if not keys:
            return {}
        if len(keys) == 1:
            return self.key(keys[0])
        recorded: dict[str, AttributeValue] = {
            KEY: [hashed_key(key) for key in keys],
            CONVERSATION: [hashed_conversation(key) for key in keys],
        }
        if any(key.call_id for key in keys):
            recorded[CALL_ID] = [key.call_id for key in keys]
        if self.sensitive:
            recorded[SCOPE] = [key.scope for key in keys]
            recorded[THREAD_ID] = [key.thread_id for key in keys]
            recorded[AGENT_DIR] = [key.agent_dir for key in keys]
        return recorded

    def text(self, name: str, value: str | None) -> dict[str, AttributeValue]:
        """One attribute a guest or the host's own data chose the content of."""
        if value is None or not self.sensitive:
            return {}
        return {name: value}

    def texts(self, name: str, values: Iterable[str]) -> dict[str, AttributeValue]:
        """The list form of :meth:`text` — omitted whole rather than emptied."""
        if not self.sensitive:
            return {}
        listed = list(values)
        return {name: listed} if listed else {}


def sorted_values(members: Iterable[object]) -> list[str]:
    """A declared set as a sorted list of its values, so two records of one set compare equal."""
    return sorted(str(member) for member in members)


def without_none(attributes: Mapping[str, AttributeValue | None]) -> dict[str, AttributeValue]:
    """Drop the unset attributes: an exporter renders ``None`` as a value rather than a gap."""
    return {name: value for name, value in attributes.items() if value is not None}
