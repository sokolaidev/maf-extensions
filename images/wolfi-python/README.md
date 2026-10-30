# Temporary Python security replacement

Python-bearing Wolfi runtimes use locally built Python 3.13.18 APKs until Wolfi supplies a qualified replacement for CVE-2026-115557. The [builder Dockerfile](Dockerfile) pins the Wolfi base, Melange package version and SHA-256 of the Wolfi recipe archive. [configure.py](configure.py) selects CPython's signed `v3.13.18` release commit `0d4bd512fc429e3d4eacdd113c975e161f21644d`, retaining the pinned Wolfi recipe's OpenSSL 4 patches, normal package splits and upstream regression gate. The unused `python-3.13-privileged-netbindservice` subpackage is omitted; none of these consumers installs its file-capability-enabled interpreter. Source pins require maintainer review; Dependabot continues to own the base image digest.

Use the shared wrapper wherever a consumer previously used `docker build`:

```sh
uv run --locked python scripts/build_wolfi_python.py --platform linux/amd64 -t maf-drawio:local images/drawio-sandbox
```

The wrapper builds the Melange toolchain, creates a disposable privileged Linux/amd64 container for Bubblewrap, and exports its signed APKs into a temporary Docker build context. It mounts neither host directories nor the Docker socket into that container and removes it on success or failure. The wrapper builds both the packages and consumer for `linux/amd64`, including when Docker defaults to another architecture, and rejects conflicting `--platform` arguments before compilation. This needs a Docker engine permitting privileged build containers and capable of running Linux AMD64 containers; native ARM64 images are not provided by this temporary override. The consumer installs the main and base Python APKs through APK, verifies the runtime version and TLS-PSK support, and removes the temporary signing public key. The private key stays inside the disposable builder. The final image has no privileged-container requirement from this packaging step.

The nine consumers are shell, document, drawio, drawio-export, hyperlight-aks-probe, and the four Terraform/OpenTofu profiles. Terraform and Hyperlight build helpers call the wrapper automatically. Bicep, Graphviz and egress-proxy do not consume this replacement. Changes to the recipe or helper select all nine consumers for fresh image security checks and are included in retained build inputs. The normal SBOM, High/Critical vulnerability, runtime and publication approval gates still apply.

For local investigation, `uv run --locked python scripts/build_wolfi_python.py --output dist/python-apks` exports the packages into a new empty directory. A subsequent local build can use `docker build --build-context python-packages=dist/python-apks ...`; that reuse is diagnostic only, while release and CI builds invoke the wrapper themselves. CI job budgets include an additional 30 minutes per fresh Python compilation, including all four builds in the Terraform live job. Package-build logs include Wolfi's SSL, hashing and HTTP regression results. A successful package build does not establish consumer qualification or a clean final-image scan.

Remove the override after the upstream Wolfi Python APK contains the security fix and all nine consumers pass runtime qualification and fresh scans using it. Remove the shared helper/context, restore direct builds and remove this directory together; do not leave old package pins or an unused build path behind.
