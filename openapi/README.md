# OpenAPI

`openapi/market-monitor-v1.yaml` is the active API Contract v1.3 Final Baseline. It remains under
the `/api/v1` route namespace and is a backward-compatible additive successor to v1.2. The exact
pre-change v1.2 source is preserved at `openapi/history/market-monitor-v1.2.yaml`.

Both files use JSON syntax, which is valid YAML 1.2, so the standard library can verify them without
another parser dependency. Do not modify the historical snapshot.

Generate the checked-in Web types with:

```text
python scripts/generate_api_types.py
```

Check for source/generated drift without writing files with:

```text
python scripts/generate_api_types.py --check
python scripts/check_repository.py
```

Do not edit `apps/web/src/api/generated.ts` by hand. Contract tests compare v1.3 with the historical
v1.2 source, compare the source path and method surface with FastAPI, and validate representative
runtime payloads against the source schemas.
