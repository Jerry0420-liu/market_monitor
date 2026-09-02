# Market Monitor — Production Server Deployment Runbook

**Status:** APPROVED FOR EXECUTION
**Scope:** Owner-accepted P0 + CR-002 Native TDX
**Target:** Clean Ubuntu Server 24.04 LTS production deployment

## 1. Execution authority

Codex is authorized to complete the deployment end-to-end without routine Owner approval.

Codex may directly fix ordinary deployment and operational issues involving:

- Ubuntu configuration
- Docker / Docker Compose
- reverse proxy
- HTTPS
- permissions
- persistence
- logging
- backup / restore
- compatibility
- health checks
- restart / reboot recovery
- production configuration

Codex must stop and report only if deployment requires changing frozen business/domain semantics, including:

- Guardian
- Scout
- Lifecycle
- Analysis Commit
- USER_QUERY isolation
- event semantics
- notification semantics
- other frozen architecture

Do not add unrelated product features.

---

## 2. Target environment

Use:

```text
Ubuntu Server 24.04 LTS
x86_64
```

Production runtime must use Docker.

Do not rely on host-installed Python, Node.js, JDK, OpenClaw, Hermes Agent, control panels, or unrelated runtimes.

Install only what is required:

- Docker Engine official stable
- Docker Compose Plugin
- basic system/diagnostic tools
- reverse proxy / certificate tooling if required by the existing deployment design

---

## 3. Approved application scope

Deploy:

```text
Market Monitor P0
+
CR-002 Native TDX Provider
```

Primary Universe must remain:

| Market | Count |
|---|---:|
| Shanghai Main Board | 1,701 |
| STAR Market | 615 |
| Shenzhen Main Board | 1,494 |
| ChiNext | 1,406 |
| **Total** | **5,216** |

Primary Universe contamination must remain zero for:

- ETF
- index
- bond
- convertible bond
- B share
- Beijing Stock Exchange individual equity
- other unrelated instruments

Indexes and ETFs may exist as Context only.

---

## 4. Pre-deployment server checks

Before deployment, record:

```bash
uname -a
uname -m
cat /etc/os-release
nproc
free -h
df -h
timedatectl
```

Confirm:

- Ubuntu 24.04 LTS
- x86_64 architecture
- adequate CPU/RAM/disk
- DNS/network connectivity
- correct system time
- NTP synchronization

Apply normal Ubuntu security/package updates.

Do not perform a major OS release upgrade.

---

## 5. Docker installation

Install Docker Engine and Docker Compose Plugin from Docker's official supported Ubuntu repository.

Record:

```bash
docker version
docker compose version
```

Enable Docker at boot.

Do not introduce Kubernetes or another orchestrator.

---

## 6. Production persistence

Use the repository's existing canonical production paths if already defined.

Otherwise use a clear layout such as:

```text
/opt/market-monitor/
/var/lib/market-monitor/database/
/var/lib/market-monitor/artifacts/
/var/log/market-monitor/
/var/backups/market-monitor/
```

Requirements:

- container recreation must not delete SQLite
- container recreation must not delete artifacts
- logs must persist according to project policy
- backups must survive application-container replacement
- permissions must follow least privilege
- configuration must not be stored only inside ephemeral containers

---

## 7. Secrets and runtime configuration

Do not hard-code:

- passwords
- API keys
- webhook tokens
- certificate private keys
- provider credentials
- other secrets

External Webhook remains:

```text
OFF by default
```

Git remains uninitialized unless explicitly authorized later.

---

## 8. Domain and HTTPS

The Owner already has a domain.

Use:

```text
Domain
→ HTTPS
→ reverse proxy
→ Market Monitor Web/API
→ Docker internal network
```

Requirements:

- valid HTTPS certificate
- HTTP may redirect to HTTPS
- backend/internal ports must not be unnecessarily exposed publicly
- SQLite must never be exposed publicly
- Docker daemon/API must never be exposed publicly
- only required ingress ports should be opened
- preserve current application authentication/access-control behavior

Use the project's existing reverse-proxy/deployment design if present.

Do not create a second competing production architecture.

---

## 9. Docker Compose deployment

Use the repository's existing production Docker Compose design.

Required:

- deterministic startup
- production restart policy
- persistent volumes/bind mounts
- internal service networking
- health checks where supported
- production environment configuration
- no development-only bind mounts unless explicitly required

Start services and record:

```bash
docker compose ps
docker compose logs --tail=200
```

All required services must reach expected healthy/running state.

---

## 10. Native TDX production qualification

Before declaring production ready, validate:

- ServerPool
- reconnect
- health probes
- quote freshness
- index/ETF probes
- block capability
- failover behavior

All seven TDX capabilities must be healthy:

```text
SH_QUOTES
SZ_QUOTES
INDEX_QUOTES
ETF_QUOTES
MINUTE_BARS
DAILY_BARS
TDX_BLOCKS
```

TCP connection alone is not proof of a healthy TDX node.

Nodes returning:

- empty Quote results
- stale data
- parse failures
- identity mismatches
- repeated protocol errors
- invalid payloads

must not remain active healthy nodes.

---

## 11. Full-market live qualification

During a normal Shanghai/Shenzhen trading session, run at least 20 complete Primary Universe Quote sweeps.

Report:

- requested
- returned
- valid
- missing
- quarantined
- P50 sweep time
- P95 sweep time
- maximum sweep time
- timeout count
- disconnect count
- empty-response count
- parse-error count
- failover count
- active healthy-node count

Acceptance target:

```text
P95 <= 20 seconds
```

Preferred:

