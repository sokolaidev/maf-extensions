"""Export an editable draw.io diagram and a PNG preview in a Docker sandbox."""

# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#     "maf-sandbox>=0.54",
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


async def render(xml: str, image: str, output: Path) -> None:
    """Render the first page and keep both artifacts after confirmed cleanup."""
    calls: list[ToolCallEnded] = []

    class Observer(SandboxObserver):
        def tool_call_ended(self, event: ToolCallEnded) -> None:
            calls.append(event)

    backend = await DockerSandboxBackend.create(
        DockerSandboxConfig(
            memory="1g",
            cpus=2,
            pids_limit=256,
            cap_drop_all=True,
            cap_add=("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"),
        )
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER, observer=Observer())
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
        if disposal.undisposed or len(calls) != 1 or calls[0].failure or calls[0].unclean:
            raise RuntimeError(f"Draw.io call or sandbox cleanup failed: {calls}, {disposal}")

        drawio = (output / "diagram.drawio").read_bytes()
        png = (output / "diagram-1.png").read_bytes()
        with Image.open(BytesIO(png)) as preview:
            preview.load()
            if preview.format != "PNG" or not drawio:
                raise RuntimeError("The export must contain an editable diagram and a PNG")
            width, height = preview.size

        evidence = {
            "tool": calls[0].tool,
            "seconds": calls[0].seconds,
            "unclean": calls[0].unclean,
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
