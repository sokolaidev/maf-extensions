"""Sandboxed Terraform and OpenTofu validation for Microsoft Agent Framework."""

import warnings as _warnings

from ._spec import TERRAFORM_TOOL_NAMES, TerraformEngine, terraform_sandbox_spec
from ._tool import make_terraform_tools

__all__ = [
    "TERRAFORM_TOOL_NAMES",
    "MafSandboxTerraformExperimentalWarning",
    "TerraformEngine",
    "make_terraform_tools",
    "terraform_sandbox_spec",
]


class MafSandboxTerraformExperimentalWarning(UserWarning):
    """Warning category for the experimental Terraform workload package."""


def _warn_experimental() -> None:
    try:
        _warnings.warn(
            "maf_sandbox_terraform is experimental and may change or be removed in future "
            "versions without notice.",
            category=MafSandboxTerraformExperimentalWarning,
            stacklevel=2,
        )
    except MafSandboxTerraformExperimentalWarning:
        pass


_warn_experimental()
