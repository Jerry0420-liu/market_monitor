# M8 Pre-CR-001 Historical Report

**Milestone:** M8 - Responsive Protection-first Web
**Historical status:** BLOCKED — recorded before CR-001 approval; superseded by `M8_REPORT.md`
**Next milestone started before this gate:** No

## Scope completed

- Responsive React client for home, sector directory/detail, events, notifications, system health,
  OWNER login, active analysis, notification settings, and not-found routes.
- Fixed protection order: judgment availability, reference level, Guardian, current lifecycle,
  Scout, supporting/contrary evidence, data limitations, and Last Valid historical reference.
- Same-origin typed API client with credentials, ETag/304 cache semantics, no-store writes, CSRF,
  If-Match, and logical-operation idempotency keys.
- Abortable/polling resources with explicit ready, loading, offline, stale-cache, and service-error
  states; opaque cursor pages load only after a user request.
- Plain-language presentation for frozen enums, evidence templates, data limitations, capabilities,
  and unknown safe fallbacks. No raw exception, stack, local path, password, endpoint value, or
  internal machine code is rendered.
- Mobile-first layout with the same critical content at 390, 768, and 1280 px; visible focus,
  skip navigation, route titles/announcements, back-forward focus handling, and reduced motion.
- OWNER flows preserve the HttpOnly cookie boundary, store only the CSRF token in tab-scoped
  session storage, and keep a lost-response retry on the same idempotency key while rotating the
  key for a new operation.

The frozen API does not expose Sector ↔ AnalysisSubject identity. The affected sector state,
facts, transitions, related-event label, ambiguity selection, and sector-selected active analysis
remain visibly disabled. No Sector UID is passed as a Subject UID and no relationship is inferred.

## Files and interfaces introduced

- `apps/web/src/api/`: generated-contract resources, same-origin client, ETag cache, protected-write
  guards, and logical-operation keys.
- `apps/web/src/auth/AuthContext.tsx`: OWNER session restoration, tab-scoped write protection,
  safe login/logout behavior, and distinct authentication-service failures.
- `apps/web/src/hooks/`: abort/race-safe resources, polling, stale retention, and cursor pages.
- `apps/web/src/components/`: application shell, resource notices, and reusable protection views.
- `apps/web/src/pages/`: all implemented M8 routes and the explicit CR-001 sector limitation.
- `apps/web/src/presentation.ts`: direct enum/reason/code-to-user-language mappings only; no market
  rule, threshold, Guardian, Scout, Confidence, or lifecycle calculation.
- `apps/web/src/styles.css`: responsive, focus-visible, overflow-safe, and reduced-motion styling.
- `apps/web/e2e/web.spec.ts` and `apps/web/playwright.config.ts`: deterministic local Chromium
  journeys with no external or production API call.
- `scripts/dev.py`, package manifests/lock, CI, `.gitignore`, and `scripts/check_repository.py`:
  pinned Playwright 1.62.1, Chromium installation, M8/full-test wiring, and required-artifact checks.
- `README.md`, `apps/web/README.md`, this report, and the M8 implementation plan: usage, routes,
  limitations, test commands, and traceability.

No public API route/schema/enum, database migration/table, market rule, Guardian/Scout behavior,
event/notification behavior, Analysis Commit, Outbox, persistence setting, live provider, secret,
deployment, license, or M9 operation was introduced.

## Test-first and verification evidence

- Route focus regression: Playwright first failed because StrictMode focused the initial heading;
  a StrictMode unit regression then failed `1/2`. URL-change focus management and next-frame focus
  restoration made the scoped unit suite `3/3` and Chromium keyboard/history test `1/1` pass;
  the complete browser suite subsequently passed `7/7`.
- Authentication/error/plain-language audit: eight affected files first produced `12 failed,
  20 passed`. After distinguishing session service errors, surfacing logout failure safely,
  removing internal terms/codes, and setting the document language, the scoped suite passed
  `40/40`; the browser suite remained `7/7`.
- Sector per-section recovery: `SectorsPage.test.tsx` passed `3/3`, including an isolated failed
  member read and retry without discarding sector metadata or the identity-blocker section.
- M7 corrective security revalidation: `python scripts/dev.py test-m7` exited `0`, `68 passed in
  24.14s`; one sandbox cache-write warning did not skip or fail a test.
- Final M8 target run `python scripts/dev.py test-m8` exited `0`: `20` unit files and `87/87`
  tests passed in `4.11s`; all `7/7` Chromium tests passed in `6.7s`.
- Repository-scanner regressions passed `7/7` in `0.72s`. The scanner still detects a quoted live
  secret fixture, ignores identifier references, and the complete repository check reported
  `repository checks passed`.
- The first closing `python scripts/dev.py verify` correctly exited `1` at Web lint with `5`
  errors and `14` warnings. The findings were explicit button types, unsupported ARIA, intentional
  resource-key dependencies, unused/type-only imports, selector specificity, and reduced-motion
  overrides. After semantic fixes, race guards, selector simplification, and narrowly justified
  reduced-motion suppressions, Web lint reported no errors or warnings; affected tests passed
  `12/12`.
