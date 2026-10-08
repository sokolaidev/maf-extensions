# sbx-template

One layer that lets an existing image run on [`maf-sandbox-docker-sbx`](../../packages/maf-sandbox-docker-sbx/README.md). The layer does three things:

- It installs `bash`, which `sbx` needs to start the sandbox.
- It installs util-linux, which gives the backend `unshare` and `mount` to bind the workspace for each command.
- It removes `/maf-sandbox`, the parent of the default storage base. The backend mounts the workspace there and refuses an image that already has the directory.

It works on bases that have `tdnf` or `apt-get` and whose util-linux `unshare` accepts `--map-user` and `--map-group`, which the backend needs. Ubuntu 22.04 (util-linux 2.37.2), Debian 12 (2.38.1), Ubuntu 24.04 and Azure Linux 3.0 all do. The build checks for both options, so a base without them fails to build with a message rather than producing an image that fails at acquire. The layer leaves the image running as root.

```bash
docker build -t bicep-sandbox:local images/bicep-sandbox
docker build --build-arg BASE=bicep-sandbox:local -t bicep-sandbox:sbx images/sbx-template
docker save -o bicep-sandbox-sbx.tar bicep-sandbox:sbx
sbx template load bicep-sandbox-sbx.tar
```

Then pass `bicep-sandbox:sbx` as the image. A base that already meets the requirements, such as [`graphviz-sandbox`](../graphviz-sandbox/), loads without this layer.

Removing `/maf-sandbox` also removes whatever the base kept there. For `bicep-sandbox` that is its fallback `bicepconfig.json`. The Bicep kind stages its own configuration into every call, so nothing that uses the kind reads the fallback.
