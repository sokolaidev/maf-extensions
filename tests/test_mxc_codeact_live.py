"""Native CodeAct workflows over the pinned MXC directory transport on hosted runners."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from maf_sandbox import (
    CallerContext,
    Cleanup,
    LandedArtifact,
    OutputSink,
    ProgramRequirements,
    SandboxRouter,
    TransferLimits,
)
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools
from maf_sandbox_mxc._backend import MxcSandboxBackend
from maf_sandbox_mxc._config import MxcSandboxConfig, startup_identity
from maf_sandbox_mxc.host_call import digest
from maf_sandbox_mxc.shared_store import Limits, ScratchLimits

pytestmark = pytest.mark.skipif(
    not os.environ.get("MXC_ADAPTER_HELPER"), reason="native MXC opt-in"
)


def text(answer):
    return answer if isinstance(answer, str) else "\n".join(str(item.text) for item in answer)


@pytest.mark.parametrize("persistent", [False, True])
def test_native_codeact_text_and_csv_chart(tmp_path, persistent):
    helper = Path(os.environ["MXC_ADAPTER_HELPER"])
    startup = Path(os.environ["MXC_ADAPTER_STARTUP"])
    config = MxcSandboxConfig(
        helper,
        startup,
        tmp_path / "store",
        "mxc-1.0.1-directory-closed-v1",
        digest(helper),
        startup_identity(startup),
        Limits(32 * 1024**3, 64 * 1024**3, result_bytes=32 * 1024**2),
        ScratchLimits(6 * 1024**3, 100_000, 12 * 1024**3),
        persistent=persistent,
    )

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        router = SandboxRouter(
            [backend], min_cleanup=Cleanup.RECLAIM if persistent else Cleanup.DISPOSE
        )
        store = InMemoryStore({"input.csv": "name,value\na,2\nb,4\n"})

        async def listing(source):
            return await source.list()

        context = CallerContext(
            current_scope=lambda: "native-codeact",
            current_thread_id=lambda: "thread",
            list_files=listing,
        )
        collected = []

        async def deliver(artifact):
            collected.append(artifact)
            return LandedArtifact(artifact.name, "native-artifact")

        bounds = TransferLimits(16 * 1024**2, 16 * 1024**2, 64)
        options: dict[str, Any] = dict(
            exec_timeout_seconds=90,
            program=ProgramRequirements(max_program_bytes=65536),
            files_in=bounds,
            files_out=bounds,
            host_tools_enabled=False,
        )
        try:
            tools = make_codeact_tools(router, "text", context, **options)
            assert tools, "the native channel was not admitted"
            tool = tools[0]
            function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
            answer = await function(
                code=(
                    "import numpy as np, pandas as pd\n"
                    "values=pd.Series(np.array([2,4,6]))\nprint('mean:', values.mean())"
                )
            )
            assert "mean: 4.0" in text(answer), text(answer)
            answer = await function(code="print('before failure')\nraise ValueError('bad input')")
            assert "before failure" in text(answer) and "ValueError: bad input" in text(answer)
            tools = make_codeact_tools(
                router,
                "files",
                context,
                file_store=store,
                output_sink=OutputSink(deliver),
                outputs=CodeactOutputs.DECLARED,
                **options,
            )
            assert tools
            tool = tools[0]
            function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
            answer = await function(
                code=(
                    "import pandas as pd\nimport matplotlib\nmatplotlib.use('Agg')\n"
                    "import matplotlib.pyplot as plt\n"
                    "data=pd.read_csv('input.csv')\n"
                    "plt.bar(data['name'], data['value'])\nplt.savefig('chart.png')\n"
                    "plt.close()\nprint('total:', data['value'].sum())"
                ),
                files=["input.csv"],
                outputs=["chart.png"],
            )
            assert "total: 6" in text(answer), text(answer)
            assert len(collected) == 1 and collected[0].content.startswith(b"\x89PNG\r\n\x1a\n")
        finally:
            await backend.aclose()

    asyncio.run(check())
