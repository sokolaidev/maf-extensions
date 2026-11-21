"""Export an editable draw.io diagram and a PNG preview in a Docker sandbox."""

# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#     "maf-sandbox>=0.55",
#     "maf-sandbox-docker>=0.26",
#     "maf-sandbox-drawio>=0.4",
#     "pillow>=13,<14",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from _scaffold import MEASURED, installed_versions, quoted
from maf_sandbox import (
    Isolation,
    SandboxAcquired,
    SandboxDisposed,
    SandboxObserver,
    SandboxRouter,
    ToolCallEnded,
    make_file_system_sink,
)
from maf_sandbox.maf import COMPLETED_TEXT, list_no_files, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_drawio import DrawioExport, make_drawio_tools
from PIL import Image

SOURCE = Path(__file__).with_name("request-flow.drawio")


class ExportObserver(SandboxObserver):
    """Track disposal of the sandbox that served the export."""

    def __init__(self) -> None:
        self.calls: list[ToolCallEnded] = []
        self.acquisitions: list[tuple[SandboxAcquired, int]] = []
        self.disposals: list[SandboxDisposed] = []

    def sandbox_acquired(self, event: SandboxAcquired) -> None:
        self.acquisitions.append((event, len(self.disposals)))

    def sandbox_disposed(self, event: SandboxDisposed) -> None:
        self.disposals.append(event)

    def tool_call_ended(self, event: ToolCallEnded) -> None:
        self.calls.append(event)

    def clean_call(self) -> tuple[ToolCallEnded, SandboxDisposed]:
        """Return the call and its successful post-acquire disposal."""
        if len(self.calls) != 1 or len(self.acquisitions) != 1:
            raise RuntimeError("Expected one draw.io call and one sandbox acquisition")
        [call] = self.calls
        [(acquired, start)] = self.acquisitions
        # Acquiring can dispose a stale instance under the same call ID.
        disposals = [item for item in self.disposals[start:] if item.call == call.call]
        if (
            call.failure is not None
            or call.unclean
            or acquired.refusal is not None
            or acquired.call != call.call
            or acquired.key.call_id != call.call
            or call.keys != (acquired.key,)
            or len(disposals) != 1
        ):
            raise RuntimeError("Draw.io call or per-call sandbox disposal was incomplete")
        [disposed] = disposals
        if (
            disposed.key != acquired.key
            or disposed.backend != acquired.backend
            or disposed.outcome != "gone"
            or disposed.failure is not None
        ):
            raise RuntimeError(f"Draw.io per-call sandbox disposal failed: {disposed}")
        return call, disposed


async def render(xml: str, image: str, output: Path) -> None:
    """Render the first page and keep both artifacts after confirmed cleanup."""
    observer = ExportObserver()
    backend = await DockerSandboxBackend.create(
        DockerSandboxConfig(
            memory="1g",
            cpus=2,
            pids_limit=256,
            cap_drop_all=True,
            cap_add=("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"),
        )
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER, observer=observer)
    scope, thread = "drawio-png", uuid4().hex
    context = make_caller_context(list_no_files, lambda: scope, lambda: thread)

    output.mkdir(parents=True, exist_ok=False)
    try:
        async with router.scope(scope, thread) as disposal:
            [create_drawio] = make_drawio_tools(
                router,
                "diagram-author",
                context,
                make_file_system_sink(output),
                image=image,
                export=DrawioExport(formats=("png",), pages=(1,)),
                exec_timeout_seconds=120,
            )
            reply = await create_drawio.func(xml=xml)
            texts = [item.text or "" for item in reply]
            if texts[:2] != [COMPLETED_TEXT, "Result: created"]:
                raise RuntimeError(quoted("\n".join(texts)))
            call, disposed = observer.clean_call()
        if disposal.undisposed is not None:
            raise RuntimeError(f"Draw.io scope cleanup failed: {disposal}")

        drawio = (output / "diagram.drawio").read_bytes()
        png = (output / "diagram-1.png").read_bytes()
        with Image.open(BytesIO(png)) as preview:
            preview.load()
            if preview.format != "PNG" or not drawio:
                raise RuntimeError("The export must contain an editable diagram and a PNG")
            width, height = preview.size

        evidence = {
            "tool": call.tool,
            "call": call.call,
            "seconds": call.seconds,
            "unclean": call.unclean,
            "call_disposal": {
                "call": disposed.call,
                "outcome": disposed.outcome,
                "failure": disposed.failure,
            },
            "undisposed": disposal.undisposed is not None,
            "width": width,
            "height": height,
            "drawio_sha256": hashlib.sha256(drawio).hexdigest(),
            "png_sha256": hashlib.sha256(png).hexdigest(),
        }
        print(f"{MEASURED}{json.dumps(evidence)}")
    except BaseException:
        shutil.rmtree(output)
        raise


def main() -> None:
    """Read the input diagram and run one export."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?", default=SOURCE)
    parser.add_argument("--image", default="maf-drawio-export:local")
    parser.add_argument("--output", type=Path, required=True, help="a new output directory")
    args = parser.parse_args()
    print(installed_versions())
    asyncio.run(render(args.source.read_text(encoding="utf-8"), args.image, args.output))
    print(f"Editable diagram and PNG saved to {args.output.resolve()}")


if __name__ == "__main__":
    main()
