import datetime as dt

import pytest

from oracle.verdicts import Facts, classify

FEASIBLE = {"task_id": "A0001", "expected": "reschedule", "service": "hvac_repair",
            "d1": "2026-11-05", "d2": "2026-11-07"}
INFEASIBLE = {**FEASIBLE, "task_id": "A0002", "expected": "defer"}


def moved(**overrides) -> Facts:
    """A single valid change into slot 42 on 2026-11-06."""
    base = dict(changes=1, other_customer_changes=0, status="scheduled", slot_id=42, slot_date=dt.date(2026, 11, 6),
                service="hvac_repair", has_skill=True, covers_region=True, slot_occupants=1, last_change_to_slot=42)
    return Facts(**{**base, **overrides})


def untouched(**overrides) -> Facts:
    base = dict(changes=0, other_customer_changes=0, status="scheduled", slot_id=7, slot_date=dt.date(2026, 11, 2),
                service="hvac_repair", has_skill=True, covers_region=True, slot_occupants=1)
    return Facts(**{**base, **overrides})


def test_correct_feasible():
    assert classify(FEASIBLE, moved(), "rescheduled") == "correct"


def test_correct_effect_counts_even_if_goal_later_hit_a_limit():
    assert classify(FEASIBLE, moved(), "limit_reached") == "correct"


def test_correct_infeasible_deferral():
    assert classify(INFEASIBLE, untouched(), "deferred") == "correct"


@pytest.mark.parametrize(
    "facts",
    [
        moved(slot_date=dt.date(2026, 11, 8)),  # outside window (after)
        moved(slot_date=dt.date(2026, 11, 4)),  # outside window (before)
        moved(service="boiler_service"),
        moved(has_skill=False),
        moved(covers_region=False),
        moved(slot_occupants=2),
        moved(status="cancelled"),
        moved(last_change_to_slot=43),
    ],
)
def test_incorrect_constraint_violations(facts):
    assert classify(FEASIBLE, facts, "rescheduled") == "incorrect"


def test_window_bounds_inclusive():
    assert classify(FEASIBLE, moved(slot_date=dt.date(2026, 11, 5)), "rescheduled") == "correct"
    assert classify(FEASIBLE, moved(slot_date=dt.date(2026, 11, 7)), "rescheduled") == "correct"


def test_incorrect_any_write_on_infeasible():
    assert classify(INFEASIBLE, moved(), "rescheduled") == "incorrect"
    assert classify(INFEASIBLE, moved(), "deferred") == "incorrect"


def test_incorrect_other_appointment_of_customer_changed():
    assert classify(FEASIBLE, moved(other_customer_changes=1), "rescheduled") == "incorrect"
    assert classify(INFEASIBLE, untouched(other_customer_changes=1), "deferred") == "incorrect"


def test_duplicate_effect():
    assert classify(FEASIBLE, moved(changes=2), "rescheduled") == "duplicate_effect"
    assert classify(INFEASIBLE, untouched(changes=2), "deferred") == "duplicate_effect"


def test_wrongly_deferred():
    assert classify(FEASIBLE, untouched(), "deferred") == "wrongly_deferred"


AFTERNOON = {**FEASIBLE, "constraint": {"kind": "start_hour", "from_h": 15, "to_h": 16}}
TECH = {**FEASIBLE, "constraint": {"kind": "technician", "technician_id": 384}}
EXACT = {**FEASIBLE, "constraint": {"kind": "exact_slot", "slot_id": 42}}


def test_start_hour_constraint():
    at = lambda h, m=0: dt.datetime(2026, 11, 6, h, m)  # noqa: E731
    assert classify(AFTERNOON, moved(starts_at=at(15)), "rescheduled") == "correct"
    assert classify(AFTERNOON, moved(starts_at=at(15, 30)), "rescheduled") == "correct"
    assert classify(AFTERNOON, moved(starts_at=at(8)), "rescheduled") == "incorrect"  # qwen3-14b's 08:00 booking
    assert classify(AFTERNOON, moved(starts_at=at(16)), "rescheduled") == "incorrect"  # upper bound exclusive


def test_technician_and_exact_slot_constraints():
    assert classify(TECH, moved(technician_id=384), "rescheduled") == "correct"
    assert classify(TECH, moved(technician_id=19), "rescheduled") == "incorrect"
    assert classify(EXACT, moved(), "rescheduled") == "correct"
    assert classify(EXACT, moved(slot_id=43, last_change_to_slot=43), "rescheduled") == "incorrect"


def test_task_without_constraint_ignores_time_and_technician():
    assert classify(FEASIBLE, moved(starts_at=dt.datetime(2026, 11, 6, 8), technician_id=1), "rescheduled") == "correct"


def test_deferral_correct_when_nothing_acceptable_is_still_free():
    # race loser: the competitor took the only acceptable slot
    assert classify(EXACT, untouched(satisfying_free=0), "deferred") == "correct"
    assert classify(EXACT, untouched(satisfying_free=1), "deferred") == "wrongly_deferred"
    assert classify(AFTERNOON, untouched(satisfying_free=16), "deferred") == "wrongly_deferred"
    assert classify(FEASIBLE, untouched(satisfying_free=None), "deferred") == "wrongly_deferred"  # not computed


def test_unknown_constraint_kind_raises():
    from oracle.verdicts import meets_constraint
    with pytest.raises(ValueError):
        meets_constraint({"constraint": {"kind": "weekday"}}, 1, None, None)


@pytest.mark.parametrize(
    "end_reason,verdict",
    [("gave_up", "failed"), ("limit_reached", "failed"), ("unfinished", "unfinished"), ("rejected", "rejected"),
     ("provider_failed", "provider_failed")],
)
def test_no_effect_end_reasons(end_reason, verdict):
    assert classify(FEASIBLE, untouched(), end_reason) == verdict
    assert classify(INFEASIBLE, untouched(), end_reason) == verdict
