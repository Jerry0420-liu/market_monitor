# Release candidate notes

> Owner-decision update (2026-08-21): Native TDX and the MIT License are selected. The only
> remaining release inputs are TDX data-rights/endpoint policy, any intentionally enabled webhook,
> and any approved remote/public exposure.

This release candidate is local-first and protection-first. Guardian remains authoritative over
Scout, official state is committed through Analysis Commit and the Transactional Outbox, and the
product does not provide trading execution, buy/sell instructions, position sizing, target prices,
or profit guarantees.

Included local capabilities are deterministic demo/replay, complete SQLite plus Artifact backups,
offline recovery with old-notification suppression, reconciliation, evidence-aware retention,
bounded maintenance, capacity verification, same-origin Web serving, and loopback-only startup.

Not activated or chosen: live market-data provider, paid account, real webhook endpoint, public
hosting, remote access, and public-use license. These remain owner inputs. Docker Compose is a
hardened Linux packaging option; native loopback deployment is the acceptance path where Docker
host networking is unavailable.
