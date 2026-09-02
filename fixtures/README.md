# Fixtures

`fixtures/` contains deterministic test and replay inputs only. It must not contain live runtime
SQLite databases, provider credentials, private logs, or production artifacts.

`m9/demo_replay.json` is the credential-free replay input used by the documented local demo and
offline recovery verification. New fixtures must be small, versioned, reproducible, and safe to
publish.
