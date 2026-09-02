# CR-001 — Expose canonical sector analysis-subject identity

**Status:** APPROVED — 2026-08-12
**Affected milestone:** M8 Responsive Web
**Frozen documents changed by this proposal:** none

The project owner approved the recommended solution and the seven constraints recorded in
`docs/architecture/API_CONTRACT_V1.3.md`. API Contract v1.3 is the Final Baseline; `/api/v1`
remains unchanged and both v1.2 Markdown files remain byte-for-byte untouched.

## 1. Historical v1.2 problem and governing specifications

The following approved requirements must all be satisfied:

- `docs/architecture/API_CONTRACT_V1.2.md` sections 3, 6, 11, and 12 expose sector resources,
  subject-state resources, committed Home data, and `AnalysisQueryRequest.subject_uid`.
- The consolidated architecture baseline requires sector detail to show members, evidence,
  state transitions, and event history, and requires active queries.
- The acceptance material requires the sector list/detail Web journey.
- The architecture baseline distinguishes a Sector target from its AnalysisSubject identity.

Before CR-001, the frozen v1.2 OpenAPI `SectorView` and `SectorMatchCandidateView` exposed
`sector_uid` but not the canonical `analysis_subject.subject_uid`. Conversely, `MarketView` and
event views exposed `subject_uid` but no sector target identity or display name. The database
correctly stored—and continues to store—these as different stable UIDs.

Consequently, a conforming v1.2 Web client could not:

1. navigate from a sector to `/api/v1/subjects/{id}/state`, facts, or transitions;
2. submit the selected sector through `AnalysisQueryRequest.subject_uid`;
3. map an event's `subject_uid` back to the sector name for a user-readable timeline.

Passing `sector_uid` to `/subjects/{id}` is not defined by the current contract and fails in the
current repository implementation. Silently accepting it would be an externally observable input-
semantic expansion and would still not solve the reverse event-to-sector label mapping.

## 2. Why v1.2 alone could not satisfy M8

The Web layer is prohibited from reading SQLite, guessing UID relationships, or creating domain
state. No v1.2 API route or field exposed the Sector ↔ AnalysisSubject relationship. Client-side
matching by names or by unrelated event ordering would have been non-deterministic and violated the
frozen identity model. Omitting state/evidence/transitions/query from sector detail would have
failed the explicit M8 deliverable.

This was the only architecture blocker for sector-to-analysis linkage and friendly event subject
labels. CR-001 resolves it without changing other M8 behavior.

## 3. Approved and implemented minimal change

API Contract v1.3 implements the approved resolution while preserving every existing path and
field:

1. It adds nullable `subject_uid` plus required `subject_uid_status: ValueStatus` to `SectorView`.
2. It adds the same two fields to `SectorMatchCandidateView`.
3. Read projections populate them by a left join to the unique `analysis_subject.sector_uid`
   relationship. A sector
   without an AnalysisSubject returns `null` and `MISSING`; it is never auto-created by a GET.
4. `/api/v1/subjects/{id}` stays strict: `{id}` remains the canonical AnalysisSubject UID.
5. TypeScript types are regenerated and contract/repository tests cover present and missing
   mappings.

The Web uses `SectorView.subject_uid` for state/facts/transitions and builds the reverse
`subject_uid → sector name` map from the paged sector directory. Sector match can submit only the
server-returned canonical subject UID. A missing mapping has an explicit no-analysis state rather
than a guessed identity.

No route, enum, database table, migration, rule, transaction boundary, or event/notification
semantics change.

## 4. Impact

### Database and migrations

- No schema or data migration. The existing unique `analysis_subject.sector_uid` relationship is
  read only.

### API and generated types

- `openapi/market-monitor-v1.yaml`: two schemas gain a nullable stable UID and ValueStatus pair.
- OWNER-only `DiagnosticsView` gains the non-negative
  `sector_subject_mapping_missing_count` integrity count. Its read-only aggregate counts directory
  Sectors with no canonical SECTOR AnalysisSubject and does not expose UID lists or alter readiness.
- API View Models/repository queries populate the mapping without mutating reference data.
- `apps/web/src/api/generated.ts` is regenerated from the approved source contract.
- Existing routes, request fields, status codes, cache/security behavior, and current fields remain.

### Rules, state, events, and notifications

- No calculation or state change. Guardian, Scout, Lifecycle, Analysis Commit, Outbox, and frozen
  notification content are untouched.

### Tests

- OpenAPI/schema generation drift and runtime parity tests.
- Repository tests for mapped and unmapped sectors and sector-match candidates.
- Web tests proving the canonical UID is used and missing mapping is shown explicitly.

### User experience

- Sector pages can show their committed state and evidence without guessing identity.
- Events can display the sector name when the subject maps to a sector.
- Unmapped sectors clearly state that no analysis subject is available.

## 5. Alternatives considered

### A. Accept sector/instrument UID aliases on `/subjects/{id}`

Rejected. It changes path input semantics without declaring the change, creates collision/precedence
questions, includes unrequested instrument behavior, and does not provide reverse display labels.

### B. Add new subject-resolution endpoints

Not recommended for P0. Separate target-to-subject and subject-to-target routes add more public
surface, caching, tests, and client round trips than two explicit fields.

### C. Reuse `sector_uid` as `subject_uid`

Rejected. The physical and domain models intentionally assign separate stable identities.

### D. Omit sector state/evidence/query or infer the relationship in Web

Rejected. Omission fails M8 acceptance; inference violates the identity and frontend boundaries.

## 6. Rollback path

The v1.3 Final Baseline must not be edited in place to remove public fields. A rollback requires a
new approved Change Request and superseding contract version which retains the v1.3 history,
followed by removal of the read projections/aggregate, regenerated client types, and disabling the
affected sector-analysis UI. No database rollback is needed because CR-001 introduced no migration
or write. M8 cannot remain accepted under such a rollback unless the superseding contract provides
an equivalent canonical identity path.

## 7. Missing-mapping operational response

A nonzero `sector_subject_mapping_missing_count` is an OWNER diagnostic that requires investigation
of reference-data onboarding. Until the approved reference-data workflow establishes the missing
SECTOR AnalysisSubject mapping, the affected Sector remains visibly unavailable for formal
analysis. GET, sector match, Web, and diagnostics never repair it automatically. P0 intentionally
adds no public repair route; operators must correct the reference-data input/workflow and rerun the
diagnostic. The count returning to zero is the recovery evidence.

## 8. Decision

Approved by the project owner on 2026-08-12. Implementation remains limited to the recommended
additive field pairs, canonical read-only mapping, explicit missing-state behavior, and integrity
diagnostics described by API Contract v1.3. M8 must pass its amended gate before M9 begins.
