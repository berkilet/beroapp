"""Phase-gate evaluation.

Evaluates the criteria in docs/PHASE_GATES.md against stored observations and
prints a pass/fail table. It writes an audit row on every run.

Critically: **evaluating a gate does not advance a phase.** Advancing requires
``--confirm`` and, for phase 3, every acknowledgement flag. Even then, this tool
records the decision in the database; the operating phase itself is set in the
environment and requires a restart, so a phase change always leaves two traces.

    python scripts/phase_gate.py --target 2
    python scripts/phase_gate.py --target 2 --confirm
    python scripts/phase_gate.py --target 3 --ack phase1-complete --ack ...
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.enums import ResolutionOutcome, SystemComponent
from app.db.models import (
    AuditLog,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PerformanceMetric,
    Prediction,
    Resolution,
    Signal,
    SystemConfig,
    SystemEvent,
)
from app.db.session import session_scope
from app.engines.authorization import PHASE3_GATE_KEY, REQUIRED_ACKS


@dataclass
class Criterion:
    number: int
    name: str
    observed: object
    required: object
    passed: bool
    note: str = ""


def _fmt(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


# ---------------------------------------------------------------------------
def evaluate_gate_1(session) -> list[Criterion]:
    s = get_settings()
    now = datetime.now(UTC)
    criteria: list[Criterion] = []

    markets_with_data = session.execute(
        select(func.count(func.distinct(MarketSnapshot.market_id)))
    ).scalar_one()
    criteria.append(
        Criterion(1, "markets observed", markets_with_data, s.gate1_min_markets,
                  markets_with_data >= s.gate1_min_markets)
    )

    predictions = session.execute(select(func.count()).select_from(Prediction)).scalar_one()
    criteria.append(
        Criterion(2, "predictions stored", predictions, s.gate1_min_predictions,
                  predictions >= s.gate1_min_predictions)
    )

    # Only predictions made at least 24h before resolution count. A prediction
    # made during the settlement window proves nothing.
    resolved = session.execute(
        select(func.count(func.distinct(Prediction.market_id)))
        .select_from(Prediction)
        .join(Resolution, Resolution.market_id == Prediction.market_id)
        .where(
            Resolution.is_ambiguous.is_(False),
            Resolution.outcome.in_([ResolutionOutcome.YES.value, ResolutionOutcome.NO.value]),
            Prediction.predicted_at <= Resolution.known_at - timedelta(hours=24),
        )
    ).scalar_one()
    criteria.append(
        Criterion(3, "resolved markets predicted >=24h ahead", resolved, s.gate1_min_resolved,
                  resolved >= s.gate1_min_resolved)
    )

    first_event = session.execute(select(func.min(SystemEvent.occurred_at))).scalar_one_or_none()
    uptime_days = (now - first_event).days if first_event else 0
    criteria.append(
        Criterion(4, "days of operation", uptime_days, s.gate1_min_uptime_days,
                  uptime_days >= s.gate1_min_uptime_days)
    )

    # Snapshot cycle coverage: how many cycles ran versus how many should have.
    cycles = session.execute(
        select(func.count()).select_from(SystemEvent).where(
            SystemEvent.component == SystemComponent.DATA_FEED.value,
            SystemEvent.event == "snapshot_cycle",
        )
    ).scalar_one()
    expected_cycles = max(1, int((uptime_days * 86_400) / s.snapshot_interval_s)) if uptime_days else 1
    gap_ratio = max(0.0, 1.0 - cycles / expected_cycles) if expected_cycles else 1.0
    criteria.append(
        Criterion(5, "snapshot gap ratio", gap_ratio, f"<= {s.gate1_max_gap_ratio}",
                  gap_ratio <= s.gate1_max_gap_ratio,
                  f"{cycles} cycles observed, ~{expected_cycles} expected")
    )

    parse_errors = session.execute(
        select(func.count()).select_from(SystemEvent).where(SystemEvent.severity == "ERROR")
    ).scalar_one()
    total_events = session.execute(select(func.count()).select_from(SystemEvent)).scalar_one()
    error_rate = parse_errors / total_events if total_events else 0.0
    criteria.append(
        Criterion(6, "error event rate", error_rate, f"<= {s.gate1_max_parse_error_rate}",
                  error_rate <= s.gate1_max_parse_error_rate)
    )

    calibration = session.execute(
        select(PerformanceMetric)
        .where(PerformanceMetric.kind == "calibration", PerformanceMetric.scope == "overall")
        .order_by(PerformanceMetric.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    has_calibration = (
        calibration is not None
        and calibration.sample_size >= s.gate1_min_resolved
        and not calibration.metrics.get("model", {}).get("insufficient_data", True)
    )
    criteria.append(
        Criterion(7, "calibration analysis completed",
                  calibration.sample_size if calibration else 0,
                  f">= {s.gate1_min_resolved} resolved", has_calibration)
    )

    # The criterion most systems skip: beating the market, not merely being
    # calibrated. The market is calibrated too.
    skill = (calibration.metrics.get("skill_vs_market", {}) if calibration else {})
    beats = skill.get("beats_baseline")
    criteria.append(
        Criterion(8, "model Brier beats market-implied Brier",
                  f"model {_fmt(skill.get('model_brier'))} vs market {_fmt(skill.get('baseline_brier'))}",
                  "model < market", bool(beats),
                  "a well-calibrated model that does not beat the market has found no edge")
    )

    criteria.append(
        Criterion(9, "security test suite", "run scripts/security_scan.sh", "green",
                  False, "must be confirmed manually with --ack security-review-complete")
    )

    from app.db.models import ModelVersion

    documented = session.execute(
        select(func.count()).select_from(ModelVersion).where(
            ModelVersion.performance_summary.isnot(None)
        )
    ).scalar_one()
    criteria.append(
        Criterion(10, "documented model performance", documented, ">= 1", documented >= 1)
    )

    return criteria


def evaluate_gate_2(session) -> list[Criterion]:
    s = get_settings()
    now = datetime.now(UTC)
    criteria: list[Criterion] = []

    gate1 = session.execute(
        select(SystemConfig).where(SystemConfig.key == "phase_gate_2_passed")
    ).scalar_one_or_none()
    criteria.append(
        Criterion(1, "phase 1 gate recorded as passed",
                  bool(gate1 and gate1.value.get("passed")), True,
                  bool(gate1 and gate1.value.get("passed")))
    )

    trades = session.execute(select(func.count()).select_from(PaperOrder)).scalar_one()
    criteria.append(
        Criterion(2, "paper trades executed", trades, s.gate2_min_paper_trades,
                  trades >= s.gate2_min_paper_trades)
    )

    settled = session.execute(
        select(func.count())
        .select_from(PaperFill)
        .join(Resolution, Resolution.market_id == PaperFill.market_id)
        .where(Resolution.is_ambiguous.is_(False))
    ).scalar_one()
    criteria.append(
        Criterion(3, "paper trades reaching resolution", settled, s.gate2_min_settled_trades,
                  settled >= s.gate2_min_settled_trades)
    )

    first_order = session.execute(select(func.min(PaperOrder.submitted_at))).scalar_one_or_none()
    days = (now - first_order).days if first_order else 0
    criteria.append(
        Criterion(4, "days of paper trading", days, s.gate2_min_days, days >= s.gate2_min_days)
    )

    calibration = session.execute(
        select(PerformanceMetric)
        .where(PerformanceMetric.kind == "calibration", PerformanceMetric.scope == "overall")
        .order_by(PerformanceMetric.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    model = calibration.metrics.get("model", {}) if calibration else {}

    brier = model.get("brier_score")
    criteria.append(
        Criterion(5, "out-of-sample Brier score", brier, f"<= {s.gate2_max_brier}",
                  brier is not None and brier <= s.gate2_max_brier)
    )

    ece = model.get("expected_calibration_error")
    criteria.append(
        Criterion(6, "expected calibration error", ece, f"<= {s.gate2_max_ece}",
                  ece is not None and ece <= s.gate2_max_ece)
    )

    # Expectancy per settled trade, after modelled slippage and fees.
    fills = list(session.execute(select(PaperFill)).scalars())
    expectancy = None
    if fills:
        net = sum(-f.slippage * f.filled_shares - f.fees for f in fills)
        expectancy = net / len(fills)
    criteria.append(
        Criterion(7, "expectancy per trade (net of costs)", expectancy,
                  f"> {s.gate2_min_expectancy}",
                  expectancy is not None and expectancy > s.gate2_min_expectancy,
                  "computed from realised paper fills, not from signal prices")
    )

    from app.db.models import PortfolioSnapshot

    max_dd = session.execute(select(func.max(PortfolioSnapshot.drawdown_pct))).scalar_one_or_none()
    criteria.append(
        Criterion(8, "maximum drawdown", max_dd, f"<= {s.gate2_max_drawdown * 100}%",
                  max_dd is not None and max_dd <= s.gate2_max_drawdown * 100)
    )

    slippage_error = None
    if fills:
        slippage_error = sum(abs(f.slippage) for f in fills) / len(fills)
    criteria.append(
        Criterion(9, "mean realised slippage", slippage_error,
                  f"<= {s.gate2_max_slippage_error}",
                  slippage_error is not None and slippage_error <= s.gate2_max_slippage_error)
    )

    latency = session.execute(select(func.avg(PaperOrder.execution_latency_ms))).scalar_one_or_none()
    criteria.append(
        Criterion(10, "mean signal-to-execution latency", latency, f"<= {s.gate2_max_latency_ms}ms",
                  latency is not None and latency <= s.gate2_max_latency_ms)
    )

    # The criterion most backtests omit: was the edge still there afterwards?
    checked = session.execute(
        select(func.count()).select_from(Signal).where(Signal.persistence_checked_at.isnot(None))
    ).scalar_one()
    persisted = session.execute(
        select(func.count()).select_from(Signal).where(Signal.edge_persisted.is_(True))
    ).scalar_one()
    ratio = persisted / checked if checked else None
    criteria.append(
        Criterion(11, "edge persistence after model latency", ratio,
                  f">= {s.gate2_min_persistence}",
                  ratio is not None and ratio >= s.gate2_min_persistence,
                  f"{persisted}/{checked} signals re-checked; an edge that has evaporated is not an edge")
    )

    return criteria


# ---------------------------------------------------------------------------
def render(target: int, criteria: list[Criterion]) -> bool:
    all_passed = all(c.passed for c in criteria)

    print(f"\nPhase gate evaluation: advance to PHASE_{target}")
    print("=" * 88)
    print(f"{'#':>3}  {'CRITERION':<44} {'OBSERVED':>14} {'REQUIRED':>14}  ")
    print("-" * 88)
    for c in criteria:
        mark = "PASS" if c.passed else "FAIL"
        print(f"{c.number:>3}  {c.name:<44} {_fmt(c.observed):>14} {_fmt(c.required):>14}  {mark}")
        if c.note:
            print(f"     └─ {c.note}")
    print("=" * 88)
    print(f"RESULT: {'ALL CRITERIA PASS' if all_passed else 'GATE NOT PASSED'}")
    if not all_passed:
        failed = [c.name for c in criteria if not c.passed]
        print(f"Blocking: {', '.join(failed)}")
    print(
        "\nPassing a gate means the recorded observations met thresholds chosen in\n"
        "advance. It does not mean the model is good, or that results will persist.\n"
    )
    return all_passed


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a phase gate.")
    parser.add_argument("--target", type=int, choices=[2, 3], required=True)
    parser.add_argument("--confirm", action="store_true",
                        help="record the gate as passed (does not itself change the running phase)")
    parser.add_argument("--ack", action="append", default=[],
                        help="operator acknowledgement; repeat for each item")
    parser.add_argument("--operator", default="operator")
    args = parser.parse_args()

    with session_scope() as session:
        criteria = evaluate_gate_1(session) if args.target == 2 else evaluate_gate_2(session)
        passed = render(args.target, criteria)

        if args.target == 3:
            missing = [a for a in REQUIRED_ACKS if a not in args.ack]
            if missing:
                print("Missing operator acknowledgements:")
                for item in missing:
                    print(f"  --ack {item}")
                passed = False

        session.add(
            AuditLog(
                actor=args.operator,
                action="phase_gate_evaluation",
                component=SystemComponent.RISK_ENGINE.value,
                output={
                    "target_phase": args.target,
                    "passed": passed,
                    "criteria": [
                        {"number": c.number, "name": c.name, "observed": _fmt(c.observed),
                         "required": _fmt(c.required), "passed": c.passed}
                        for c in criteria
                    ],
                    "acknowledgements": args.ack,
                    "confirmed": args.confirm,
                },
                occurred_at=datetime.now(UTC),
            )
        )

        if args.confirm and passed:
            key = f"phase_gate_{args.target}_passed" if args.target == 2 else PHASE3_GATE_KEY
            row = session.execute(
                select(SystemConfig).where(SystemConfig.key == key)
            ).scalar_one_or_none()
            payload = {
                "passed": True,
                "recorded_at": datetime.now(UTC).isoformat(),
                "acknowledgements": args.ack,
            }
            if row is None:
                session.add(SystemConfig(key=key, value=payload, updated_by=args.operator))
            else:
                row.value = payload
                row.updated_by = args.operator
            print(f"Recorded {key}=passed.")
            print(
                f"The running phase is NOT changed by this tool. Set CURRENT_PHASE=PHASE_{args.target} "
                "in the environment and restart the services."
            )
        elif args.confirm:
            print("Refusing to record a gate that did not pass.")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
