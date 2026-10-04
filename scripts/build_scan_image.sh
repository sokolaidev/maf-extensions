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
  diagram)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target images/diagram-sandbox
    ;;
  drawio-sandbox)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target images/drawio-sandbox
    ;;
  drawio-export)
    docker build --pull --platform linux/amd64 -t maf-image-scan:target images/drawio-export
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
