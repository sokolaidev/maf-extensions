# Root probe fixture

This image gives uid/gid `10001:20001` ownership of `/usr/local/bin` and places it ahead of `/usr/bin` in `PATH`. The live test plants a `test` executable there as the guest, proves the unqualified root command executes it, then checks acquisition and file writes bypass it. It exercises both fresh and cached prerequisite checks, quoted paths, and rejection of ordinary and dangling symlinks. Each case disposes its network-isolated container in `finally`.

Run from the repository root on Windows under the account that owns the WSLC engine:

```powershell
wslc image build -t maf-sandbox-wslc-path-shadow:ci packages/maf-sandbox-wslc/tests/fixtures/path-shadow
$env:MAF_SANDBOX_WSLC_E2E_PATH_SHADOW_IMAGE = 'maf-sandbox-wslc-path-shadow:ci'
uv run pytest packages/maf-sandbox-wslc/tests/test_wslc_root_probe.py -q
```
