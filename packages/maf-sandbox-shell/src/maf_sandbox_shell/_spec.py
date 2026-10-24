"""Host-selected analyzers and the boundary required for static shell validation."""

import re
from dataclasses import dataclass
from enum import StrEnum

from maf_sandbox import (
    Capability,
    Cleanup,
    Egress,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxSpec,
)


class ShellFlavor(StrEnum):
    """The analyzer family enabled by the host."""

    BASH = "bash"
    PWSH = "pwsh"


class ShellDialect(StrEnum):
    """The fixed POSIX-family grammars supported by the Bash flavor."""

    BASH = "bash"
    SH = "sh"
    DASH = "dash"


class ShellSeverity(StrEnum):
    """Diagnostic thresholds, from the most restrictive to the most inclusive."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    STYLE = "style"


SHELL_TOOL_NAMES: frozenset[str] = frozenset({"bash_validate", "pwsh_validate"})
SEVERITIES = tuple(ShellSeverity)


@dataclass(frozen=True, slots=True)
class ShellPolicy:
    """Immutable host policy; inline suppressions remain honored and reported.

    ``default_dialect`` applies only to shebangless .sh files. PowerShell dependencies are
    supplied by the selected image's inventory, never by tool arguments.
    """

    minimum_severity: ShellSeverity = ShellSeverity.STYLE
    excluded_rules: tuple[str, ...] = ()
    default_dialect: ShellDialect = ShellDialect.BASH

    def __post_init__(self) -> None:
        object.__setattr__(self, "minimum_severity", ShellSeverity(self.minimum_severity))
        object.__setattr__(self, "default_dialect", ShellDialect(self.default_dialect))
        if (
            type(self.excluded_rules) is not tuple
            or len(self.excluded_rules) > 256
            or any(
                type(rule) is not str
                or not re.fullmatch(r"SC[0-9]{4,6}|PS[A-Za-z][A-Za-z0-9]{1,100}", rule)
                for rule in self.excluded_rules
            )
            or len(set(self.excluded_rules)) != len(self.excluded_rules)
        ):
            raise ValueError("excluded_rules must be a bounded tuple of distinct rule IDs")


def shell_sandbox_spec(
    image: str | None = None,
    image_id: str | None = None,
    *,
    flavor: ShellFlavor,
) -> SandboxSpec:
    """Require a fresh closed-network POSIX sandbox with disposal after every call."""
    return SandboxSpec(
        kind=ShellFlavor(flavor).value,
        image=image,
        image_id=image_id,
        work_dir=None,
        egress=Egress.CLOSED,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN}),
        requires_os_family=OsFamily.POSIX,
        min_isolation=Isolation.CONTAINER,
        isolation_scope=IsolationScope.CALL,
        min_cleanup=Cleanup.DISPOSE,
        confined_to_guest_call_path=False,
    )
