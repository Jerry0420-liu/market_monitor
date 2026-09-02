# Development

Contribution workflow is documented in [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md).

Use the pinned toolchain:

```text
python scripts/dev.py install
python scripts/dev.py verify
```

Tests use small deterministic fixtures and temporary databases. Runtime data belongs under
[`../../runtime/`](../../runtime/) and is never source-controlled. Current acceptance state is
centralized in [`../acceptance/CURRENT_STATUS.md`](../acceptance/CURRENT_STATUS.md).
