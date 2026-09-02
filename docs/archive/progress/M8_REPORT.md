# M8 Progress Report

**Milestone:** M8 - Responsive Protection-first Web
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- Responsive React client for home, sector directory/detail, events, notifications, system health,
  OWNER login, active analysis, notification settings, and not-found routes.
- Fixed protection order: judgment availability, reference level, Guardian, current lifecycle,
  Scout, supporting/contrary evidence, data limitations, and Last Valid historical reference.
- Same-origin typed API client with credentials, ETag/304 cache semantics, no-store writes, CSRF,
  If-Match, and logical-operation idempotency keys.
- Abortable/polling resources with explicit ready, loading, offline, stale-cache, service-error, and
  successful-empty states. High-growth collections use explicit opaque-cursor “load more” actions.
- API Contract v1.3 Final Baseline under approved CR-001. It preserves `/api/v1` and byte-exact
  v1.2 history while adding canonical nullable AnalysisSubject identity/status to `SectorView` and
  `SectorMatchCandidateView` plus an OWNER diagnostic missing-mapping count.
- Sector state, facts, transitions, related events, and USER_QUERY use only a returned canonical
  VALUE `subject_uid`. MISSING and inconsistent pairs show “当前板块暂不可进行正式分析” and make
  no analysis call.
- Event labels use only strict canonical subject equality against the bounded first Sector page;
  unresolved subjects stay generic and raw UIDs are never displayed.
- Mobile-first layout preserves critical content at 390, 768, and 1280 px, with keyboard focus,
  skip navigation, route titles/announcements, back-forward handling, and reduced motion.

## Files and interfaces introduced

- `docs/architecture/API_CONTRACT_V1.3.md`: complete current additive Final Baseline.
- `openapi/history/market-monitor-v1.2.yaml`: byte-exact machine-readable v1.2 history.
- `openapi/market-monitor-v1.yaml`: active v1.3 contract; route/method surface unchanged.
- `apps/api/src/market_monitor_api/repository.py`: read-only Sector canonical-subject projection and
  missing-mapping aggregate.
- `apps/api/src/market_monitor_api/queries.py`: read-only canonical identity on exact/ambiguous
  SectorMatch candidates; USER_QUERY write semantics remain unchanged.
- `apps/api/src/market_monitor_api/app.py`: API metadata version 1.3 and existing OWNER diagnostics
  response extended by `sector_subject_mapping_missing_count`.
- `apps/web/src/api/generated.ts`: generated v1.3 types.
- `apps/web/src/pages/SectorsPage.tsx`: guarded canonical-subject child boundary, committed
  protection sections, independent retries, explicit fact/transition pagination, and bounded
  first-page related-event filtering.
- `apps/web/src/pages/AnalysisPage.tsx`: explicit exact/ambiguous Sector selection and canonical
  USER_QUERY with logical idempotency-key reuse.
- `apps/web/src/hooks/useSectorSubjectLabels.ts`: bounded strict reverse label map for Home/Events.
- Web client, auth, routing, presentation, page, style, Vitest, and Playwright files documented in
  `tasks/M8_IMPLEMENTATION_PLAN.md`.

## Migrations and schemas

- Database migrations/tables: none.
- Guardian, Scout, Lifecycle, Analysis Commit, event, notification, transaction, and persistence
  schemas: unchanged.
- Public compatibility: v1.3 is additive; `/api/v1` is unchanged. Tests pin both v1.2 Markdown
  SHA-256 values and the historical v1.2 OpenAPI hash, compare path/method sets, and allow only the
  approved schema additions.

## Test-first and verification evidence

- Contract/history RED: `3 failed, 16 passed`; missing v1.3/history artifacts caused the failures.
  GREEN: `19 passed in 4.31s` after publishing v1.3 and preserving history.
