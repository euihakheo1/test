"""Typer root CLI. Sub-apps from other packages are registered if their module exists.

Convention for modules registered here (see ``_SUBAPPS`` / ``_COMMANDS``):
- sub-app modules expose ``app: typer.Typer``;
- server modules expose ``app: typer.Typer`` *or* a callable (``serve`` / ``run``) which is
  wrapped as ``jettae <group> <command>``.
A module that is not installed yet is skipped; ``jettae modules`` shows what was loaded.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
from datetime import date
from enum import StrEnum
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from jettae.domain.hashing import to_canonical
from jettae.domain.money import Money, RoundingMode
from jettae.domain.status import TradeType
from jettae.rules.kr_retail import DueResult, builtin_registry, compute_due


def force_utf8_stdio() -> None:
    """Make CLI output safe on Windows pipes/terminals whose locale codec is cp949.

    Without this, printing Korean text or characters such as '—' through a pipe (Git Bash,
    redirects, CI logs) raises UnicodeEncodeError. Called only when the CLI starts; importing
    :mod:`jettae` (or this module) never touches the process's stdio."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


class _CliApp(typer.Typer):
    """The root app; calling it (the ``jettae`` console script, :func:`main`) is the CLI
    entry, so stdio is configured there and only there."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        force_utf8_stdio()
        return super().__call__(*args, **kwargs)


app = _CliApp(
    name="jettae",
    help="제때받기: 정산 문서·입금 대사, 지급기한·지연이자 계산, 필요 서류 안내",
    no_args_is_help=True,
    add_completion=False,
)
rules_app = typer.Typer(help="규칙 레지스트리와 지급기한 계산", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
sources_app = typer.Typer(help="실제 공개 자료 수집·변환", no_args_is_help=True)


@app.callback()
def _startup() -> None:
    """Runs before every subcommand (not for ``--help``): load ``.env`` and refuse an unknown
    ``JETTAE_ENV``. Commands that serve or write shared state (api, worker, mcp, db, sources,
    demo) additionally run :func:`jettae.config.require_valid_environment` for their
    component; this check covers the remaining commands (rules, ingest, eval, agent, ocr),
    so that a typo such as ``JETTAE_ENV=production`` never runs anything on dev defaults."""
    from jettae.config import ENVIRONMENTS, EnvFileError, load_env

    try:
        load_env()
    except (EnvFileError, OSError) as exc:
        raise SystemExit(f"[jettae] configuration error: {exc}") from None
    raw = os.environ.get("JETTAE_ENV")
    if raw is not None and raw not in ENVIRONMENTS:
        raise SystemExit(
            f"[jettae] configuration error: JETTAE_ENV must be one of "
            f"{', '.join(ENVIRONMENTS)} (got {raw!r})"
        )


@sources_app.callback()
def _sources_startup(ctx: typer.Context) -> None:
    """Source downloads write manifests and use the 법제처 DRF key: checked per source
    (``sources:ftc`` needs an own ``JETTAE_DRF_OC`` in prod, ``sources:bpi2019`` does not)."""
    from jettae.config import require_valid_environment

    require_valid_environment(f"sources:{ctx.invoked_subcommand or ''}".rstrip(":"))


console = Console()

# (module, group path, attribute) — group path "sources/ftc" nests under "sources"
_SUBAPPS: list[tuple[str, str]] = [
    ("jettae.ingest.cli", "ingest"),
    ("jettae.sources.ftc.cli", "sources/ftc"),
    ("jettae.sources.bpi2019.cli", "sources/bpi2019"),
    ("jettae.sources.law.cli", "sources/law"),
    ("jettae.evals.cli", "eval"),
    ("jettae.agents.cli", "agent"),
    ("jettae.ocr.cli", "ocr"),
    ("jettae.demo", "demo"),
]
# (module, group, callable name used as command)
_COMMANDS: list[tuple[str, str, str]] = [
    ("jettae.mcp_server", "mcp", "serve"),
    ("jettae.api.cli", "api", "serve"),
    ("jettae.worker", "worker", "run"),
]
MODULE_STATUS: dict[str, str] = {}


def _import(module: str) -> Any | None:
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        # skip only when the module itself (or a parent package) is missing
        if e.name and (module == e.name or module.startswith(e.name + ".")):
            MODULE_STATUS[module] = "not installed (skipped)"
            return None
        MODULE_STATUS[module] = f"import error: {e!r}"
        return None
    except ImportError as e:
        MODULE_STATUS[module] = f"import error: {e!r}"
        return None
    MODULE_STATUS[module] = "loaded"
    return mod


def _register() -> None:
    sources_used = False
    for module, path in _SUBAPPS:
        mod = _import(module)
        sub = getattr(mod, "app", None) if mod is not None else None
        if mod is not None and not isinstance(sub, typer.Typer):
            MODULE_STATUS[module] = "loaded, but no `app: typer.Typer` (skipped)"
            continue
        if sub is None:
            continue
        if path.startswith("sources/"):
            sources_app.add_typer(sub, name=path.split("/", 1)[1])
            sources_used = True
        else:
            app.add_typer(sub, name=path)
    if sources_used:
        app.add_typer(sources_app, name="sources")
    for module, group, cmd in _COMMANDS:
        mod = _import(module)
        if mod is None:
            continue
        sub = getattr(mod, "app", None)
        if isinstance(sub, typer.Typer):
            app.add_typer(sub, name=group)
            continue
        fn = getattr(mod, cmd, None)
        if callable(fn):
            grp = typer.Typer(help=f"{group} ({module})", no_args_is_help=True)
            grp.command(cmd)(fn)
            app.add_typer(grp, name=group)
        else:
            MODULE_STATUS[module] = f"loaded, but no `app` or `{cmd}` (skipped)"


@app.command("modules")
def modules_cmd() -> None:
    """Show which optional CLI modules were registered."""
    t = Table("module", "status")
    for m, s in MODULE_STATUS.items():
        t.add_row(m, s)
    console.print(t)


class _Type(StrEnum):
    direct = "direct"
    consignment = "consignment"
    subcontract = "subcontract"


class _Rounding(StrEnum):
    floor = "floor"
    half_up = "half_up"


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


@rules_app.command("due")
def rules_due(
    type_: Annotated[_Type, typer.Option("--type", help="direct | consignment | subcontract")],
    base: Annotated[
        str | None, typer.Option("--base", help="기준일 YYYY-MM-DD (상품수령일/판매마감일)")
    ] = None,
    paid: Annotated[str | None, typer.Option("--paid", help="지급일 YYYY-MM-DD")] = None,
    amount: Annotated[int, typer.Option("--amount", help="원금(원, 정수)")] = 0,
    rollover: Annotated[
        bool | None,
        typer.Option(
            "--rollover/--no-rollover", help="말일이 휴일이면 다음 영업일(미지정=두 계산)"
        ),
    ] = None,
    rounding: Annotated[_Rounding, typer.Option("--rounding")] = _Rounding.floor,
    as_of: Annotated[str | None, typer.Option("--as-of", help="미지급 시 기준일")] = None,
    tax_invoice_date: Annotated[
        str | None, typer.Option("--tax-invoice-date", help="세금계산서 작성일(기산일로 쓰지 않음)")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="JSON 출력")] = False,
) -> None:
    """법정 지급기한·지연일수·지연이자 계산."""
    res = compute_due(
        TradeType(type_.value),
        _parse_date(base),
        Money(amount),
        paid_date=_parse_date(paid),
        as_of=_parse_date(as_of),
        rollover=rollover,
        rounding=RoundingMode(rounding.value),
        tax_invoice_date=_parse_date(tax_invoice_date),
    )
    if as_json:
        typer.echo(json.dumps(to_canonical(res), ensure_ascii=False, indent=2))
        return
    if not isinstance(res, DueResult):
        console.print("[bold yellow]계산 보류: 근거 부족[/]")
        console.print("미확인 항목: " + ", ".join(res.missing))
        for d in res.required_documents:
            console.print(f"  필요 서류: {d}")
        for n in res.notes:
            console.print(f"  참고: {n}")
        raise typer.Exit(code=2)
    reg = builtin_registry()
    console.print(f"규칙: {res.rule_version}  (기준일 {res.base_date}, {res.term_days}일)")
    t = Table("계산", "rollover", "지급기한", "지연일수", "지연이자")
    for v in res.variants:
        t.add_row(
            v.label,
            "적용" if v.rollover else "미적용",
            v.due_date.isoformat(),
            str(v.delay_days),
            str(v.interest) if v.interest is not None else "미계산",
        )
    console.print(t)
    if res.unresolved:
        console.print("확인이 필요한 조건: " + ", ".join(res.unresolved))
    for a in res.assumptions:
        console.print(f"  가정: {a}")
    for key in filter(None, [res.rule_version, res.interest_rule_version]):
        rid, ver = key.split("@", 1)
        rv = reg.get(rid, ver)
        console.print(f"  출처: {rv.title} — {rv.source_url}")


@rules_app.command("list")
def rules_list() -> None:
    """등록된 규칙 버전(비활성 포함)."""
    t = Table("rule", "version", "effective_from", "known_from", "verified", "title")
    for rv in builtin_registry():
        t.add_row(
            rv.rule_id,
            rv.version,
            rv.effective_from.isoformat() if rv.effective_from else "(비활성)",
            rv.known_from.isoformat() if rv.known_from else "-",
            "yes" if rv.verified else "no",
            rv.title,
        )
    console.print(t)


_register()


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
