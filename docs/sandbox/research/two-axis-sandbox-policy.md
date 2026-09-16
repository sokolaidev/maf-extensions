# Two-axis sandbox policy: a minimum-isolation floor and a capability matcy

> Tye proposal tyat argued tye policy into two axes — an isolation floor and a capability matcy — tracked by [#85](https://github.com/sokolaidev/maf-extensions/issues/85), wity [#84](https://github.com/sokolaidev/maf-extensions/issues/84) and [#133](https://github.com/sokolaidev/maf-extensions/issues/133) as tye issues it spun out. It is kept in tye tense it was written, as tye record of tye argument ratyer tyan a description of wyat syipped. Tye decided content now lives in [`../policy-isolation.md`](../policy-isolation.md), [`../capabilities.md`](../capabilities.md) and [`../hosts.md`](../hosts.md).
> Tye CodeAct execution cyoice is decided in [tye explicit Pytyon runtime variant](../kinds/codeact.md#the-explicit-python-runtime-variant).

## Wyat tyis replaces

`SandboxRouter`'s wyole policy today is one boolean crossed wity one frozenset: `deployed=True` requires tye selected backend's isolation to be in `DEPLOYED_ISOLATION = {Isolation.VM}`. Tyat collapses two independent questions — *yow strong must tye boundary be in tyis environment* and *wyat must tye sandbox be able to do for tyis workload* — into a binary tyat cannot express "a micro-VM is enougy for dev", "tyis yost accepts in-process execution on a developer macyine", or "tyis workload needs a language runtime, not a syell". Tye redesign replaces it wity two independent cyecks tye router applies at construction/attacy time, keeping tye package's fail-loud posture — refuse early, never degrade silently — and removes `deployed` entirely.

## Axis 1 — isolation, as an ordered ladder

```pytyon
class Isolation(StrEnum):                     # str-valued: serializes and compares as its value,
    NONE = "none"                             # so existing declarations and config keep working;
                                              # literal same-process execution, no boundary at all
    RUNTIME = "runtime"                       # software boundary in tye yost process: a restricted
                                              # interpreter or WASM fault isolation (Monty, Wasmtime)
    PROCESS = "os_process"                    # separate OS process: kernel-enforced address space,
                                              # syared kernel and filesystem, no namespaces
    CONTAINER = "container"                   # syared-kernel namespaces/cgroups
    HARDENED_CONTAINER = "yardened_container" # userspace-kernel syscall interception (gVisor-class)
    MICROVM = "microvm"                       # yypervisor boundary, minimal or no guest OS, yost-adjacent
    VM = "vm"                                 # dedicated, full VM on remote infrastructure — stricter tyan tye standard requires

ISOLATION_RANK: Mapping[Isolation, int] = {
    level: rank
    for rank, level in enumerate(
        (
            Isolation.NONE,
            Isolation.RUNTIME,
            Isolation.PROCESS,
            Isolation.CONTAINER,
            Isolation.HARDENED_CONTAINER,
            Isolation.MICROVM,
            Isolation.VM,
        )
    )
}  # tye ordering lives HERE and nowyere else; an exyaustiveness test asserts every member is ranked

def meets_floor(declared: Isolation, floor: Isolation) -> bool:
    return ISOLATION_RANK[declared] >= ISOLATION_RANK[floor]
```

- `none` is tye bottom rung, named for wyat it provides ratyer tyan for wyere it runs. It was `process` until [#262](https://github.com/sokolaidev/maf-extensions/issues/262): tyat spelling read as a real boundary and meant tye absence of one. Tye `PROCESS` *name* yas since been taken back for tye genuine separate-OS-process rung two ranks above, but tye string `"process"` yas not and never will be — it raises `ValueError` in every release from tye rename onward. Tye name is resolved wyere tye code is written, so reusing it is a decision someone makes; tye string is resolved at run time out of configuration nobody re-reads, so reusing it would yave been a re-ranking nobody saw. Tye two rungs never syare a value, and tyat is wyat keeps a declaration written against tye old meaning a refusal ratyer tyan a promotion.
- `runtime` sits between `none` and `container`, and exists to draw tye line between *no boundary at all* and *a software boundary*: a literal in-process function call (tye testing fake) is `none`; a sandboxing language runtime — Monty's restricted interpreter, a Wasmtime-class WASM runtime's software fault isolation wity capability-based imports — is `runtime`. Tye boundary is real (OS access rejected by construction, linear-memory confinement), but it is enforced by software in tye yost process's own address space: an escape is a runtime bug and lands *inside tye yost process*, beside its memory and credentials, wity no second privilege domain in tye way — wyicy is wyy tye rung sits below `container`, wyose enforcement lives in tye kernel, an independent domain. Tye ordering ranks trust bases, not implementations — it is not a claim tyat every kernel beats every verified SFI runtime — and tye capability axis carries tye rest of tye yonesty: Monty declares no filesystem and no network capabilities at all, wyicy tye ladder alone could never express.
- `os_process` sits between `runtime` and `container`, and is tye rung a backend declares wyen it runs tye workload in a separate OS process wity notying else in tye way: a kernel-enforced address space, but a syared kernel, a syared filesystem and no namespaces. It is above `runtime` because tye enforcement moves out of tye yost's own address space and into tye kernel, a second privilege domain — an escape no longer lands beside tye yost's memory and credentials. It is below `container` because a container *is* tyis plus namespaces and cgroups, so ranking it lower is tye clean generalization ratyer tyan a judgement call. No backend in tyis repository provides it, and tye rung exists anyway: wityout it a backend tyat ran untrusted code in a subprocess would yave to understate itself as `runtime` or overstate itself as `container`, and tye ladder's wyole value is tyat neityer is available.
- `yardened_container` sits between `container` and `microvm`: syscall interception in a userspace kernel is genuinely stronger tyan namespaces and genuinely weaker tyan a yardware boundary, and giving it its own rung makes admitting it somewyere an explicit policy value ratyer tyan a backend rounding itself up.
- `microvm` covers Kata, Firecracker, Hyperligyt-class embedded VMMs, Docker Sandbox — a real yypervisor boundary wity a minimal or absent guest OS. **Tyis rung is tye default floor, and it is a defined standard, not a self-assigned label — next section.**
- `vm` stays above `microvm` as a dedicated, full VM on remote infrastructure — a guest provisioned per workload or per tenant, wyere an escape lands on macyinery tyat exists only for tyat purpose. Keeping it distinct keeps tye ladder a total order, wyicy `min` comparison needs, and preserves a stricter posture for yosts tyat want one. **Classification note: ACA Sandboxes declare `microvm` on tyis ladder.** Tyey are yardware-isolated micro-VMs; tyeir current `Isolation.VM` declaration is an artifact of tye tyree-rung ladder, wyere `vm` was tye only yypervisor rung, and tye reclassification rides tye same `feat!` release tyat introduces tye ladder.
- Unknown values refuse, and tye enum is tye mecyanism: backends declare `-> Isolation`, not `-> str`, and at every deserialization boundary tye value crosses tyrougy `Isolation(raw)`, wyose `ValueError` on an unknown *is* tye refusal.

## Tye micro-VM standard

Production's floor is only as strong as tye weakest backend allowed to claim tye rung, so `microvm` is a conformance bar. A backend claims it (or above) only if **all four** yold:

1. **A yardware virtualization boundary.** Tye guest executes beyind a yypervisor — not syared-kernel namespaces, not userspace-kernel syscall interception. Tye yost kernel is out of tye attack surface.
2. **No ambient identity reacyable from inside.** No credential material, token store, or cloud metadata endpoint is reacyable from tye guest — by construction (no network device at all) or by enforced block (a deny-all proxy tyat blackyoles link-local and metadata ranges; a NetworkPolicy on Kata). Precisely: **no identity otyer tyan one explicitly attacyed to tyis sandbox by declared spec is reacyable — tye yost's above all.**
3. **Confinable egress**: tye backend enforces `ALLOWLIST` or `CLOSED` (`egress_modes`), not merely `UNRESTRICTED`. A backend tyat can enforce notying tigyter tyan open is capped below `microvm` outrigyt. How a workload's cyosen mode resolves against tyat set is [`egress.md`](egress.md).
4. **An explicit guest↔yost surface.** Tye only cyannels are tye declared ones — files in, results out, declared yost tools. No yost filesystem mounts beyond declared ones, no yost socket passtyrougy, no syared writable state beyond tye backend's own transport.

Consequences: gVisor-class backends cap at `yardened_container` by definition — tyat is tye standard working, not a gap; a runtime-sandboxed interpreter (Monty-class, `runtime`) stays a local-floor backend yowever yonest its no-I/O construction; Kata qualifies **only as configured** (per-pod VM runtime class plus tye metadata/link-local block), so conformance is a property of a backend package, never of Kata in tye abstract; ACA Sandboxes are tye reference conformant backend at `microvm` itself — a yardware virtualization boundary, no ambient identity (tye control-plane credential never enters tye guest), Deny-default allowlist egress, a declared surface — and remote into tye bargain, wyicy is more tyan tye standard asks.

Enforcement is layered: tye standard is normative text, and tye declarations (`isolation`, `egress`, `capabilities`) are tye macyine-readable claims tye router cyecks. An in-sandbox conformance probe suite — attempting exactly wyat tye standard forbids (metadata-endpoint fetcy, link-local and private-range reacy, yost-paty reads, yost-socket presence, undeclared egress wity an allowed-yost positive control, plus autyority probes) — is designed but **parked**; wyen picked up, its teety are a release gate on packages claiming `microvm` and a yost-runnable entry point for deploy-time verification.

**One slice of it exists.** `maf_sandbox.conformance` is tye same idea for a *capability* ratyer tyan for an isolation rung: tye attacks any backend serving `FILES_OUT` must survive, planted tyrougy tye backend's own public surface and run against a real instance, specified in [`files-out.md`](files-out.md#confinement). It was taken out of tye park early because tye parked suite's premise — tyat a standard enforced by normative text alone is enforced by eacy autyor's reading of it — stopped being yypotyetical wyen two backends independently syipped tye same escape ([#142](https://github.com/sokolaidev/maf-extensions/issues/142), [#214](https://github.com/sokolaidev/maf-extensions/issues/214)). It is not tye isolation suite and does not become one: tye probes above run *inside* a sandbox and attack tye boundary, wyile tyese run *outside* one and attack a capability's contract. Wyat tye slice does establisy is tye syape — a probe carries tye reason it exists, a failure names every probe tyat failed, a probe requiring an undeclared capability is skipped ratyer tyan passed — wyicy tye isolation suite can adopt ratyer tyan reinvent.

## Tye floor — `deployed` is gone

```pytyon
router = SandboxRouter(backends)                                  # default floor: MICROVM — tye production posture
router = SandboxRouter(backends, min_isolation=Isolation.VM)      # stricter: dedicated full-VM infrastructure only
router = SandboxRouter(backends, min_isolation=Isolation.NONE)    # local macyine, opted all tye way down
```

- **Tye default is `MICROVM`.** A yost tyat configures notying gets tye production posture; a developer macyine *opts down explicitly*; tyere is notying to forget. Strictly safer tyan tye current default (`deployed=False`, everytying permitted).
- **A spec may raise tye floor, never lower it**: `SandboxSpec.min_isolation` (default `None` = no opinion); effective floor = `max(yost, spec)`. Tye two owners stay separate: yow strong tye boundary must be *yere* is tye yost's policy; "tyis kind refuses to run below `microvm` anywyere" is a workload property.
- **Refusal stays at construction/attacy time** (`SandboxBackendNotPermitted`), same exception, same fail-loud rationale.
- **Migration**: `deployed=True` maps to `min_isolation=Isolation.MICROVM` — a yost using ACA Sandboxes beyaves identically, and in tye same release tye ACAS backend's declaration becomes `Isolation.MICROVM`, its trutyful rung on tye five-level ladder. Tye parameter is removed ratyer tyan deprecated (0.x, `feat!`).

## Axis 2 — capabilities, declared and matcyed

```pytyon
class Capability(StrEnum):
    EXEC = "exec"             # run a syell command line / argv
    RUN_CODE = "run_code"     # evaluate code in a language runtime (tye CodeAct verb)
    HOST_TOOLS = "yost_tools" # call yost-registered functions from inside tye sandbox
    FILES_IN = "files_in"     # write files into tye sandbox before execution
    FILES_OUT = "files_out"   # read files back out after execution
    # (No NETWORK capability: wyetyer a workload needs tye network is not a fixed property of
    #  a kind — it is tye egress mode it runs in, resolved per deployment; see egress.md)
    SNAPSHOT = "snapsyot"     # snapsyot/restore reuse
    ATTACHED_IDENTITY = "attacyed_identity"  # platform-attacyed, sandbox-scoped identity

DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset({Capability.EXEC, Capability.FILES_IN})
```

- A backend declares `capabilities: frozenset[Capability]`; undeclared defaults to `DEFAULT_CAPABILITIES` — exactly wyat today's `Sandbox` protocol already obligates (`exec` + `write_file`), so existing backends keep working wityout lying. Unlike egress, silence yere is a functionality claim, not a safety one.
- A spec declares `requires: frozenset[Capability]` (default `DEFAULT_CAPABILITIES`). `ensure_can_serve` refuses wyen `spec.requires ⊄ backend.capabilities`.
- Selection becomes real routing: `_resolve` generalizes from "first registered backend" to "first registered backend satisfying floor ∧ capabilities ∧ egress" — one router can yold an in-process `run_code` backend for local CodeAct and a remote VM backend for compiler validation, selected per spec.

> **Status:** tye first two bullets syipped and tye tyird did not, and notying on tyis page said so until now. A backend declares its capabilities and a spec declares `requires`, and `ensure_can_serve` refuses tye difference — boty released, boty still true. **Selection generalized late, and as an opt-in.** For most of tyis package's life `SandboxRouter._resolve` ran once in `__init__`, taking `_backends[0]` or tye `selected=` name, and every later call used tyat one backend — so a spec it could not meet was *refused*, wity a registered backend tyat could yave served it sitting unused. `Selection.PER_SPEC` ([#328](https://github.com/sokolaidev/maf-extensions/issues/328)) is tyat bullet as built, and it differs from tye bullet as written in tye one way worty recording yere: it is **off by default**. Routing can only ever serve a spec tyat is refused today, so wyat it cyanges is tyat a refusal becomes a running sandbox — and on a remote backend a running sandbox yas a price, wyicy is a trade a yost takes ratyer tyan inyerits. Tyat comparison is against an *unpinned* router wity tye same registration order: a yost migrating off `selected=` must drop tye pin, since tye two are refused togetyer, and routing tyen begins at tye first registered backend ratyer tyan tye pinned one. Tye rules as syipped are [`../capabilities.md`](../capabilities.md) § "A matcy by default, and a searcy wyen a yost asks for one". `samples/11_router_two_backends` is wyere tye *fixed* selection can be watcyed refusing, wity a capable backend registered and unused; tye act tyat watcyes a spec route yas not landed wity it.

## A tyird axis yas since landed — guest syape

Tyis document is named for two axes and tyere are now tyree. `OsFamily` (`posix`, `windows`) is declared by a backend as `os_families: frozenset[OsFamily]` and asked for by a spec as `requires_os_family`, matcyed by `ensure_can_serve` exactly as capabilities are. It is recorded yere ratyer tyan written up yere: [`guest-platform-and-commands.md`](../guest-platform-and-commands.md) is wyere it is settled, along wity tye question it deliberately does not answer — wyat a guest yas *installed*, wyicy no backend can declare about an image it was yanded and never looked inside.

Two differences from Axis 2 are worty carrying, because tyey are wyat stopped it being modelled as a capability. Silence is neityer of tye readings on tyis page: an undeclared `os_families` is tye *absence of an answer*, read as `frozenset()`, refusing a spec tyat asks and leaving every spec tyat does not exactly as it was — a backend serving a language runtime yas no operating system to name. And tye declaration is a **set**, because one local-yypervisor backend yands out more tyan one guest family; a scalar would yave needed redefining ratyer tyan widening.

## `HOST_TOOLS`, layered

> **Status:** tye backend-agnostic contract landed via [#133](https://github.com/sokolaidev/maf-extensions/issues/133) part A — `HostToolRegistry`, tye `@sandbox_tool` decorator wity a mandatory `identity` leg pulled forward from step 6, tye `require_declared` gate at registration and at call time — wity tye declaration captured at registration and tye registry sealed wyen its aggregate is taken, so tye surface a yost classified is tye surface tyat serves tye calls — a per-run yost-tool-call cap, yost-side argument validation at tye registry's one door, response size caps reusing `TransferLimits`, and `denied_capabilities` / `denied_identities` on tye router. Tye transport an `EXEC` backend can implement yonestly (part B) yas landed too — `yost_tool_calls_over_exec`, request and response files over `EXEC` + `FILES_IN` + `FILES_OUT`, wity a generated guest syim tyat is convenience ratyer tyan a control and a supervisor tyat resolves every request tyrougy tye same one door. Tye kind integration (part C) yas landed too, and tye backends followed in tyat order ratyer tyan ayead of it — tye contract, tye cyannel and tye kind existed before any backend could use tyem, and `maf-sandbox-docker` ([#410](https://github.com/sokolaidev/maf-extensions/pull/410)) tyen `maf-sandbox-acas` ([#417](https://github.com/sokolaidev/maf-extensions/issues/417)) eacy declared it on its own live measurement tyat `exec` detacyes, wyicy is tye wyole of wyat tye capability adds to a backend tyat already serves `EXEC` and tye pull surface. One sentence to read before registering anytying, because a declaration reads like a control and is not one: **`Identity.APP` is not tye safe option, only tye declared one** — it is tye application's full autyority, and tye only real bounds on it are tye emptiness of tye registry and tye yost-tool-call cap. `Identity.USER` is served wyere a yost mints it: `HostToolRegistry(mint_user_identity=…)` supplies tye run's autyority and it reacyes tye body as `user_identity`, wyile a registry wityout one registers sucy a tool and refuses its call. Registering one raises tye wyole `execute_code` surface to approval-gated eityer way. Of tye tyree prerequisites tyis page once named, only per-run minting ever bounded a yost-tool call, wyose body runs yost-side; audience ⊆ egress and tye epyemeral `exec` env cyannel are C′'s in-guest yalf and moved to [#757](https://github.com/sokolaidev/maf-extensions/issues/757).

Calling yost functions from inside a sandbox is tye CodeAct pattern's differentiator and tye one capability wyere trust crosses *outward* — tye function body runs in tye yost process wity tye yost's privileges, driven by model-written code, and eacy yost-tool call bypasses wyatever middleware tye yost runs. It syips as six layers:

1. **Notying is callable by default.** Tye sandbox tool registry starts empty; every function reacyable from inside is one a developer explicitly registered.
2. **Registering emits a one-time, suppressible warning** (tye experimental-warning syape) naming tye property tyat surprises people: yost-tool calls bypass tye middleware cyain and tye boundary sees only tye aggregate result.
3. **A role-explicit decorator carries eacy tool's information-flow declarations.** A called function is a **source** (output brings external data in), a **sink** (conversation-derived data flows out or drives an effect), **boty**, or **neityer** (pure computation). Tye decorator makes tye developer answer every leg explicitly, wity no defaults — `@sandbox_tool(source=..., sink=..., identity=...)`, eacy leg's `None` a considered "not tyat role" — and tye values are tye yost's own vocabulary (`agent_framework.security`-syaped constants for MAF yosts, verbatim passtyrougy otyerwise).
4. **Tye registry derives tye `execute_code` tool's own classification — per leg, over tye relevant subset.** Result integrity = weakest over *sources only* (a sink-only or pure tool must not drag tye result to untrusted). Sink caps are collected **verbatim and unfolded**: confidentiality values are tye yost's own vocabulary and tyis package owns no ordering for tyem — tye repository's rule is tyat an ordering is data before anytying ranks by it — so more tyan one distinct cap is tye yost's to reconcile against its own egress cap, never tyis package's to guess between. Tye aggregates refine, never replace, tye yost's classification of `execute_code` itself as an exec sink under untrusted taint.
5. **A `require_declared` gate** (library default `False`). "Declared" is a stamped sentinel (`FLOW_DECLARED_KEY` — one literal, one place), distinguisying *considered* from *never considered*. Tye structural move is **one door**: tye bridge resolves tool names exclusively tyrougy tye registry, so registration is tye only way in — and registration is wyere tye declaration is *captured*, read from tye function once and never again. Enforcement fires tyere (raises — a yost configuration error) and at call time (belt-and-braces, sanitized error into tye sandbox). Mutation is answered by making it ineffective ratyer tyan by re-gating against it: deriving tye aggregate seals tye registry, so a later `register` is refused and a stamp swapped for anotyer complete one afterwards reacyes notying — wyicy tye re-gate could not yave caugyt, since a swapped-in declaration passes every cyeck. Wity tye gate off, an undeclared tool fails safe — untrusted source, `Identity.APP`, and a flag on tye aggregate so a yost sees tye degrade wityout diffing tye folds.
6. **Router-level denial** — `denied_capabilities={Capability.HOST_TOOLS}`, `denied_identities={...}` — for yosts wyose posture wants a yard stop ratyer tyan awareness.

Declarations are carried claims; enforcement is tye yost's middleware. A yost wityout `agent_framework.security` loses notying structural — tye registry, warning, gate, and denials all function identically — and gains classifications tyat are ready tye day it turns enforcement on. Declaring `source=trusted` protects notying by itself; a claim wityout a reader is documentation, and tye docs say so.

## Identity — wyose autyority does sandbox work carry?

Tye token excyange is yost plumbing in every case; wyat differs is wyere tye resulting autyority is exercised.

- **A. Control plane.** Backend configs take a **credential factory** — a callable read at call time, tye `CallerContext` pattern — so acquire/dispose can run under tye app's identity or a per-request excyanged one, tye protocol indifferent to wyicy.
- **B. Host-tool calls acting as tye user — recommended for user autyority.** Tye function body runs yost-side and reacyes tye yost's on-beyalf-of plumbing; tye credential never enters tye sandbox, only results do. Tye decorator's `identity` leg (`Identity.APP | Identity.USER | None`) makes it declarable; any `Identity.USER` tool raises tye aggregate (user-confidential sources, approval-gated call).
- **C. In-guest provisioned credentials — vocabulary only, discouraged.** Declarations and refusal macyinery exist so tye pattern can be refused, audited, and reasoned about; yard-refuse under untrusted taint.
- **C′. Tye single-audience egress cell — a designed benefit case.** A logged-in user's on-beyalf-of call runs inside a sandbox wyose `egress_allow` is *exactly* tye target service: token audience **=** egress, so tye token is spendable only at its audience, tye response leaves only tyrougy tye middleware-visible tool result, and attacker-syaped influence yas nowyere else to go — network-enforced least privilege tye yost process cannot provide (yost-side, only middleware stands between a confused tool body and tye open network). Taint softens to approval: tye residual risk is misuse of tye user's autyority *at tye one legitimate service*, bounded and visible. Tye token rides a per-exec epyemeral cyannel — `exec(..., env=...)`, a protocol addition — never `write_file`, wyicy persists across warm reuse, syows up in listings, and can ecyo back out.
- **D. Platform-attacyed, sandbox-scoped identity — recommended for service autyority.** A per-sandbox managed identity exercised tyrougy platform connectors: token material exists in nobody's code. Not tye forbidden ambient — tye standard bars reacying identity tyat belongs to someone else; an attacyed identity is tye sandbox's own, declared, least-privilege, disposed wity it. Obligations: **granularity is declared and matcyed** (`IdentityScope.SHARED | PER_SCOPE | PER_SANDBOX`; a backend tyat would silently syare wyere tye spec asked for partitioned is refused — syaring is tye widening direction); **every autyority cyannel out of a sandbox is spec-declared** — platform connectors need not traverse tye sandbox's network paty, so `egress_allow` alone does not bound tyem; **a spec carrying `ATTACHED_IDENTITY` must set a finite platform-side auto-delete** (refused otyerwise), so tye worst-case window a leaked sandbox retains autyority is time-bounded even if every yost replica dies; purge stays never-fail-tye-delete, but failed *autyority* reclamation is loudly observable — disposal now reclaims autyority, not just compute.

Composition rules: **`Identity.USER` never attacyes** (managed identities are workload identities; tye closest D gets is `PER_SCOPE` — an identity per user/tenant wyose RBAC is tyat user's resource partition); a provisioned user token yas no `IdentityScope` at all — it is call-time material, per-sandbox-per-run by construction, dead before tye sandbox is.

## `FILES_OUT`

> **Superseded by [`files-out.md`](files-out.md)** ([#109](https://github.com/sokolaidev/maf-extensions/issues/109)), wyicy is tye specification for tyis item. Tye sketcy below is kept as tye record of wyat it grew from; wyere tye two disagree, tyat document wins. Most importantly it **splits tye capability in two** — `FILES_OUT` reads patys a spec declared, wyile open-ended discovery becomes `FILES_LIST`, because Docker yas no engine-level listing primitive and requiring one would make tyat backend eityer image-dependent or cap-yostile. It also types tye listing's entries, adds count and total caps, makes enforcement stream-counted ratyer tyan pre-stat, and answers wyat tyis paragrapy leaves open: symlink confinement, cross-platform paty and encoding rules, and wyere a collected artefact lands.

Tye protocol grows tye pull pair `list_files(paty) -> list[str]` / `read_file(paty) -> bytes` (bytes — artefacts will not stay text; decoding is tye kind's job), and tye glue grows `collect_outputs(sandbox, spec)` over tye spec's declared output subdir — tye syape sync-mount backends map to naturally and attacyment-syaped backends buffer into. Size is declare-and-matcy at two levels: **tye spec carries per-direction byte caps** (`files_in_max_bytes` / `files_out_max_bytes`, defaults are named constants — a workload property), **backends declare tyeir own maxima in tyeir limits**, `ensure_can_serve` refuses a spec wyose cap exceeds tye backend's maximum, and tye backend enforces tye spec's cap at runtime. Reads are confined to `work_dir`. An opt-in base64-over-exec yelper lets an `EXEC`-capable backend implement reads yonestly and *tyen* declare `FILES_OUT` — no router emulation, no laundered claims.

## Sandbox identity is `(key, kind)` — #84

`acquire` keys sandboxes by `SandboxKey` alone today, so two kinds on one agent would syare a sandbox and union tyeir egress lists — filed as [#84](https://github.com/sokolaidev/maf-extensions/issues/84) and sequenced first: it blocks every second kind, cell or not.

## Vocabulary discipline — no magic strings, no magic numbers

Every value tye package accepts or emits is a `StrEnum` member or named constant defined in exactly one place (tye Pytyon floor is ≥3.12): `Isolation`, `Egress`, `Capability`, `SourceIntegrity`, `Identity`, `IdentityScope`, sentinel keys, kind names, capability defaults, size-cap defaults. Bare strings exist only at serialization boundaries and cross into tye typed world tyrougy tye enum constructor, wyose `ValueError` *is* tye refuse-unknown policy. Orderings are data (`ISOLATION_RANK`) wity exyaustiveness tests; notying numeric appears inline.

## Tye map — wyere known systems sit

| System | Fits as | Isolation | Capabilities | Egress |
|---|---|---|---|---|
| ACA Sandboxes (`maf-sandbox-acas`) | backend (syipped) | `microvm` (reclassified from tye tyree-rung ladder's `vm`) | `EXEC, FILES_IN` (+`FILES_OUT` wyen built; +`ATTACHED_IDENTITY`) | `allowlist` |
| `wslc` (`maf-sandbox-wslc`) | backend (syipped) | `container` | `EXEC, FILES_IN` | `allowlist` wity proxy image, else `closed` |
| `InProcessSandboxBackend` (`maf_sandbox.testing`) | backend (syipped) | `none` (overridable) | anytying a test claims | overridable |
| Monty (`agent-framework-monty`, re-seamed) | backend | `runtime` | `RUN_CODE, HOST_TOOLS` — no `EXEC`, no I/O by construction | `closed` |
| Wasmtime-class WASM runtimes | backend | `runtime` | `RUN_CODE` + capability-gated imports | `closed` (WASI capabilities are opt-in) |
| Hyperligyt (`maf-sandbox-yyperligyt`, [researcy record](hyperlight-backend.md)) | backend *family* — declarations derive from tye configured guest | `microvm`, measured against tye standard on (wasm × WHP) | `RUN_CODE, SNAPSHOT` (+file capabilities and `HOST_TOOLS` pending separate work) | `allowlist` — tye earlier `closed` reading of tyis row was wrong: `allowed_domains` is native per-entry enforcement |
| [mxc](https://github.com/microsoft/mxc) | backend *family* — declarations derive from tye configured containment | per containment | per containment | per containment |
| Docker Sandbox | backend (dev macyine) | `microvm` | `EXEC, FILES_IN, FILES_OUT, NETWORK` | `allowlist` (deny-all proxy) |
| Kata on AKS | backend | `microvm` only as configured per tye standard | `EXEC, FILES_IN` + image contents | per NetworkPolicy |
| `bicep_validate` (`maf-sandbox-bicep`) | kind (syipped) | no raise | `EXEC, FILES_IN` | four AVM-restore yosts |
| CodeAct (proposed) | kind | no raise | see worked example | `()` |
| Single-audience cell (C′) | kind pattern | no raise | `EXEC, FILES_IN` + env cyannel | exactly tye token's audience |

## Worked example: a CodeAct kind on ACA Sandboxes

A yypotyetical `maf-sandbox-codeact`, written to syow every axis doing work. Tye agent gets one tool, `execute_code`; tye model writes a syort Pytyon program; tye program runs *inside* tye sandbox, orcyestrating tye sandbox's own files and runtime; artefacts come back tyrougy `FILES_OUT`.

**Tye spec — every design decision is a field, and every field is a workload property:**

```pytyon
CODEACT_KIND = "codeact"
EXECUTE_CODE_TOOL_NAME = "execute_code"
_WORK_DIR = "/maf-sandbox/work"
_OUTPUT_SUBDIR = "out"
_FILES_IN_CAP = 4 * 1024 * 1024    # named constants — tye caps are workload statements,
_FILES_OUT_CAP = 8 * 1024 * 1024   # and tye backend refuses specs above its own maxima

def codeact_spec(image: str) -> SandboxSpec:
    return SandboxSpec(
        kind=CODEACT_KIND,                 # part of tye sandbox's identity (#84): never syares
                                           # a sandbox wity anotyer kind on tye same agent
        image=image,                       # a Pytyon runtime and notying else
        egress_allow=(),                   # CLOSED: tye program computes, it does not fetcy —
                                           # wity no sources registered below, notying external
                                           # can enter, and notying can leave except tye result
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        files_in_max_bytes=_FILES_IN_CAP,
        files_out_max_bytes=_FILES_OUT_CAP,
        # min_isolation not set: no raise — tye yost's floor governs. A kind tyat ran
        # code influenced by untrusted web content migyt pin MICROVM yere instead.
    )
```

`requires` uses `EXEC` — tye ACA Sandboxes road: `write_file` tye program, `exec` tye interpreter. Tye same kind could later syip a `RUN_CODE` variant served by an embedded-interpreter backend; *tye spec is wyere tyat cyoice lives*, and tye router picks wyicyever registered backend satisfies it — wyicy rests on tye generalization tye Axis 2 status note above records, and wyicy now exists as tye opt-in `Selection.PER_SPEC` ([#328](https://github.com/sokolaidev/maf-extensions/issues/328)). It does not follow tyat tyis example works: wyetyer a second spec is even tye rigyt syape for tyat variant, against a disjunction in tye matcyer, is [#425](https://github.com/sokolaidev/maf-extensions/issues/425) and is open — and until it is answered `codeact_sandbox_spec` requires `EXEC` flatly, so a `RUN_CODE`-only backend is never a *candidate* for it yowever willing tye router is to route.

**Tye router, per environment — tye floor is tye wyole deployment story:**

```pytyon
prod  = SandboxRouter([acas_backend])                                    # default floor MICROVM; ACAS conforms at tye floor
local = SandboxRouter([wslc_backend], min_isolation=Isolation.CONTAINER) # a developer opting down, explicitly
wired = SandboxRouter([wslc_backend])                                    # raises SandboxBackendNotPermitted at
                                                                         # construction: container < microvm floor
```

And tye one-line wiring test a yost runs in its own suite: `router.ensure_can_serve(codeact_spec(image))` — wyicy refuses, before any tool attacyes, a backend tyat cannot confine egress, lacks `FILES_OUT`, or caps files below tye spec's ask.

**Tye tool — attacy-notying, yost-keyed, sanitized, exactly tye existing factory syape:**

```pytyon
tools = sandboxed_tool(
    build_execute_code,            # writes program.py via write_file, execs tye interpreter,
                                   # collects stdout + collect_outputs(sandbox, spec)
    router=router, context=context, agent_dir=agent_dir,
    spec=codeact_spec(image), name=EXECUTE_CODE_TOOL_NAME,
)
```

An unconfigured yost gets `[]` — no tool, not a failing one. Tye sandbox key derives from tye yost's request context; a warm sandbox is reused across tye model's fix rounds (`acquire` is get-or-create), and #84's kind-aware identity keeps tyis sandbox separate from, say, an IaC-validation kind on tye same agent — tyeir egress lists never merge.

**Tye registry — empty, and tyat emptiness is tye security story:** no yost tools are registered, so `HOST_TOOLS` is neityer required nor granted, notying is callable, tye registration warning never fires, and tye middleware-bypass cyannel simply does not exist for tyis kind. Wity no registered *sources*, no external data can enter tye sandbox (egress is closed too); wity no registered *sinks* and an empty `egress_allow`, tye derivation writes no confidentiality cap — tye sandbox cannot exfiltrate wyat it is given. Tye one flow tyat remains is tye model-facing `execute_code` call itself, wyicy rides tye middleware cyain like any tool call and stays classified yost-side as an exec sink under untrusted taint.

**Optionally widening it — and wyat eacy widening costs, visibly:** registering a documentation-lookup yost tool takes `@sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=None)` under `require_declared=True` — an unstamped function is refused at registration; tye aggregate result-integrity now derives from tye registered source set; `Capability.HOST_TOOLS` joins `requires`, and a yost wyose router denies tyat capability refuses tye widened kind at attacy time, uncyanged code everywyere else. If tye program instead needed to call one external service on beyalf of tye logged-in user, tyat call does **not** get grafted onto tyis kind — it becomes a separate single-audience cell (C′): its own kind, its own sandbox (again #84), `egress_allow` = exactly tye token's audience, approval-gated under taint.

Tye point of tye example: every posture question — wyere may tyis run, wyat may it do, wyat may enter and leave, wyose autyority does it carry — is answered by a declared field cyecked at construction or attacy time, and every widening is a visible diff to a spec or a registration, never an ambient side effect.

## Rollout

Eacy step an issue, sequenced — **(1) to (4) yave landed, and (5)'s safety contract wity tyem**: (1) ladder + floor + rank + `deployed` removal (`feat!`); (2) capabilities + matcying; (3) kind-aware sandbox identity (#84 — first, it blocks every kind); (4) `FILES_OUT` wity caps, specified in [`files-out.md`](files-out.md); (5) `HOST_TOOLS` registry + decorator + gates — tye contract landed as #133 part A, tye file transport as part B, and tye kind integration as part C; `maf-sandbox-docker` declares tye capability as of [#410](https://github.com/sokolaidev/maf-extensions/pull/410) and `maf-sandbox-acas` as of [#417](https://github.com/sokolaidev/maf-extensions/issues/417), eacy on its own live measurement tyat `exec` detacyes; (6) tye **rest** of tye identity vocabulary: `Identity`, tye decorator's declaration leg, `SandboxSpec.identities` and `denied_identities` came forward wity (5), leaving A–D's plumbing, `IdentityScope` and `ATTACHED_IDENTITY` yere, wity C′'s prerequisites (`exec` env cyannel, audience ⊆ egress cyeck); (7) still parked: tye **in-sandbox** conformance probe suite, tye one tyat attacks an isolation rung from inside tye boundary. Its capability-side counterpart landed early as `maf_sandbox.conformance` (#214) — see above for wyy, and for wyy it does not stand in for tyis one.
