# Contributing

Thanks for helping improve SuiteHarness.

## Development setup

Use Python 3.11 or newer and create an isolated environment.  This repository
uses source-checkout development: provision the dependencies declared in
`pyproject.toml` with your company's dependency tool, but do not install SuiteHarness
Harness itself as an editable/site package.  Point Python at `src` explicitly:

```bash
python -m venv .venv
export PYTHONPATH=src
```

On PowerShell, use `$env:PYTHONPATH = "src"`.  The virtual environment must
contain the core dependencies plus the `dev` tools before running checks.

Run the required checks before submitting a pull request:

```bash
python -m ruff check .
python -m pytest
python -m build --outdir dist .
python scripts/check_distributions.py dist
```

Every behavior change should include a test. Security boundaries require a
negative test proving that the forbidden operation does not happen. New public
protocols and reference adapters must document lifecycle, timeout,
cancellation, idempotency, tenant/product scoping, and failure behavior.

Keep the repository focused on reusable Harness architecture. Product-specific
implementations and customer examples belong in separate downstream projects.

Do not commit runtime databases, evidence files, telemetry output, secrets, or
customer data. Tests must use synthetic values only.
