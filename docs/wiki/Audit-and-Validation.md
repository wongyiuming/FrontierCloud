# Audit and Validation

## September 2026 four-area review

The current review covers functionality, performance, security and audit behavior. Priority means operational urgency for this project; it is not a CVSS score.

| Direction | P0 | P1 | P2 | P3 |
|---|---:|---:|---:|---:|
| Functionality | 0 | 2 | 7 | 1 |
| Performance | 0 | 0 | 1 | 0 |
| Security | 0 | 1 | 2 | 0 |
| Audit | 0 | 0 | 1 | 0 |
| Total | 0 | 3 | 11 | 1 |

No P0 was demonstrated in the reviewed scope. That statement is bounded evidence, not a claim that the project is vulnerability-free. The repository report records each finding, reproduction, repair and remaining limitation.

## Persistent development environment

The development environment is a retained private-CA HTTPS cluster:

```text
1 Master
3 Direct Followers
6 Relay Followers
```

Each logical node owns an isolated Docker daemon. This preserves the normal Compose container names and lets the released Updater replace only that node's services. Web continues to run as its unprivileged production UID; root is limited to host provisioning, CD and bounded fault injection.

The real-data import used normal Admin upload reservations and finalization rather than copying managed files into place. It created:

| Type | Objects |
|---|---:|
| MP3 | 671 |
| MP4 | 668 |
| Distinct LRC | 360 |
| Total | 1,699 |

The imported payload total is 16,249,624,613 bytes. Local, Direct and Relay placements are all represented. Media names and payloads remain development data and are not published in the repository or Wiki.

## Release and CD evidence

Starting with all ten nodes at `b20db44`, the normal Admin **upgrade and distribute** transaction converged every application and Updater runtime to `3f4ea30`. The normal rollback transaction then returned all ten to `b20db44`. The persistent CD reconciliation service subsequently called **upgrade and distribute** and restored all ten to `3f4ea30`.

CD runs as a root-owned system service every ten minutes. It does not write Git state directly as a deployment shortcut. It logs into the Master through verified HTTPS, calls the public Admin release transaction, and verifies ten-node convergence. New upgrades fail closed unless the current `main` has the required merged `dev -> main` provenance and successful exact-`dev` CI.

## Acceptance scope

The candidate passed local unit/runtime discovery, Linux unit/runtime discovery, Node browser-support smoke, real Chromium playback/karaoke, private-CA positive validation, unknown-CA and hostname-mismatch negative validation, Direct/Relay streaming, recording, lyrics, backup and rollback/CD exercises. The persistent environment also runs bounded concurrent Range reads and recoverable Web, Redis and MySQL fault tests.

The bounded real-data run completed 1,200 concurrent 1 MiB Range reads with 48 workers and no byte/status failures. On the shared physical host it measured 168.84 requests/s, p50 186.43 ms and p95 557.27 ms. These figures are useful for detecting regressions on this host; they are not a capacity promise for another network or machine.

Results describe the tested commit, host and dataset. They do not establish a production availability or throughput SLA, and the development CA is not a public-trust certificate.

Dependency review uses both project-level `pip-audit` and built-image Trivy results. The Python dependency resolution had no known advisory. Fixable Alpine packages in the Nginx image are upgraded during build. Scanner entries without an available vendor package remain visible as residual base-image risk; embedded-SBOM entries are checked against actual runtime package metadata before being counted as installed.