- Final local `python scripts/dev.py verify` exited `0` in `79s`: repository, Python/Web format,
  Python/Web lint, Python/Web type checks, production build, `185/185` Python tests in `60.88s`,
  `20` Web files with `87/87` unit tests in `4.34s`, and `7/7` Chromium tests in `6.7s`. Mypy
  checked `105` source files. Vite built `35` modules; the main JavaScript artifact was `251.64 kB`
  (`76.35 kB` gzip).
- Clean-copy installation and verification exited `0` at
  `%TEMP%\market-monitor-m8-13ec1c6cc0d24c5ea4f5506dd14e3b67\market-monitor`.
  Robocopy exited `1` (files copied, no failure), the copy contained no `.git`, pip `26.2`, all
  pinned Python/Node dependencies, and Chromium installed from the lock/configured toolchain.
  Clean-copy verify passed repository/format/lint/type/build gates, `185/185` Python tests in
  `62.18s`, `87/87` Web unit tests in `20.99s`, and `7/7` Chromium tests in `6.2s`.

All independently executable M8 technical and clean-copy gates are green. M8 still cannot pass
while CR-001 remains unapproved because the frozen acceptance journey requires the absent canonical
Sector ↔ AnalysisSubject mapping.

## Normal, degraded, failure, restart, and recovery behavior

- Healthy committed responses render without client-side recomputation. A valid 200
  SUSPENDED/WARMING_UP/UNAVAILABLE answer is shown as no judgment, not as a transport failure.
- A 304 reuses only its matching in-memory cached representation; a 304 without cache is a visible
  protocol error. Sensitive responses and writes are never cached.
- Offline with retained data keeps the dated content and marks it stale; offline without data,
  stale-after-refresh, and API failure are distinct. API failures expose only a bounded request ID.
- Navigation aborts old reads and ignores late responses. Each independent resource can retry;
  member retry does not reload or erase other sector sections.
- A lost write response preserves its request body and idempotency key. A changed payload or new
  completed operation rotates the key. If-Match conflict reloads server truth instead of forcing an
  overwrite. HTTP 401 clears tab write state; 409 and 429 retain distinct user messages.
- Session-service failure is not mislabeled as a password/login failure. Logout failure leaves the
  current session visibly present and warns that it may still be valid.
- Browser refresh relies on the HttpOnly session cookie and tab-scoped CSRF value only; passwords,
  endpoint values, ETags, and market results are not persisted to long-lived storage.

M8 introduces no database durability or notification-recovery behavior. M1-M7 restart, SQLite
WAL/FULL, Analysis Commit, Outbox, and restore-generation guarantees remain unchanged.

## Security, accessibility, and product-language analysis

- Source and rendered-flow scans found no trading instruction, recommendation, target, position,
  profit guarantee, unsafe HTML insertion, client-side market calculation, raw stack/path/secret,
  password/endpoint persistence, or hidden responsive protection content.
- Server text is rendered through React text nodes. Evidence descriptions use the finite approved
  reason/template dictionary; unknown keys receive a non-inferential Chinese fallback and arbitrary
  attributes are not interpolated.
- The document language is `zh-CN`; status is always expressed in text, never by color alone.
  Static fetched content is not placed in live regions. Dynamic loading/offline/stale/error/session
  feedback uses bounded status or alert regions.
- Initial full-document load preserves the skip link as the first keyboard stop. Actual SPA route
  changes update the title, announce the route, and focus the new main heading, including browser
  back/forward navigation under React StrictMode.

## Deviations and Change Requests

- `docs/decisions/CR-001_SECTOR_SUBJECT_IDENTITY.md` is `PROPOSED — OWNER AND ARCHITECTURE APPROVAL
  REQUIRED`. It recommends adding nullable canonical `subject_uid` plus ValueStatus to Sector and
  SectorMatch views without a migration, new route, enum, or rule change.
- No proposed OpenAPI edit or generated-type change has been implemented. This is the sole known
  architecture-blocking scope. Omitting the affected journey would fail M8 acceptance; guessing it
  would violate the frozen identity and Web boundaries.
- No frozen document was changed. Git acceptance remains intentionally skipped and Git was not
  initialized.

## Risks, known limitations, and rollback

- M8 is not PASS. Sector committed state/evidence/transitions, related event labels, ambiguity
  selection, and sector-selected active analysis remain unavailable until CR-001 is approved and
  implemented, or an approved alternative resolves the contract conflict.
- Browser tests use deterministic contract mocks. They prove client behavior, not live-provider,
  public-network, hosted-CI, or production-browser success.
- The client has no durable offline store; retained stale content exists only for the current page
  lifetime. This is intentional P0 behavior.
- Rollback is limited to removing the M8 Web routes/assets/test wiring and restoring the previous
  placeholder. No database/API migration rollback is required. CR-001 itself contains the rollback
  for its proposed additive fields.

## External activation requirements

- Live A-share operation requires owner selection, licensing, and credentials for a market-data
  provider. The repository enables only deterministic fixture/replay behavior.
- External webhook delivery requires an owner-configured endpoint plus explicit OWNER enablement;
  no real endpoint or secret is stored in the repository or Web client.
- Public exposure, production packaging, and the open-source license remain owner/M9 decisions and
  are not claimed.

## Gate conclusion

All independent M8 implementation work and its local plus clean-copy technical gates are complete;
M9 has not started. M8 cannot be accepted and M9 cannot begin until CR-001 receives the required
owner and architecture approval and the approved mapping path is implemented and verified. No
technical failure is being hidden or treated as an expected pass.