- API projection RED: three intended failures identified absent Sector fields/diagnostic method.
  GREEN includes VALUE, MISSING, all-mapped zero, unversioned exclusion, GET/match/diagnostics read
  side-effect checks, exact/ambiguous matching, and returned-sector mutation isolation.
- Analysis Web RED rounds: `7 failed, 6 passed`, then hardening `3 failed, 13 passed`; GREEN covers
  exact/ambiguous selection, MISSING/inconsistent identity, canonical payload, concurrent blocking,
  lost-response idempotency reuse, and candidate-key rotation.
- Sector Web regressions cover canonical UID distinct from Sector UID, protection order, strict
  related events, independent state/fact/transition/event retry, explicit fact/transition cursors,
  and loading/error versus empty semantics.
- Current targeted M7 command `python scripts/dev.py test-m7`: exit `0`, `71 passed` in `26.38s`.
- Current repository/history command `.venv\Scripts\python.exe -m pytest -p no:cacheprovider
  tests/test_repository_check.py -q`: exit `0`, `9 passed in 0.67s`.
- Current CR Web command using Vitest on six affected files: exit `0`, `6 files`, `43/43` tests in
  `21.85s`.
- Current M8 command `python scripts/dev.py test-m8`: exit `0`, `21` unit files with `111/111`
  tests in `4.55s`, and Chromium `8/8` in `9.7s`.
- Current local `python scripts/dev.py verify`: exit `0` in `82.8s`; repository, format, lint,
  Python/Web type checks, and production build passed; Python `190/190` in `62.26s`, Web `111/111`
  in `4.58s`, and Chromium `8/8` in `9.4s`. Vite built `36` modules and a `260.13 kB`
  main JavaScript artifact (`78.53 kB` gzip).
- Clean-copy root:
  `%TEMP%\market-monitor-m8-1786525212398-680770\market-monitor`.
  Robocopy exit `1` means files were copied successfully; the pre-install audit found zero excluded
  directories and `GIT_PRESENT=False`. `python scripts/dev.py install` exited `0` in `71.7s`,
  installed pip `26.2`, all locked Python dependencies, `119` npm packages, and Chromium.
  Clean-copy `python scripts/dev.py verify` exited `0` in `114.4s`; repository/format/lint/type/build
  gates passed, Python `190/190` in `64.54s`, Web `111/111` in `21.01s`, and Chromium `8/8` in
  `9.4s`.

Four command-invocation failures were not counted as product failures or completion evidence:

- Combining individual M7 files with a root-level test omitted `tests/m7/conftest.py`; 23 tests
  passed and 27 errored before bodies ran because `m7_runtime` was absent. The established
  `python scripts/dev.py test-m7` entry then passed all 71 tests.
- A sandboxed direct Vitest launch could not write Vite's dependency-cache temporary file. The
  approved project-local cache write rerun passed all 43 targeted tests.
- Sandboxed Biome `--write` could read but could not open even an unchanged file for writing on this
  Windows host. The approved rerun outside that restriction formatted 117 Python and 54 Web files;
  no file needed a formatting change.
- The first sandboxed `python scripts/dev.py test-m8` launch hit the same Vite `.vite-temp` EPERM.
  Its approved rerun is the green 111-unit/8-browser result above.

The first post-document `python scripts/dev.py verify` correctly stopped at the repository check
because this report contained a machine-specific absolute clean-copy path. The path was replaced by
the portable `%TEMP%` form before the full gate was rerun; no source or frozen contract changed.
The corrected post-document rerun exited `0` in `81.5s`: Python `190/190` in `63.12s`, Web
`111/111` in `4.36s`, and Chromium `8/8` in `8.2s`, with every repository, format, lint, type, and
build check green.

No skipped, expected-failure, placeholder, or empty-assertion test is counted as completion evidence.

## Normal, degraded, failure, restart, and recovery behavior

- Healthy committed responses render without client-side recomputation. A valid 200
  SUSPENDED/WARMING_UP/UNAVAILABLE answer is shown as no judgment, not transport failure.
