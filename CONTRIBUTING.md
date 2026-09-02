# Contributing

Keep changes focused and preserve the protection-first flow documented in
[`docs/architecture/`](docs/architecture/). Do not commit credentials, local `.env` files,
machine paths, dependency directories, generated build output, or runtime data.

Install the pinned toolchain and run the repository gate before review:

```text
python scripts/dev.py install
python scripts/dev.py verify
```

Use [`docs/decisions/`](docs/decisions/) for approved contract changes and
[`docs/operations/`](docs/operations/) for deployment or recovery procedures.
