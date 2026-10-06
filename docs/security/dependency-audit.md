# Dependency audit

Each run appends a dated section. A run reports only what the vulnerability databases knew on
that day; "no known vulnerabilities" is not a statement that a package is free of defects.

## 2026-10-06

Environment: Windows 11, Git Bash, uv 0.12.23, pip-audit 2.10.1 (run through `uvx`, not a
project dependency), Node.js 24.16.0, npm 11.13.0. Inputs: `uv.lock` and
`frontend/package-lock.json` as they are in the working tree on that date.

### Python (`uv.lock`, all extras)

Commands (repository root). The `uv export ... | uvx pip-audit -r /dev/stdin` form does not work on
Windows (native executables cannot open `/dev/stdin`), so the export goes through a temporary file
outside the repository:

```bash
uv export --frozen --all-extras --no-emit-project --format requirements-txt -o "$TMPDIR/req-all.txt"
uvx pip-audit -r "$TMPDIR/req-all.txt" --no-deps --disable-pip                 # PyPI advisory DB
uvx pip-audit -r "$TMPDIR/req-all.txt" --no-deps --disable-pip -s osv          # OSV
```

On Linux/macOS the piped form is equivalent:
`uv export --frozen --all-extras --no-emit-project --format requirements-txt | uvx pip-audit -r /dev/stdin --no-deps --disable-pip`.

Result: 105 pinned packages audited, **no known vulnerabilities** in either database.

| Package | Advisory | Severity | Fixed version | Decision |
|---|---|---|---|---|
| (none) | | | | |

### npm (`frontend/package-lock.json`)

Commands (in `frontend/`, after `npm ci`):

```bash
npm audit --omit=dev     # production dependencies only (next, react, react-dom and their deps)
npm audit                # everything, including dev tooling
```

Results:

- `npm audit --omit=dev`: **0 vulnerabilities** (exit code 0).
- `npm audit`: **5 high** (exit code 1), all one advisory reached through the lint toolchain:
  `eslint-config-next@16.3.8` → `@next/eslint-plugin-next@16.3.8` → `fast-glob@3.3.1` →
  `micromatch@4.0.8` → `braces@3.0.3`.

| Package (installed) | Advisory | Severity | Fixed version | Decision |
|---|---|---|---|---|
| braces 3.0.3 | GHSA-vfj7-8cjw-p6xm (stack-exhaustion DoS through deeply nested brace patterns; CVSS 7.5; affected `<=3.0.3`) | high | none published (3.0.3 is the latest release on npm on 2026-10-06) | **Deferred**, see below |
| micromatch 4.0.8 | via braces | high | none | Deferred (same cause) |
| fast-glob 3.3.1 | via micromatch | high | none | Deferred (same cause) |
| @next/eslint-plugin-next 16.3.8 | via fast-glob | high | none within 16.x | Deferred (same cause) |
| eslint-config-next 16.3.8 | via @next/eslint-plugin-next | high | npm suggests `eslint-config-next@14.2.35` (`npm audit fix --force`) | **Not applied**: a major downgrade that does not match Next 16 |

Why deferred:

- No patched `braces` exists, so neither an upgrade within the compatible range nor an npm
  `overrides` entry can remove the advisory. The only offered "fix" downgrades the lint config to
  the Next 14 line, which would break `npm run lint` for this Next 16 app.
- The chain is dev-only: it runs inside ESLint on the developer machine and in CI, over glob
  patterns written in this repository's lint configuration. It is not part of the production
  bundle (`npm audit --omit=dev` is clean) and does not process user input.
- Revisit when `braces` publishes a fix or `@next/eslint-plugin-next` drops `fast-glob@3.3.1`;
  re-run both commands above and update this table.

### What was changed because of this audit

Nothing: there was no fix that could be applied without a breaking change. Lock files were not
modified by the audit.

### Not covered

- Container base images (`deploy/Dockerfile` uses `python:3.12-slim` and the `uv` image; they were
  not built or scanned, Docker is not available on the development machine).
- GitHub Actions used by `.github/workflows/ci.yml` (pinned to major versions, not to commit SHAs).
- Licence compliance of dependencies (see `docs/PUBLIC_USE.md`).

## 2026-10-07 (re-run for the review-fix commit)

Same machine and tools (uv 0.12.23, pip-audit 2.10.1 through `uv tool run`, Node.js 24.16.0,
npm 11.13.0). `uv.lock` and `frontend/package-lock.json` were not changed by the review fixes.

| Check | Command | Result |
|---|---|---|
| Python, PyPI advisory DB | `uv export --frozen --all-extras --no-emit-project --format requirements-txt -o "$TMPDIR/req-all.txt"` then `uvx pip-audit -r "$TMPDIR/req-all.txt" --no-deps --disable-pip` | 107 pinned packages, "No known vulnerabilities found", exit 0 |
| Python, OSV | same with `-s osv` | "No known vulnerabilities found", exit 0 |
| npm, production deps | `npm audit --omit=dev` (in `frontend/`) | "found 0 vulnerabilities", exit 0 |
| npm, all deps | `npm audit` | 5 high, exit 1: the same `braces` advisory GHSA-vfj7-8cjw-p6xm through `eslint-config-next@16.3.8` (dev-only lint chain); npm still offers only `eslint-config-next@14.2.35` (breaking downgrade). Decision unchanged: **deferred** (see the 2026-10-06 section) |

The earlier section reports 105 packages; this export has 107 `name==version` lines
(`grep -cE '^[A-Za-z0-9_.-]+==' req-all.txt`). `uv.lock` is unchanged since the first commit; the
cause of the difference (counting method or the earlier working tree) was not investigated.
Nothing was changed because of this audit.

