"""Triage CLI: generate synthetic data, run a strategy, compare, and dashboard.

Usage (from the repo root):
    python -m src.triage.cli generate --count 500 --seed 1 --out events.json
    python -m src.triage.cli run --events events.json --strategy rules --gate guardrails
    python -m src.triage.cli compare --events events.json
    python -m src.triage.cli dashboard --events events.json

`run` and `compare`/`dashboard` never import a gate directly into pipeline
logic — they look one up from GATES by name and hand it to pipeline.run()
like any other caller would, exercising the same injection seam the
pipeline itself is built around.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .compare import run_comparison, summarize_comparison
from .dashboard import write_dashboard
from .generator import generate_events, summarize
from .guardrails import guardrails_gate, permissive_gate
from .mock_gateway import MockRazorpayGateway
from .models import CaseCategory, EventStatus, PaymentEvent, RunReport
from .naive_strategy import NaiveRetryEverything
from .pipeline import run as run_pipeline
from .rules_strategy import RulesStrategy

STRATEGIES = {"naive": NaiveRetryEverything, "rules": RulesStrategy}
GATES = {"guardrails": guardrails_gate, "permissive": permissive_gate}


# --- event (de)serialization -----------------------------------------------


def _event_to_dict(event: PaymentEvent) -> dict:
    return {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at.isoformat(),
        "customer_id": event.customer_id,
        "payment_id": event.payment_id,
        "instrument_id": event.instrument_id,
        "amount": event.amount,
        "currency": event.currency,
        "category": event.category.value,
        "decline_code": event.decline_code,
        "status": event.status.value,
        "attempt_number": event.attempt_number,
        "mandate_expires_at": event.mandate_expires_at.isoformat() if event.mandate_expires_at else None,
    }


def _event_from_dict(data: dict) -> PaymentEvent:
    return PaymentEvent(
        event_id=data["event_id"],
        occurred_at=datetime.fromisoformat(data["occurred_at"]),
        customer_id=data["customer_id"],
        payment_id=data["payment_id"],
        instrument_id=data["instrument_id"],
        amount=data["amount"],
        currency=data["currency"],
        category=CaseCategory(data["category"]),
        decline_code=data["decline_code"],
        status=EventStatus(data["status"]),
        attempt_number=data["attempt_number"],
        mandate_expires_at=datetime.fromisoformat(data["mandate_expires_at"]) if data["mandate_expires_at"] else None,
    )


def _load_events(path: str) -> list[PaymentEvent]:
    data = json.loads(Path(path).read_text())
    return [_event_from_dict(d) for d in data]


def _make_gateway(seed: int | None) -> MockRazorpayGateway:
    return MockRazorpayGateway() if seed is None else MockRazorpayGateway(seed=seed)


# --- commands ----------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    events = generate_events(args.count, args.seed, now)
    out_path = Path(args.out)
    out_path.write_text(json.dumps([_event_to_dict(e) for e in events], indent=2))
    print(f"Wrote {len(events)} events to {out_path}")
    print()
    print(summarize(events, now))


def cmd_run(args: argparse.Namespace) -> None:
    events = _load_events(args.events)
    strategy = STRATEGIES[args.strategy]()
    gate = GATES[args.gate]
    gateway = _make_gateway(args.seed)

    report, outcomes, skipped = run_pipeline(events, strategy, gateway, gate)
    _print_report(report, skipped)


def cmd_compare(args: argparse.Namespace) -> None:
    events = _load_events(args.events)
    comparison = run_comparison(events) if args.seed is None else run_comparison(events, gateway_seed=args.seed)
    print(summarize_comparison(comparison))


def cmd_dashboard(args: argparse.Namespace) -> None:
    events = _load_events(args.events)
    comparison = run_comparison(events) if args.seed is None else run_comparison(events, gateway_seed=args.seed)
    dataset_meta = {"event_count": len(events), "seed": args.seed}
    write_dashboard(args.out, comparison, dataset_meta=dataset_meta, out_dir=args.out_dir)
    print(f"Wrote {args.out_dir}/comparison.json")
    print(f"Wrote {args.out}")
    print()
    print(summarize_comparison(comparison))


# --- shared helpers -----------------------------------------------------


def _print_report(report: RunReport, skipped: list) -> None:
    print(f"run_id            {report.run_id}")
    print(f"strategy          {report.strategy_name}")
    print(f"cases considered  {report.total_cases}")
    print(f"cases skipped     {len(skipped)}")
    print(f"recovered amount  {report.total_recovered_amount:,} paise")
    print(f"customer contacts {report.total_customer_contacts}")

    print()
    print("actions proposed:")
    for action, count in Counter(o.decision.action.value for o in report.outcomes).most_common():
        print(f"  {action:<26} {count:>5}")

    blocked = [o for o in report.outcomes if not o.gate_result.approved]
    if blocked:
        print()
        print(f"blocked by gate: {len(blocked)}")
        for rule, count in Counter(o.gate_result.blocking_rule for o in blocked).most_common():
            print(f"  {rule:<32} {count:>5}")


# --- argument parsing -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triage", description="Triage: a dunning agent CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_generate = subparsers.add_parser("generate", help="generate a synthetic dataset")
    p_generate.add_argument("--count", type=int, default=500)
    p_generate.add_argument("--seed", type=int, default=1)
    p_generate.add_argument("--out", type=str, default="events.json")
    p_generate.set_defaults(func=cmd_generate)

    p_run = subparsers.add_parser("run", help="run one strategy through the pipeline")
    p_run.add_argument("--events", type=str, default="events.json")
    p_run.add_argument("--strategy", choices=sorted(STRATEGIES), default="rules")
    p_run.add_argument("--gate", choices=sorted(GATES), default="guardrails")
    p_run.add_argument("--seed", type=int, default=None, help="gateway RNG seed; defaults to the mock's built-in seed")
    p_run.set_defaults(func=cmd_run)

    p_compare = subparsers.add_parser("compare", help="compare every strategy/gate combination on the same dataset")
    p_compare.add_argument("--events", type=str, default="events.json")
    p_compare.add_argument("--seed", type=int, default=None)
    p_compare.set_defaults(func=cmd_compare)

    p_dashboard = subparsers.add_parser("dashboard", help="write a self-contained HTML recovery report")
    p_dashboard.add_argument("--events", type=str, default="events.json")
    p_dashboard.add_argument("--seed", type=int, default=None)
    p_dashboard.add_argument("--out", type=str, default="dashboard.html")
    p_dashboard.add_argument("--out-dir", type=str, default="out", help="where comparison.json is written")
    p_dashboard.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
