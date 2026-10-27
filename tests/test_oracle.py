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


@pytest.mark.parametrize(
    "end_reason,verdict",
    [("gave_up", "failed"), ("limit_reached", "failed"), ("unfinished", "unfinished"), ("rejected", "rejected"),
     ("provider_failed", "provider_failed")],
)
def test_no_effect_end_reasons(end_reason, verdict):
    assert classify(FEASIBLE, untouched(), end_reason) == verdict
    assert classify(INFEASIBLE, untouched(), end_reason) == verdict
