"""Docker CodeAct samples require GET-only PyPI access before any model call."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    EgressRule,
    SandboxCapabilityNotSupported,
    SandboxRouter,
)
from maf_sandbox_docker import DockerSandboxBackend

SAMPLES = (
    "06_docker_codeact",
    "08_docker_codeact_files",
    "16_docker_codeact_outputs_store",
    "19_autogen_docker_codeact",
)
ROOT = Path(__file__).resolve().parents[1]


def load_sample(name, monkeypatch):
    directory = ROOT / "samples" / name
    monkeypatch.syspath_prepend(str(directory))
    monkeypatch.delitem(sys.modules, "_scaffold", raising=False)
    spec = importlib.util.spec_from_file_location(f"egress_{name}", directory / "agent.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules.pop("_scaffold", None)
    return module


class AdmissionReached(Exception):
    pass


@pytest.mark.parametrize("name", SAMPLES)
@pytest.mark.parametrize("supports_methods", [True, False])
def test_sample_admits_only_a_backend_enforcing_get_on_pypi(name, supports_methods, monkeypatch):
    module = load_sample(name, monkeypatch)
    monkeypatch.setenv("MAF_EGRESS_PROXY_IMAGE", "sample-proxy:test")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://model.invalid")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_MODEL", "test-model")
    configs = []
    specs = []
    ensure = SandboxRouter.ensure_can_serve

    def backend(config):
        configs.append(config)
        result = DockerSandboxBackend(config)
        if not supports_methods:
            result._declarations = replace(
                result.declarations,
                capabilities=result.declarations.capabilities - {Capability.EGRESS_METHODS},
                egress_method_tokens=frozenset(),
            )
        return result

    def admit(router, spec):
        specs.append(spec)
        ensure(router, spec)
        raise AdmissionReached

    monkeypatch.setattr(module, "DockerSandboxBackend", backend)
    monkeypatch.setattr(SandboxRouter, "ensure_can_serve", admit)
    expected = AdmissionReached if supports_methods else SandboxCapabilityNotSupported
    with pytest.raises(expected):
        asyncio.run(module.run())

    [config] = configs
    [spec] = specs
    assert config.egress_proxy_image == "sample-proxy:test"
    assert not config.allow_private_http
    assert spec.egress is Egress.ALLOWLIST
    assert spec.egress_allow == (EgressRule("pypi.org", methods=("GET",)),)
    assert Capability.EGRESS_METHODS in spec.required_capabilities


@pytest.mark.parametrize("name", SAMPLES)
def test_missing_proxy_configuration_refuses_before_backend_or_model_creation(name, monkeypatch):
    module = load_sample(name, monkeypatch)
    monkeypatch.delenv("MAF_EGRESS_PROXY_IMAGE", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://model.invalid")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_MODEL", "test-model")

    def unexpected_backend(config):
        pytest.fail("missing proxy configuration reached backend construction")

    monkeypatch.setattr(module, "DockerSandboxBackend", unexpected_backend)
    assert asyncio.run(module.run()) == 2
