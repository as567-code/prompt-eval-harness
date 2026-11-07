"""``promptharness models`` sub-app: live verification of every catalog model."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer

from promptharness.catalog import Catalog, ModelEntry, UnknownModelError
from promptharness.cost import notional_cost_usd
from promptharness.providers.base import CompletionRequest, Provider, ProviderError
from promptharness.providers.control import (
    BudgetExceeded,
    RunBudget,
    call_with_retry,
    quota_pressure,
)
from promptharness.providers.registry import provider_for

models_app = typer.Typer(help="Model catalog tools.", no_args_is_help=True)


@dataclass(frozen=True)
class VerifyRow:
    model: str
    served: str
    input_side: int
    output: int
    notional_usd: float
    cli_version: str
    quota_pressure: float | None
    status: Literal["OK", "FAIL"]
    detail: str


def _fail(model: str, detail: str) -> VerifyRow:
    return VerifyRow(model, "-", 0, 0, 0.0, "-", None, "FAIL", detail)


def verify_models(
    entries: list[ModelEntry],
    factory: Callable[[ModelEntry], Provider],
    *,
    budget: RunBudget | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[VerifyRow]:
    """Send one tiny request per entry; a failure is a FAIL row, never an abort."""
    if budget is None:
        budget = RunBudget(max_calls=len(entries) * 2, max_notional_usd=1.0)
    rows: list[VerifyRow] = []
    for idx, entry in enumerate(entries):
        req = CompletionRequest(
            model=entry.id,
            effort=entry.default_effort,
            system="You are a terse assistant.",
            prompt="Reply with exactly: ok",
            timeout_s=180,
        )
        try:
            completion = call_with_retry(factory(entry), req, budget=budget, sleep=sleep)
            cost = notional_cost_usd(completion.usage_gross, entry)
            budget.charge(calls=0, usd=cost)
        except ProviderError as err:
            rows.append(_fail(entry.id, f"{err.error_class}: {err}"))
            continue
        except BudgetExceeded as err:
            detail = f"budget_exceeded: {err}"
            rows.extend(_fail(e.id, detail) for e in entries[idx:])
            return rows
        ok = completion.served_model == entry.id
        rows.append(
            VerifyRow(
                model=entry.id,
                served=completion.served_model,
                input_side=completion.usage_gross.input_side,
                output=completion.usage_gross.output,
                notional_usd=cost,
                cli_version=completion.cli_version,
                quota_pressure=quota_pressure(completion.quota),
                status="OK" if ok else "FAIL",
                detail="" if ok else f"served {completion.served_model!r}, wanted {entry.id!r}",
            )
        )
    return rows


def _table(rows: list[VerifyRow]) -> list[str]:
    header = ("model", "served", "in/out", "notional_usd", "cli_version", "quota", "status")
    body = [
        (
            r.model,
            r.served,
            f"{r.input_side}/{r.output}",
            f"{r.notional_usd:.6f}",
            r.cli_version,
            "-" if r.quota_pressure is None else f"{r.quota_pressure:.2f}",
            r.status,
        )
        for r in rows
    ]
    widths = [max(len(str(line[i])) for line in [header, *body]) for i in range(len(header))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(line, widths, strict=True)) for line in [header, *body]
    ]
    for r in rows:
        if r.status == "FAIL":
            lines.append(f"  {r.model}: {r.detail}")
    return lines


@models_app.command("verify")
def verify(
    catalog: Annotated[Path, typer.Option("--catalog")] = Path("config/models.yaml"),
    model: Annotated[list[str] | None, typer.Option("--model", help="Limit to model ids.")] = None,
    work_root: Annotated[Path, typer.Option("--work-root")] = Path("runs/_adhoc/cwd"),
) -> None:
    """Make one tiny live call per model and report served model, usage and quota."""
    if os.environ.get("PEH_LIVE") != "1":
        typer.echo("refusing: set PEH_LIVE=1 to make live CLI calls", err=True)
        raise typer.Exit(2)
    cat = Catalog.load(catalog)
    try:
        entries = [cat.get(m) for m in model] if model else list(cat.entries)
    except UnknownModelError as err:
        typer.echo(f"unknown model: {err}", err=True)
        raise typer.Exit(2) from err
    work_root.mkdir(parents=True, exist_ok=True)
    root = work_root.resolve()
    rows = verify_models(entries, lambda e: provider_for(e, work_root=root))
    for line in _table(rows):
        typer.echo(line)
    if any(r.status == "FAIL" for r in rows):
        raise typer.Exit(1)
