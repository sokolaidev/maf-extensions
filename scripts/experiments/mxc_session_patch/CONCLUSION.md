# MXC Hyperlight feasibility conclusion

Recommendation: **go for experimental integration using the removable MXC patch; no-go for advertising the selected production backend contract yet**. The feasibility spike establishes modified-interpreter capture and recovery for fixed workloads. It does not narrow the selected file, persistence, networking or compatible-machine recovery requirements.

MXC v0.9.0 at `86fb3d2abaf9c431556692037bff881830b543a5` provides fresh execution through its public runner. Persistent-session operations in this experiment require the feature-gated downstream patch; they are not a supported released MXC API. [Upstream #1374](https://github.com/microsoft/mxc/issues/1374) contains the proposal and contribution offer. The wrapper keeps replacement with an upstream implementation localized; no upstream PR has been published.

| Area | Evidence and decision |
|---|---|
| Rich Python and continuity | Fixed NumPy/pandas state, lambda and guest file/seek position passed on Windows/WHP and Linux/KVM |
| Capture and process restart | Separate native process restores committed interpreter state; post-capture mutations remain absent |
| Durable completion | Local checkpoint/result transaction and retries passed; corruption regressions cover reused chunks; this is not power-loss or host-reboot qualification |
| Files and artifacts | Fixed CSV-to-chart and identical saved-artifact redelivery passed; general file confinement remains unimplemented |
| Execution transport | Guest streams merge and a long write was truncated; no-go for the suite's faithful bounded-output contract until corrected |
| Compatible-machine recovery | Unrun; local OS ownership is not distributed fencing, and cross-hypervisor checkpoint compatibility is not implied |
| Networking | No network authority is configured in the fixed probes; closed and allowlisted enforcement require independent bypass tests |

See [native controls](README.md), [publication evidence](HOST_PUBLICATION.md) and [owner-liveness continuation](OWNERSHIP.md) for exact source identities, runtime results and limits. Historical reports remain tied to their measured revisions.

Independent implementation follows under [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648): bounded native transport [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668), owner-death cleanup [#1669](https://github.com/sokolaidev/maf-extensions/issues/1669), safe files [#1670](https://github.com/sokolaidev/maf-extensions/issues/1670), compatible-machine recovery/fencing [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671), storage retention [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672), and egress qualification [#1673](https://github.com/sokolaidev/maf-extensions/issues/1673). Router/CodeAct integration remains under the parent issue, gated on those contracts rather than the existence of a runtime name.
