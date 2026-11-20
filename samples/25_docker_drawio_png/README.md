# Export a draw.io diagram to PNG

Render [a three-node request flow](request-flow.drawio) in a disposable Docker sandbox. The same `create_drawio` call writes an editable `diagram.drawio` and a first-page preview, `diagram-1.png`, through the host's file sink. The host checks both saved files and decodes the PNG before reporting success.

## Run it

Use uv and Docker with Linux/amd64 containers. The diagram is bundled, so the sample needs no model or Azure credentials.

Build the [draw.io export image](../../images/drawio-export/) from the repository root, then run the sample against published packages:

```bash
uv run --locked python scripts/build_wolfi_python.py --platform linux/amd64 -t maf-drawio-export:local images/drawio-export
uv run --no-project samples/25_docker_drawio_png/agent.py --output samples/25_docker_drawio_png/out/preview
```

Open `diagram-1.png` in an image viewer and `diagram.drawio` in draw.io to continue editing. Both files are under the sample's `out/preview/` directory. Pass your own uncompressed `.drawio` file as the positional argument. Use a new `--output` directory for each run. To use a verified published image, follow the [image verification instructions](../../images/drawio-export/README.md#publication-and-consumer-verification) and pass its digest reference with `--image`.

`DrawioExport(formats=("png",), pages=(1,))` selects native PNG output for the first page while retaining the whole editable diagram. The dedicated Docker backend grants the export coordinator the capabilities it needs. Each render runs in its own sandbox with closed egress. [Draw.io host configuration](../../docs/sandbox/kinds/drawio.md#host-configuration) describes the format and page options.

[Verify (live)](../../.github/workflows/verify-live.yml) retains the exported artifacts and call timing for each successful run.
