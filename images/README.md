# Published container images

Use each image's consumer guide for its purpose, pull and verification instructions, runtime interface and support boundaries. A visible registry tag does not establish release completion; check the [release status and evidence](https://sokolaidev.github.io/maf-extensions/) before use.

| Image | Purpose | Consumer documentation |
|---|---|---|
| `ghcr.io/sokolaidev/maf-extensions/graphviz` ([0.1.2](https://github.com/sokolaidev/maf-extensions/releases/tag/image-graphviz-v0.1.2)) | Render Graphviz DOT to PNG on Linux/amd64 | [Graphviz image guide](graphviz-sandbox/README.md) |

See the [security documentation](../docs/security/README.md) for scan coverage, artifact verification and tested configurations, and the [image release contract](../docs/security/container-image-releases.md) for publication and monitoring requirements.

The [dependency inventory](DEPENDENCIES.md) identifies authoritative manifests, scheduled updates, compatibility lines and manually maintained exceptions for every image family.
