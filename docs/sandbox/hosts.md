# Host responsibilities

The host chooses storage, credentials, tool registrations and information-flow policy. Core checks declared contracts and controls when data crosses the sandbox boundary.

Source tools return content to the model. Destination tools accept data from it. Tool declarations describe those roles; labels on individual content items describe the returned data. The host's middleware reads both.

![Source tools declare integrity and return content items with integrity and confidentiality labels. The model receives those items through information-flow middleware. Destination tools declare the maximum confidentiality they accept. The middleware checks the conversation and content labels before a destination call. A tool can be both a source and a destination; each role is declared separately.](assets/information-flow.svg)

The [information-flow guide](information-flow.md) defines those labels and the four result items. This page covers the host wiring that gives them meaning.

## Where artifacts land

A `LAND` output leaves the sandbox through an `OutputSink`. A `CONSUME` output goes back into the kind, which performs its own bounded read. Both roles undergo the [file and transfer checks](capabilities.md#declaring-outputs).

| Type | Purpose |
|---|---|
| `Artifact` | Bytes, public name, kind, optional media type and host-minted call ID sent to the sink |
| `OutputSink` | Host callback plus name-normalization and per-call settings |
| `LandedArtifact` | Echoed name, model-safe `display` text and an optional host-only `handle` |

Core calls the host's `deliver` callback. The callback chooses a file store, object store, local directory or other destination. Core never guesses where a host wants artifacts stored.

Keep credentials, signed URLs and internal storage details out of `display`. A `handle` is not automatically printed, but the host still controls how it is later used.

![Output collection first validates all names, sink requirements and call identity. It then checks every declared file and reads all landing outputs within the limits. Only after the complete set passes does it call the host sink for each artifact. A failure before delivery leaves the sink untouched; a sink callback failure can leave earlier deliveries in place.](assets/host-output-landing.svg)

Collection validates names and required sink settings before reading guest files. It checks all declared files, then reads all `LAND` bytes and rechecks their sizes. No sink receives bytes until the complete set passes.

Delivery is sequential and per artifact. A callback failure can leave earlier deliveries in place. There is no rollback or batch-commit contract. SDK buffering can also exceed the accepted-transfer limit before core receives the bytes.

### Names and destination safety

Artifact names must be valid relative names. Empty segments, `.` or `..` segments, backslashes, control characters, invalid UTF-8 and excessive byte lengths are refused.

NFC normalization is the default. `NameNormalization.NONE` preserves spelling, but collision checks still compare NFC-normalized, lowercased names. Case-only collisions within one collection are refused; the comparison uses `lower`, not Unicode `casefold`.

`portable_file_name` is an optional destination helper. It rewrites Windows-invalid characters, reserved names and trailing periods or spaces. It is not applied automatically and does not replace collection collision checks.

`make_file_system_sink` confines files under a host-selected root and rejects linked ancestry. Existing destinations are refused unless replacement is explicitly enabled. The host owns the root's access permissions and sharing policy.

### Agent file stores

`make_file_store_sink` writes UTF-8 text under a host-minted call ID and artifact name. It does not overwrite existing files. Invalid text raises `SandboxLandingNotText`; an existing name raises `SandboxLandingExists`. Other store failures propagate.

The sink declares `per_call=True`, so collection requires a call ID before reading outputs. The underlying store remains responsible for its own confinement.

Pass the store's `FileStoreProvenance` record to the sink. It records the output as untrusted before writing. A failed write keeps that conservative record.

Use `sandbox_outputs_read_tools` to expose named listing and reading tools for the output store. It creates no write tool. The host must classify these tools before use; withholding direct guest output does not prevent an explicit read tool from returning the same bytes.

## Outbound confidentiality

The host supplies `outbound_max_confidentiality` in its own classification vocabulary. Core places it in the tool's `max_allowed_confidentiality` declaration when the tool has an outbound channel. It does not invent a cap or order host-specific values.

An outbound channel includes unrestricted networking, a nonempty allowlist, landed outputs, opted-in attached authority, or host-tool activity identified through `also_carries_out`. An empty allowlist alone carries nothing out.

`HostToolAggregate.outbound_caps` contains every registered sink cap as an unordered set. The host must reconcile them; core cannot infer which arbitrary host string is strictest. CodeAct also accounts for undeclared host tools.

An output sink and explicit `declarations=` cannot be supplied together to `sandboxed_tool`. This prevents manual declarations from bypassing sink-derived policy. [Result confidentiality](#classify-derived-tool-results) is a separate setting.

### Prevent cross-conversation storage relays

Give each conversation a separate artifact destination and output-store namespace. Refusing overwrites does not stop one conversation reading another's output or learning that a name is occupied.

Keep writable output storage separate from input stores that other conversations can read. A registered sink tool and source tool can also form a shared-storage path between guests. Each declaration describes one tool; it cannot establish that the pair is safely partitioned.

The host must review those connections. Core cannot inspect arbitrary callbacks to discover their storage or network destinations.

<a id="host-tools-calling-outward"></a>

## Calling host tools

Guest code can call only functions explicitly registered in `HostToolRegistry`. Their bodies run in the host process with its authority. These nested calls bypass the agent's middleware chain; the chain sees the outer sandbox tool result.

![Guest code requests a host tool by name and arguments. The host registry checks registration, captured declarations, allowed identity, call and response budgets, and argument binding. A user-identity call also requires a host mint. Only then can the registered body run in the host process. Its response returns to the guest. Agent middleware surrounds the outer sandbox tool, not each nested host-tool call.](assets/host-tool-gates.svg)

An empty registry exposes nothing. Registration emits a suppressible warning about middleware bypass. A declaration describes authority; it does not reduce the privileges of a Python function.

`@sandbox_tool(source=..., sink=..., identity=...)` requires an explicit answer for each role. `None` means the function has no such role. A source uses `SourceIntegrity`; a sink uses the host's confidentiality vocabulary; identity uses `Identity`.

| Registry control | Behavior |
|---|---|
| `require_declared=True` | Refuse undeclared tools at registration and call time |
| Default `require_declared=False` | Treat an undeclared tool as an untrusted source with app authority; set `has_undeclared` |
| `allowed_identities` | Default permits `APP`; the host must opt into `USER`; `identity=None` needs no authority |
| Captured declarations | Read once at registration; later decorator changes do not replace them |
| Aggregate | Seals registration; reports weakest source integrity, all sink caps and whether user approval is required |
| Router denials | Refuse `HOST_TOOLS` or named identities across every workload |

Only source tools contribute to aggregate source integrity. A registry with no sources has no source-integrity opinion. Sink-only and pure tools do not lower that result.

Arguments are bound against the Python signature in the host before the body runs. An unreadable signature is refused. The body remains responsible for value validation and the effects it performs.

The host enforces the per-run call cap and response budgets before invoking the body. Response framing consumes the byte budget too. These checks do not predict the size of an arbitrary result or roll back a body that has already acted.

### Transport over EXEC

`host_tool_calls_over_exec` supervises a detached guest program. Requests and responses travel through files using `EXEC`, `FILES_IN` and `FILES_OUT`; `FILES_LIST` is not required.

The generated guest shim is a convenience. Guest code can change or bypass it, so it carries no enforcement authority. The registry performs the checks in the host.

ACAS and Docker support this transport. WSLC lacks the required output transfer. The program must outlive the launch request and remain observable from later requests; backend tests establish that behavior.

Each `HostToolRun` has a stable `run_id` for attribution. The supervisor cleans its transport directory; the kind collects artifacts before the wrapper cleans the call directory. [Tool-call lifetime](tool-call.md) defines process cleanup and its limits.

## Identity — whose authority sandbox work carries

Control-plane credentials, host-tool authority and guest-attached authority are separate mechanisms.

![Backend management credentials stay in the host and authorize control-plane operations. Registered host tools also run in the host, using application authority or a per-run user credential supplied by a host mint. Attached authority is exercised from the sandbox through declared platform channels, with explicit scope and retention checks. The core attached-authority declaration does not discover ACAS group-configured identity.](assets/attached-authority.svg)

| Mechanism | Where credentials are used | Host responsibility |
|---|---|---|
| Backend control plane | Host | Supply acquire and cleanup credentials for the trusted target |
| `Identity.APP` host tool | Host tool body | Register functions with appropriate application privileges |
| `Identity.USER` host tool | Host tool body | Permit the identity and mint authority for the run |
| Core attached authority | Sandbox's declared platform channel | Opt in, bound sharing and lifetime, and use a backend that enforces them |
| ACAS group identity | Service-configured sandbox group | Route workloads to a group with the intended authority |

[ACAS credentials](backends/acas-credentials.md) define request binding, cleanup grants and credential lifetime. Those credentials remain in the host and are independent of guest identity.

### User authority in host tools

`HostToolRegistry(mint_user_identity=...)` calls the host with the run's `run_id`. It passes the first usable result to `USER` tool bodies as `user_identity` and caches it for that run. Failed mint attempts are not cached.

The guest cannot supply `user_identity`; that reserved argument is refused before minting. Where a mint is configured, registration refuses a `USER` function that cannot accept the parameter. Without a mint, a permitted `USER` tool remains declarable but its calls are refused.

Any registered `USER` tool makes the outer surface require approval. The host still owns credential scope and expiry. Returning the same long-lived credential for every run satisfies the callback shape but provides no per-run restriction.

Guest HTTP workloads on Docker and WSLC can use the external credential gateway below. `Identity.USER` remains a host-tool identity; it does not configure that gateway.

### Core attached-authority contract

`AttachedIdentity(scope, auto_delete_seconds, channels)` describes authority a backend promises to enforce. `NO_ATTACHED_IDENTITY` claims no attachment within this contract; it does not inspect deployment configuration.

The scope order is `NONE`, `PER_SANDBOX`, `PER_SCOPE`, `SHARED`, from narrowest to widest. The host's `max_identity_scope` defaults to `NONE`. Both the host and workload must permit the backend's sharing scope.

An opted-in workload requires `ATTACHED_IDENTITY`, a non-`NONE` `max_identity_scope` and positive integer `max_identity_retention_seconds`. Capability and backend attachment declarations must agree. An ordinary workload is refused against a backend declaring attached authority.

`AuthorityChannel.EGRESS_HEADER` is the supported channel. Each concrete destination has an `EgressRule` with an exact `authority` audience. The audience is an opaque nonempty string without whitespace or control characters; it is never inferred from the hostname.

The backend must inject the configured principal's bearer header only for those destinations and audiences. It must prevent forwarding authority elsewhere, preserve ordinary network confinement and expose no additional undeclared token endpoint or connector. Declared and requested channels must match.

`auto_delete_seconds` is a platform-enforced maximum from sandbox creation until that sandbox loses the ability to spend authority. Guest activity cannot extend it. The bound must hold after every host disappears and fit within the workload's retention limit.

An idle timeout, token lifetime or [operator retention sweep](operations.md) alone is insufficient. A shared principal can outlive a sandbox, but the sandbox's use of it must end within the bound. `PER_SCOPE` also requires principal exclusivity across that whole scope.

Docker and WSLC advertise this contract when configured with `credential_gateway`. ACAS supports managed identity through group configuration without ARM discovery or drift polling. Ordinary ACAS specs can reach that configured identity without core opt-in, audience or retention checks. See [sandbox group identity](backends/acas.md#sandbox-group-identity).

### Credentials for guest HTTP requests

Configure `CredentialGateway(provider, max_lifetime_seconds=300)` from `maf_sandbox.credentials` on `DockerSandboxConfig` or `WslcSandboxConfig`, together with a rebuilt packaged `egress_proxy_image`. The lifetime accepts integer seconds from 1 to 3600. The backend declares `ATTACHED_IDENTITY`, `PER_SANDBOX`, and the configured retention bound. The host must permit `max_identity_scope=IdentityScope.PER_SANDBOX`; the workload must request that capability, scope and retention explicitly, with `isolation_scope=IsolationScope.CALL` and concrete `EgressRule(..., authority=...)` destinations. Ordinary workloads need a backend without a credential gateway.

The async provider receives a `CredentialRequest` containing the trusted `SandboxKey`, workload kind, actual runtime instance ID, fresh generation, exact audience set, and Unix expiry deadline. Each authority rule must use a unique audience; duplicate audiences are refused before provisioning. The provider must authorize all of that context and return exactly one `CredentialGrant(audience, origin, token, expires_at)` for each audience. `origin` is an exact HTTPS origin, including a nondefault port where needed. Its hostname must match the rule. The token is a bearer value; the expiry is a Unix timestamp. Neither a requested audience nor a sandbox key proves business authorization.

The host must obtain `scope`, conversation, agent and call identifiers from trusted request context. Include both tenant and user in `scope` when they are separate boundaries. Do not use a tenant-wide scope for user-specific credentials. Do not return a shared token from a provider intended to authorize individual users.

Every credential acquisition creates a fresh workload container, private network and external gateway, including acquisitions with the same key on competing hosts. There is no shared credential cache, gateway adoption, ownership transfer or refresh operation. A copied guest header cannot select another principal: the gateway removes guest-supplied `Authorization` and injects its own authorized credential only at the configured origin, method and path. Plain allowlisted hosts without a grant receive no `Authorization` header. This header policy applies only when the credential gateway is enabled. A fresh call must obtain a fresh grant. Applications requiring a single business operation across replicas must enforce that in their authorization or idempotency service.

Only the trusted runtime management channel installs grants. Tokens enter the gateway through stdin and a private one-time file; they do not enter guest mounts, environment variables or process arguments. The gateway's private CA key also stays outside the guest. The gateway verifies the guest's private network address and its own boot identity. A restart rejects the previous grant. Each generation has separate runtime resources, so an old process or recycled address in another generation cannot reach a new grant.

The gateway checks authority on every HTTP request, including requests over existing TLS connections. It enforces the earlier of the grant expiry and its fixed lifetime, using a monotonic deadline once loaded. Active upstream streams are cancelled at that deadline. The lifetime is independently capped from proxy startup, before the workload container is created. No heartbeat or host cleanup is needed for this bound. Normal call cleanup removes the gateway before the workload; if cleanup cannot reach the runtime, the independent expiry still applies. Effects already accepted by an upstream cannot be undone.

Key, kind and scope cleanup report proxy or network removal failures as incomplete cleanup, even when the workload container is already gone. The creating backend retains the workload name for a later retry if label listing fails. Other users, calls and generations remain separate. A failed listing still reports `unlisted`, since the local fallback cannot prove cleanup of resources created by another host.

Credential injection always requires verified upstream TLS, including private destinations. `allow_private_http` does not relax this requirement. Allowed upstream services receive the bearer token and must be trusted not to disclose it in responses or through their own features. The gateway does not prevent misuse of the permissions that the host granted at an allowed service.

<a id="file-store-provenance--what-a-kind-reads-and-what-it-is-worth"></a>

## File-store provenance

`AgentFileStore` returns text without content labels. `FileStoreProvenance` records model-driven mutations by path so a kind can assess what it reads.

Wire the same record into three places: `file_store_provenance_middleware`, `list_all_files(..., provenance=record)`, and `sandboxed_tool(file_store_provenance=record)`. Use one record per store.

![The host shares one provenance record between write-observing middleware, file listings and sandboxed reads. A model-driven mutation records untrusted integrity. A read compares the record's state before and after fetching bytes, then combines stable evidence with the listing. Changed evidence produces an unlabelled read; an optional integrity requirement can refuse it.](assets/file-provenance.svg)

The middleware records mutations as untrusted in a `finally`, even if the body raises or returns a refusal as text. It reads the final normalized path after other middleware has expanded arguments. Deletes are recorded too; only the host may `forget` a path after establishing removal.

`floor=None` leaves paths with no entry unestablished. A trusted floor is a host claim about those paths. Recorded entries always override it and can only lower integrity. An out-of-band overwrite does not erase an entry.

A trusted floor requires construction of the observing middleware. This catches a missing observer, but cannot verify that the host actually added it to the agent's chain. Wiring remains the host's responsibility.

`read_file` checks the record's value and change counter before and after reading. Stable evidence is combined with the listing's label. Any intervening record change leaves the read unlabelled, including a change to another path.

Set `requires_file_integrity` to reject weaker reads. Its default, `None`, accepts every readable file. `UNTRUSTED` requires an established label; `TRUSTED` accepts only trusted evidence. Without a session record, admission uses the listing alone.

Refused content is not delivered or counted as fed input. Bicep can continue with other admitted files; CodeAct stops that call. Provenance wiring errors still raise rather than becoming content refusals.

One limit remains: middleware records when the writing call returns. Bytes already written by an unfinished call are not yet recorded. A trusted floor includes the host's responsibility for that interval.

## Classify derived tool results

Set a tool's `additional_properties["confidentiality"]` to the classification used by the application. This classifies results. `max_allowed_confidentiality` instead limits data sent to a destination; a provenance floor instead describes file integrity.

Core weakens source integrity after an untrusted or unestablished file read. A trusted read never promotes an untrusted tool. Every shipped kind remains an untrusted source.

Tools that commit guidance always label derived items. Tools without committed guidance need both valid source-integrity and explicit confidentiality declarations for core to stamp derived items. Otherwise the framework resolves their labels. [Information flow](information-flow.md#how-core-labels-a-call) defines the full rules.

## Where the storage base comes from

The backend binds a storage base when it acquires a sandbox. `SandboxSpec.work_dir=None` delegates that choice. An explicit guest-native value requires that base; the default `/maf-sandbox/work` is an explicit value.

Kinds use `working_directory="."` for the base and `session.guest_call_path()` for a relative call child. Relative directories cannot escape the base. File paths cannot escape their working directory. Commands and argv are opaque and are never rewritten.

Bicep keeps an explicit base so the compiler can discover its image-provided configuration. CodeAct delegates allocation. The host-tools launcher resolves native paths inside the guest for its program, imports and transport files.

For exec and file workloads, acquisition prepares the base through the backend's file mechanism. Existing contents, ownership and modes remain. Unsafe or obstructed ancestry causes refusal. Runtime-only workloads need no filesystem.

## Status

| Decision | State | Tracking |
|---|---|---|
| Artifact collection, sinks and names | Implemented; delivery remains per artifact | [#113](https://github.com/sokolaidev/maf-extensions/pull/113) (merged); [#156](https://github.com/sokolaidev/maf-extensions/pull/156) (merged) |
| File-store output sink and read tools | Implemented | [#902](https://github.com/sokolaidev/maf-extensions/pull/902) (merged) |
| Atomic batch delivery | Unimplemented | untracked |
| Host-tool registry, declarations and transport | Implemented | [#133](https://github.com/sokolaidev/maf-extensions/issues/133) (closed); [#410](https://github.com/sokolaidev/maf-extensions/pull/410) (merged); [#417](https://github.com/sokolaidev/maf-extensions/issues/417) (closed) |
| Host-tool identity admission and per-run minting | Implemented | [#396](https://github.com/sokolaidev/maf-extensions/issues/396) (closed); [#568](https://github.com/sokolaidev/maf-extensions/issues/568) (closed); [#446](https://github.com/sokolaidev/maf-extensions/issues/446) (closed); [#593](https://github.com/sokolaidev/maf-extensions/pull/593) (merged) |
| Core attached-authority admission | Implemented; Docker and WSLC support bounded egress headers | [#1168](https://github.com/sokolaidev/maf-extensions/issues/1168) (closed); [#1192](https://github.com/sokolaidev/maf-extensions/pull/1192) (merged); [#567](https://github.com/sokolaidev/maf-extensions/issues/567) (open) |
| ACAS group-configured identity | Supported outside the core attached-authority contract | [#1170](https://github.com/sokolaidev/maf-extensions/issues/1170) (open) |
| Principal references | Unimplemented | [#566](https://github.com/sokolaidev/maf-extensions/issues/566) (open) |
| Credentials for guest HTTP | Docker and WSLC external gateways implemented | [#757](https://github.com/sokolaidev/maf-extensions/issues/757) (closed) by [#1427](https://github.com/sokolaidev/maf-extensions/pull/1427) (merged) |
| File-store provenance and result labels | Implemented with the read/write interval limits above | [Information-flow status](information-flow.md#status) |
| Cross-conversation host storage paths | Host-owned partitioning; no automatic callback inspection | [#793](https://github.com/sokolaidev/maf-extensions/issues/793) (closed) |
| Storage-base preparation and allocation | Implemented | [#466](https://github.com/sokolaidev/maf-extensions/issues/466) (closed); [#1086](https://github.com/sokolaidev/maf-extensions/pull/1086) (merged); [#480](https://github.com/sokolaidev/maf-extensions/issues/480) (closed); [#1090](https://github.com/sokolaidev/maf-extensions/pull/1090) (merged) |
