# Generate a storage deployment script with Microsoft Learn and shell validation

An agent consults the public Microsoft Learn MCP server, writes an Azure CLI Bash script that creates a resource group and StorageV2 account, and repairs analyzer findings through `maf-sandbox-shell` on Docker. The host validates the final stored bytes and retains `deploy.sh` for inspection. The sample never executes the deployment script or creates Azure resources; static validation does not prove deployment success.

## Run

Configure `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_CHAT_MODEL`, sign in with `az login`, and start Docker. Build the validation image from the repository:

```bash
uv run --locked python scripts/build_wolfi_python.py -t maf-shell:live images/shell-sandbox
export SHELL_SANDBOX_IMAGE=maf-shell:live
uv run samples/21_shell_storage/agent.py --output deploy.sh
```

The output must not already exist. Review the generated script before executing it separately; it requires `RESOURCE_GROUP`, `AZURE_LOCATION` and `STORAGE_ACCOUNT_NAME` from its caller. The host authenticates to the model through Azure Identity. Microsoft Learn MCP requires no credential. The validation guest has closed egress and receives only the submitted script, never host credentials.

The live check requires an actual Microsoft Learn search result, successful Bash validation of the exact output hash, complete sandbox disposal and successful artifact read-back. It does not assess Azure CLI runtime behavior or cloud permissions. Logs quote model and tool text so neither can impersonate host measurements.
