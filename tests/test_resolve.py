"""Resolve: intent validation, patching, the deterministic evaluator, oracle staging and paged search (offline)."""

import datetime as dt

import httpx
import pytest

from resolve import cases as C
from resolve import intent as I
from resolve.api import FieldDesk
from resolve.evaluate import decide
from resolve.oracle import _stage

DANA, ALEX, JP, JW = 11, 12, 13, 14
FACTS = {
    "today": "2026-11-10", "horizon_days": 13,
    "technicians": [{"technician_id": DANA, "name": "Dana Okafor"}, {"technician_id": ALEX, "name": "Alex Rivera"},
                    {"technician_id": JP, "name": "Jordan Pike"}, {"technician_id": JW, "name": "Jordan Wu"}],
    "history": [{"appointment_id": 501, "technician_id": DANA, "technician_name": "Dana Okafor"}],
}
NAMES = {t["technician_id"]: t["name"] for t in FACTS["technicians"]}


def rule(kind, strength="flexible", quote="after three", **kw):
    return {"kind": kind, "strength": strength, "quote": quote, **kw}


def slot(i, when, tech):
    return {"slot_id": i, "starts_at": f"2026-11-{when}", "technician_id": tech}


SAID = "I need someone after three this week. Dana if possible. Don't push it into next week without checking."


def problems(intent, said=SAID):
    return I.validate(I.assign_ids(intent), FACTS, said)


def test_valid_intent_passes():
    intent = {"rules": [rule("start_at_or_after", time="15:00"),
                        rule("dates_between", quote="this week", date_from="2026-11-10", date_to="2026-11-13"),
                        rule("technician_is", "prefer", "Dana if possible", technician_ids=[DANA])]}
    assert problems(intent) == []


@pytest.mark.parametrize("bad, expect", [
    (rule("dates_between", date_from="2026-11-13", date_to="2026-11-11"), "is after date_to"),
    (rule("on_dates", dates=["2026-11-31"]), "not a real date"),
    (rule("on_dates", dates=["2026-12-15"]), "outside the bookable horizon"),
    (rule("on_dates", quote="any day you like", dates=["2026-11-12"]), "not in the customer's words"),
    (rule("technician_is", technician_ids=[ALEX]), "was not named by the customer"),
    (rule("technician_is", technician_ids=[999]), "not on the roster"),
    (rule("on_dates", dates=["2026-11-12"], only_on_dates=["2026-11-12"]), "only_on_dates is for time"),
    (rule("technician_is", technician_ids=[ALEX], from_appointment=501), "was with technician"),
    (rule("technician_is", technician_ids=[DANA], from_appointment=777), "not one of this customer's past visits"),
    (rule("on_dates", time="15:00", dates=["2026-11-12"]), "do not belong to kind"),
])
def test_validator_rejects(bad, expect):
    assert any(expect in p for p in problems({"rules": [bad]})), problems({"rules": [bad]})


def test_impossible_window_and_reference_grounding():
    window = {"rules": [rule("start_at_or_after", time="16:00"), rule("start_before", time="09:00")]}
    assert any("no start time" in p for p in problems(window))
    # scoped to different dates, the same two limits are possible
    ok = {"rules": [rule("start_at_or_after", time="16:00", only_on_dates=["2026-11-16"]),
                    rule("start_before", time="09:00", only_on_dates=["2026-11-17"])]}
    assert problems(ok) == []
    ref = {"rules": [rule("technician_is", quote="after three", technician_ids=[DANA], from_appointment=501)]}
    assert problems(ref) == []


def test_shared_first_name_must_be_unclear():
    said = "Move it to whenever Jordan is free this week."
    merged = {"rules": [rule("technician_is", quote="whenever Jordan is free", technician_ids=[JP, JW])]}
    assert any("fits 2 technicians" in p for p in problems(merged, said))
    guessed = {"rules": [rule("technician_is", quote="whenever Jordan is free", technician_ids=[JP])]}
    assert any("fits 2 technicians" in p for p in problems(guessed, said))
    unclear = {"rules": [], "unclear": [{"phrase": "Jordan", "readings": [
        {"label": "Jordan Pike", "rules": [rule("technician_is", quote="Jordan", technician_ids=[JP])]},
        {"label": "Jordan Wu", "rules": [rule("technician_is", quote="Jordan", technician_ids=[JW])]}]}]}
    assert problems(unclear, said) == []
    assert problems(guessed, "whenever Jordan is free, Jordan Pike please") == []  # the surname tells them apart


def test_patch_keeps_untouched_rules_and_refuses_kind_change():
    v1 = I.assign_ids({"rules": [rule("on_dates", quote="Friday", dates=["2026-11-13"]),
                                 rule("technician_is", "prefer", "Dana", technician_ids=[DANA])]})
    with pytest.raises(I.PatchError, match="must keep the kind"):
        I.apply_patch(v1, {"ops": [{"op": "replace_rule", "rule_id": "r1",
                                    "rule": rule("start_at_or_after", time="14:00")}]}, None)
    v2, decision = I.apply_patch(v1, {"ops": [{"op": "add_rule", "rule": rule("start_at_or_after", time="14:00")}]},
                                 None)
    assert decision == "changed" and v2["rules"][:2] == v1["rules"] and v2["rules"][2]["id"] == "r3"
    assert I.diff(v1, v2) == {"added": ["r3"], "removed": [], "changed": [], "approved_slots": []}
    assert I.apply_patch(v1, {"ops": [{"op": "decline"}]}, None)[1] == "declined"