```text
P95 <= 15 seconds
```

A few explicitly invalid/no-valid-quote symbols may remain quarantined if they match frozen TDX semantics and do not degrade the whole sweep.

---

## 12. Market-phase-aware freshness

Freshness checks must respect market phase.

### During trading

- minute bars must remain appropriately fresh

### During lunch break

- lack of new bars during the break is not automatically stale

### After close

- final valid bar near normal market close is acceptable
- do not mark the source stale merely because no new minute bar appears after market close

Do not weaken active-trading freshness rules to hide real delays.

---

## 13. Frozen TDX data semantics

### Equity / ETF Quote

```text
price = CNY/share
last_close = authoritative previous close
vol = cumulative lots → ×100 into shares
amount = cumulative CNY
```

### 1-minute bars

```text
volume = interval shares
amount = interval CNY
```

### Daily bars

```text
volume = daily shares
amount = daily CNY
```

### Index

Use the dedicated index-volume normalization path.

### Invalid / no-valid-quote

Zero OHLC plus floating sentinel amount must remain:

```text
NO_VALID_QUOTE / MISSING
```

and must never become a zero-price market fact.

Validate request/response instrument identity.

---

## 14. Core application validation

Confirm real Native TDX data correctly flows through:

- Market State
- Guardian
- Scout
- Sector / Context views
- Events
- Notifications
- System/Data Health
- Current State projections
- relevant APIs
- Web UI

Do not modify Guardian/Scout thresholds merely to make first-run output look better.

An empty event/opportunity list is valid if no legitimate event exists.

---

## 15. Full verification suite

Run the repository's canonical verification command:

```bash
python scripts/dev.py verify
```

Also run any currently required production/deployment verification command defined by the repository.

All accepted baseline capabilities must remain intact, including:

- Python tests
- Web unit tests
- Chromium/browser flows
- demo/replay
- backup/restore
- clean-copy verification where applicable

---

## 16. Backup and restore verification

Verify production backup:

- backup command succeeds
- backup artifact exists
- required SQLite/application state is included
- referenced artifacts follow frozen backup contract
- running DB is not corrupted

Perform a controlled restore verification using the project's existing restore/recovery process.

Do not overwrite live production state without first creating a safe recovery point.

---

## 17. Restart validation

Perform application restart using the canonical project procedure, for example:

```bash
docker compose restart
```

Confirm:

- services recover
- persistent data remains
- Native TDX reconnects
- health returns to normal
- no duplicate event/notification side effects occur merely from restart

---

## 18. Server reboot validation

Perform one controlled server reboot after deployment is complete.

After boot confirm:

- Docker starts automatically
- Market Monitor starts according to production policy
- persistent database/artifacts remain intact
- Native TDX reconnects
- Web/API becomes healthy
- HTTPS/domain access returns
- no development-only manual command is required to restore service

---

## 19. Resource validation

After production startup record:

```bash
docker stats --no-stream
free -h
df -h
```

Report:

- CPU usage
- memory usage
- swap usage
- disk usage
- database size
- artifact size
- log size
- backup size

Do not leave the server near resource exhaustion.

---

## 20. Logging and disk protection

Enable or confirm log rotation.

The system must not allow unlimited logs to consume the production disk.

Verify:

- application log retention
- Docker log retention
- backup retention
- disk-space monitoring/alert threshold

Do not delete canonical market data or required audit artifacts merely to reduce disk usage.

---

## 21. Minimum production security

At minimum:

- apply current Ubuntu security updates
- use SSH keys where available
- disable unnecessary services
- do not expose SQLite
- do not expose Docker daemon/API
- do not unnecessarily expose backend-only ports
- use HTTPS
- restrict permissions on secrets
- limit firewall/security-group rules to required access

Do not add unrelated security products that materially change deployment architecture.

---

## 22. No-silent-change rule

Deployment must not silently alter:

- Primary Universe definition
- Guardian semantics
- Scout semantics
- TDX field semantics
- risk/opportunity tags
- notification behavior
- Analysis Commit behavior
- API domain semantics
- database truth model

If a frozen semantic change appears necessary, stop that change and report it as a proposed new Change Request.

---

## 23. Production acceptance gate

Production deployment is complete only when all are true:

```text
Ubuntu 24.04 verified
Docker / Compose verified
Market Monitor services healthy
HTTPS / domain works
persistent data confirmed
Primary Universe = 5,216
non-primary contamination = 0
7/7 TDX capabilities healthy
20 live sweeps completed
TDX P95 <= 20s
Guardian / Scout / UI / API functional
full verify passes
backup verified
restore verified
container restart verified
server reboot recovery verified
resource usage acceptable
no unresolved blocker
```

---

## 24. Final deployment report

Create:

```text
docs/archive/progress/PRODUCTION_DEPLOYMENT_PREPARATION_REPORT.md
```

The final report must include:

1. server OS / architecture
2. CPU / RAM / disk
3. Docker / Compose versions
4. deployment path
5. domain / HTTPS status
6. container/service status
7. persistence paths
8. Primary Universe count and composition
9. TDX 7/7 capability status
10. 20-sweep P50 / P95 / Max
11. timeout/disconnect/failover results
12. Guardian / Scout / UI / API checks
13. verification test results
14. backup / restore result
15. restart result
16. reboot recovery result
17. CPU / memory / disk utilization
18. known limitations
19. unresolved blockers, if any
20. exact production access URL
21. start / stop / restart commands

End with exactly:

```text
PRODUCTION DEPLOYMENT COMPLETE — READY FOR OWNER USE
```

After completion, keep the production service running.

Do not continue adding features.