- A 304 reuses only its matching memory cache; a 304 without cache is a visible protocol error.
  Sensitive responses and writes are not cached.
- Offline with retained data keeps dated stale content; offline without data, failed refresh, API
  failure, loading, and successful empty are distinct.
- Navigation aborts old reads and ignores late responses. State, facts, transitions, event history,
  members, and instruments recover independently without discarding successful sibling sections.
- Fact/transition pages advance only on an explicit action. Related-event lookup intentionally stops
  at the first event page and visibly states incompleteness when another cursor exists.
- A lost protected-write response preserves body and idempotency key; changed payload or new
  operation rotates it. If-Match conflict reloads server truth instead of force-overwriting.
- Session-service failure is not mislabeled as bad credentials. HTTP 401 clears tab write state;
  logout failure leaves the possibly valid session visible.
- M8 adds no persistence recovery behavior. M1–M7 WAL/FULL, writer queue, Analysis Commit, Outbox,
  restart, and restore-generation guarantees remain unchanged and fully regress under `verify`.

## Security, durability, accessibility, and product language

- Repository scanner passed. Focused scans found no prohibited trading/guarantee copy, unsafe HTML,
  Web SQLite access, AnalysisSubject creation, client market calculation, or Sector/name/code UID
  fallback.
- Sector list/detail/match/diagnostics use read connections and canonical `LEFT JOIN` projections.
  The `sector_match` function contains no write; existing USER_QUERY writes remain authenticated,
  CSRF/idempotency-protected, audited, and isolated from official truth.
- Server text renders through React text nodes. Unknown reason/fact/unit/subject values use bounded,
  non-inferential Chinese fallbacks.
- Status has text, not color alone. `lang=zh-CN`, semantic regions/headings, visible focus, skip
  navigation, reduced motion, and 390/768/1280 content parity have real Chromium coverage.
- CR-001 changes no SQLite durability setting, table, migration, writer path, transaction boundary,
  event, or notification behavior.

## Deviations and Change Requests

- CR-001 was approved by the owner on 2026-08-12 and implemented as API Contract v1.3 Final
  Baseline. It is backward-compatible and additive; v1.2 history was not edited in place.
- The OWNER diagnostic count is the minimum implementation of the requirement that expected
  analyzable mapping gaps not remain silent. It does not create an incident, change readiness, list
  UIDs, or repair data.
- `ARCHITECTURE_FREEZE.md` and the two historical v1.2 Markdown files remain byte-identical. Mutable
  authority/index files point to v1.3; no frozen file was silently overwritten.
- Git acceptance is skipped under explicit owner authorization; Git was not initialized or used.

## Risks, known limitations, rollback, and external activation

- Event-to-Sector labels use one bounded Sector page. A subject outside that page remains generic;
  the client does not fetch unbounded pages or claim the label map is complete.
- Sector related events use the first ascending event page. If another cursor exists, the UI marks
  the result incomplete; it does not call it “recent” or silently auto-page.
- Browser tests use deterministic contract mocks and prove client behavior, not live-provider,
  public-network, hosted-CI, or production-browser success.
- The client has no durable offline store; retained stale content is page-lifetime only.
- A nonzero mapping diagnostic requires the approved reference-data workflow to establish the
  missing SECTOR AnalysisSubject. Reads never auto-repair it; returning the count to zero is the
  recovery evidence.
- Rollback of v1.3 requires a new approved superseding Change Request/version while preserving v1.3
  history. No database rollback applies because CR-001 added no migration or write.
- Live A-share operation still requires owner selection/licensing/credentials for a provider.
  External webhook delivery remains disabled until owner configuration. Public release and the
  final open-source license remain M9/owner decisions.

## Gate conclusion

All amended M8 code, targeted tests, browser journeys, scans, local full verification, clean-copy
installation/verification, and documentary gates are green. `.git` remained absent, and M9 files
were absent when this gate closed. M8 passed before any M9 implementation work began.
