# Controlled Terraform dependency preparation

> Design record for [#1249](https://github.com/sokolaidev/maf-extensions/issues/1249), 2026-09-14. Select host-controlled preparation before changing any portable egress contract.

## Choice

Prepare provider ZIP mirrors and separate, reviewed module bundles outside the validation sandbox. A trusted operator supplies the manifest; configuration, providers, agents and guests cannot submit requests to the preparer. The output is an input to an immutable image build and explicit file staging, not a running network service. Validation keeps CLOSED egress, the existing filesystem mirror configuration and call-scoped disposal. No provider or engine executable runs on the application host during preparation.

A network mirror would still need a guest-facing request-content boundary. Direct downloads would require TLS inspection plus bypass-resistant routing on Docker and WSLC, and measured request enforcement on ACAS. Neither belongs in this implementation. No portable spec field or capability is added, and method enforcement in #377 remains independent.

## Bounded authority

The manifest pins an engine, full provider source addresses, exact versions/platforms, complete ZIP SHA-256 digests and an operator-supplied provenance reference. Digests must come from independently trusted release metadata; downloading a checksum next to an artifact does not establish trust. The preparer verifies bytes, not signatures or the truth of the operator's provenance assertion. Provider source addresses are preserved in the packed filesystem mirror. Supplied lockfiles remain unchanged and the existing launcher makes them read-only.

Each artifact has one exact HTTPS URL and, optionally, an exact redirect chain. GitHub release downloads can instead authorize one server-issued redirect to a repository-ID-bounded release-assets path. Only that transfer may carry the server's signed query. The guest never receives the redirect or supplies its query. Discovery, Git transports, credentials, arbitrary HTTP requests and unrestricted CDN rules are refused.

Requests are fixed GETs with fixed headers and no body. URLs must already be canonical ASCII: lower-case DNS host, port 443 implicit, nonempty absolute path, no percent encoding, dot segments, duplicate separators, backslashes, userinfo, fragments or caller query. Every connection resolves and checks all addresses, connects to the selected public address without another DNS lookup, and verifies TLS against the approved hostname. Environment proxies are ignored. Redirects, bytes and wall time are bounded. A parent process bounds even DNS and stalled headers. Errors report fixed decision codes, never URLs, response bodies or signed tokens.

## Module boundary

Module bundles pin immutable revisions and archive digests separately from provider locks. Each approved bundle declares its archive subtree and complete local module graph. Preparation checks every HCL/JSON configuration sibling and every module edge against that graph, refuses remote/dynamic sources, cycles, escapes, ambiguous file precedence and unlisted configuration directories, and preserves file bytes. This first mechanism accepts already-local module graphs; converting arbitrary remote registry/Git graphs to local bundles requires a separately reviewed preparation step. It never resolves an undeclared remote child automatically.

Archives are processed as bounded data without extracting filesystem entries. Links, special files, traversal, case collisions, oversized expansion and invalid text are refused. Outputs contain the verified provider ZIPs, module text and a sanitized manifest receipt. The receipt identity includes the whole request policy and module graph. The host builds from a fresh empty-mirror base, uses the resulting immutable image ID and stages the module files explicitly. Output directory permissions alone are not an immutability boundary; image identity and trusted artifact storage are.

## Evidence required

Deterministic tests must exercise allowed transfers and receiver-accepted negative requests, neighboring paths, URL encodings, query/header/body injection, redirects, private addresses, resource bounds, archive corruption and graph mismatches. Real checks must download and verify complete provider/module artifacts and initialize and validate with both engines under CLOSED Docker egress. Missing artifacts and incompatible locks must remain incomplete. Record Docker adapter evidence separately from request transport tests; ACAS and WSLC require their own live qualification.

## Measured result

The [sanitized evidence](terraform-dependency-evidence.json) records complete public artifact downloads, the approved manifest hashes and immutable image IDs. Both runnable examples returned validation PASS with zero errors/warnings and formatting PASS. All 59 request/manifest/archive tests passed. Each of the six Docker cases passed for both engines; the native-lock controls were rerun after replacing a ZIP-only checksum fixture with real engine-generated locks containing the unpacked package hash. Supplied locks and source files remained unchanged. Direct HTTP and raw CONNECT controls succeeded against the receiving container over bridge networking, then failed in CLOSED adapter sandboxes. Docker reported network mode `none` and disposal after calls. This does not qualify ACAS/WSLC or arbitrary remote module graph conversion. The implemented contract and reproduction commands are in [the image guide](../../../images/terraform-sandbox/README.md#approved-dependency-preparation).