def test_accept_proposal_only_for_offered_slots():
    v1 = I.assign_ids({"rules": [rule("on_dates", quote="Friday", dates=["2026-11-13"])]})
    proposal = {"candidates": [{"slot_id": 7}, {"slot_id": 8}]}
    with pytest.raises(I.PatchError):
        I.apply_patch(v1, {"ops": [{"op": "accept_proposal", "slot_id": 9}]}, proposal)
    with pytest.raises(I.PatchError):  # two offered: the customer must say which
        I.apply_patch(v1, {"ops": [{"op": "accept_proposal"}]}, proposal)
    v2, _ = I.apply_patch(v1, {"ops": [{"op": "accept_proposal", "slot_id": 8}]}, proposal)
    assert v2["approved_slots"] == [8]


INTENT = I.assign_ids({"rules": [
    rule("start_at_or_after", time="15:00"),
    rule("dates_between", quote="this week", date_from="2026-11-10", date_to="2026-11-13"),
    rule("technician_is", "prefer", "Dana", technician_ids=[DANA])]})


def test_evaluator_books_best_authorized_without_asking():
    slots = [slot(1, "13T15:30", ALEX), slot(2, "13T16:00", DANA), slot(3, "12T10:00", DANA)]
    d = decide(INTENT, slots, NAMES)
    assert d.kind == "book" and d.slot["slot_id"] == 2  # preference met beats earlier


def test_evaluator_asks_permission_with_real_slots_only():
    slots = [slot(1, "13T10:00", ALEX), slot(2, "16T15:00", ALEX), slot(3, "16T16:30", ALEX)]
    d = decide(INTENT, slots, NAMES)
    assert d.kind == "permission" and [c["slot_id"] for c in d.candidates] == [1, 2]
    must = I.assign_ids({"rules": [{**r, "strength": "must"} if r["kind"] != "technician_is" else r
                                   for r in INTENT["rules"]]})
    assert decide(must, slots, NAMES).kind == "no_options"
    assert decide({**INTENT, "approved_slots": [3]}, slots, NAMES).slot["slot_id"] == 3


def test_evaluator_clarifies_only_when_readings_disagree():
    item = {"phrase": "Jordan", "readings": [
        {"label": "Jordan Pike", "rules": [rule("technician_is", quote="Jordan", technician_ids=[JP])]},
        {"label": "Jordan Wu", "rules": [rule("technician_is", quote="Jordan", technician_ids=[JW])]}]}
    intent = {"rules": [], "unclear": [item]}
    d = decide(intent, [slot(1, "11T10:00", JP), slot(2, "12T13:00", JW)], NAMES)
    assert d.kind == "clarify" and "Jordan Pike" in d.question
    # both readings accept the same slot: no question needed
    same = {"rules": [], "unclear": [{"phrase": "afternoon", "readings": [
        {"label": "after 12", "rules": [rule("start_at_or_after", time="12:00")]},
        {"label": "after 13", "rules": [rule("start_at_or_after", time="13:00")]}]}]}
    assert decide(same, [slot(1, "11T12:30", JP), slot(2, "11T14:00", JW)], NAMES).slot["slot_id"] == 2


def test_alternatives_in_order():
    intent = I.assign_ids({"rules": [], "alternatives": [
        {"label": "Wed pm Sam", "rules": [rule("on_dates", quote="w", dates=["2026-11-11"]),
                                          rule("start_at_or_after", quote="w", time="12:00")]},
        {"label": "Thu am", "rules": [rule("on_dates", quote="t", dates=["2026-11-12"]),
                                      rule("start_before", quote="t", time="12:00")]}]})
    assert decide(intent, [slot(1, "12T09:30", ALEX), slot(2, "11T14:00", DANA)], NAMES).slot["slot_id"] == 2
    assert decide(intent, [slot(1, "12T09:30", ALEX), slot(3, "11T10:00", DANA)], NAMES).slot["slot_id"] == 1


def test_oracle_stage_counts_turns_before_the_change():
    t = [dt.datetime(2026, 1, 1, 0, 0, s) for s in (0, 10, 20)]
    assert _stage(dt.datetime(2026, 1, 1, 0, 0, 5), t) == 0
    assert _stage(dt.datetime(2026, 1, 1, 0, 0, 25), t) == 2


def test_case_files_are_consistent():
    for split in ("dev", "heldout", "faults"):
        for case in C.load(C.CASE_DIR / f"{split}.yaml"):
            assert C.check(case) == [], case.id


async def test_search_all_pages_past_the_first_page():
    rows = [{"slot_id": i, "starts_at": "2026-11-12T09:00", "technician_id": 1, "date": "2026-11-12"}
            for i in range(61)]

    def backend(req: httpx.Request) -> httpx.Response:
        off = int(req.url.params.get("offset", 0))
        assert req.url.params["start_after"] == "00:00"  # selects the paged query
        return httpx.Response(200, json={"total_count": 61, "slots": rows[off:off + 50]})

    api = FieldDesk("http://fd.test", client=httpx.AsyncClient(transport=httpx.MockTransport(backend)))
    got = await api.search_all("hvac_repair", "2026-11-10", "2026-11-23", ["Z01"])
    assert [s["slot_id"] for s in got] == list(range(61))
