# Privacy and local-data handling

Market Monitor is designed for local operation. Its SQLite database, content-addressed Artifacts,
backup sets, audit records, session records, notification history, and locally configured OWNER
account are sensitive local data. Treat the data directory and backups with the same care as other
private financial-monitoring records.

The credential-free demo/replay mode uses only checked-in deterministic fixture data. It does not
activate a live market provider and does not make webhook calls. A configured webhook can transmit
the immutable notification context to the owner-selected endpoint only after the endpoint is
configured and delivery is explicitly enabled. The owner is responsible for that endpoint's privacy
terms, retention, and network controls.

Do not commit or share `.env`, password files, cookies, CSRF values, webhook endpoints, backups,
database copies, Artifacts, or machine-specific diagnostics. Error and reconciliation outputs are
designed to avoid secrets, payloads, UIDs, and absolute paths; nevertheless, reports should be
reviewed before sharing.

Backup and restore are local operational tools, not a deletion guarantee. Retention defaults to a
dry-run and deliberately preserves protected evidence, corrections, events, notification history,
and auditability. When disposing of local data or backups, use an owner-approved secure deletion or
storage-retention process appropriate to the host platform.
