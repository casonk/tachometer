# Contributing

## Expectations

- Keep `clockwork` dependency-light and deterministic.
- Prefer additive manifest fields over hidden behavior.
- Keep scheduler rendering separate from workload logic. If a feature belongs to
  a repo's own wrapper script, document the boundary instead of moving it here.
- Update the README and examples when the manifest schema changes.
- Keep tests aligned with real downstream scheduler patterns from the portfolio.

## Local Validation

```bash
pip install -e .[dev]
ruff check .
ruff format --check .
black --check --diff .
pytest -q
```

## Repo Baseline

This repository follows the portfolio standards in `./util-repos/traction-control`
from the portfolio root.
