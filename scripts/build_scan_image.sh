#!/usr/bin/env bash
set -euo pipefail

case "$1" in
  bicep)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target images/bicep-sandbox
    ;;
  bicep-prepared)
    docker build --pull --platform linux/amd64 -t maf-image-scan:base images/bicep-sandbox
    uv run --locked python scripts/build_bicep_prepared_image.py --base-image maf-image-scan:base --tag maf-image-scan:target
    ;;
  sbx-bicep)
    docker build --pull --platform linux/amd64 -t maf-image-scan:base images/bicep-sandbox
    docker build --platform linux/amd64 --build-arg BASE=maf-image-scan:base -t maf-image-scan:target images/sbx-template
    ;;
  graphviz)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target images/graphviz-sandbox
    docker run --rm --network none maf-image-scan:target sh -ec 'printf "digraph { a -> b }" | dot -Tpng -o /tmp/render.png; test -s /tmp/render.png'
    ;;
  drawio-sandbox)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target images/drawio-sandbox
    docker run --rm --network none maf-image-scan:target sh -ec 'python3 --version; printf "digraph { a -> b }" | dot -Tpng -o /tmp/render.png; test -s /tmp/render.png'
    ;;
  drawio-export)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target images/drawio-export
    docker run --rm -i --network none --entrypoint python3 maf-image-scan:target - <<'PY'
import json
import runpy
import time
from pathlib import Path

export = runpy.run_path('/opt/maf-drawio/export.py')['export_document']
xml = '<mxfile><diagram><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" parent="1" vertex="1" value="Smoke" style="fontFamily=DejaVu Sans;"><mxGeometry x="10" y="10" width="120" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>'
export(xml, {'pages': [], 'formats': ['png', 'svg', 'jpg'], 'scale': 1, 'jpeg_quality': 90, 'transparent': False}, time.monotonic() + 120)
assert json.loads(Path('exports.json').read_text())['files'] == ['diagram-1.png', 'diagram-1.svg', 'diagram-1.jpg']
assert Path('diagram-1.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
print('Offline PNG, SVG and JPEG exports passed')
PY
    ;;
  terraform-random)
    uv run --locked python images/terraform-sandbox/build_image.py --engine terraform --profile random --tag maf-image-scan:target
    ;;
  opentofu-random)
    uv run --locked python images/terraform-sandbox/build_image.py --engine opentofu --profile random --tag maf-image-scan:target
    ;;
  terraform-prepared)
    uv run --locked python images/terraform-sandbox/build_image.py --engine terraform --profile builtin --tag maf-image-scan:base
    docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-image-scan:base --build-arg MANIFEST=dependencies.terraform.json -f images/terraform-sandbox/prepared.Dockerfile -t maf-image-scan:target images/terraform-sandbox
    ;;
  opentofu-prepared)
    uv run --locked python images/terraform-sandbox/build_image.py --engine opentofu --profile builtin --tag maf-image-scan:base
    docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-image-scan:base --build-arg MANIFEST=dependencies.opentofu.json -f images/terraform-sandbox/prepared.Dockerfile -t maf-image-scan:target images/terraform-sandbox
    ;;
  hyperlight)
    uv run --locked python scripts/build_hyperlight_aks_image.py --require-clean --output "$RUNNER_TEMP/hyperlight-image" --tag maf-image-scan:target
    ;;
  egress-proxy)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy
    ;;
  *) echo "Unknown image scan profile: $1" >&2; exit 2 ;;
esac

case "$1" in
  terraform-*|opentofu-*)
    docker run --rm -i --network none --read-only --tmpfs /tmp:rw,exec,size=256m maf-image-scan:target /usr/local/bin/python3 - < images/terraform-sandbox/test_runner.py
    ;;
esac
