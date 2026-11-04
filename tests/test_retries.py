from analysis.retries import classify_goal


def op(tool, args="a", status="ok"):
    return {"tool": tool, "args_hash": args, "status": status}


def test_clean_workflow_is_all_base():
    ops = [op("get_customer_appointments"), op("check_day_availability", "d1"),
           op("check_day_availability", "d2"), op("reschedule_appointment", "s1"), op("get_appointment")]
    assert classify_goal(ops) == ["base"] * 5


def test_same_call_retry_only_right_after_failure():
    ops = [op("check_day_availability", "d1", "timeout"), op("check_day_availability", "d1", "ok")]
    assert classify_goal(ops) == ["base", "same_call_retry"]
    ops = [op("check_day_availability", "d1"), op("check_day_availability", "d1")]
    assert classify_goal(ops) == ["base", "other"]  # exact repeat without failure


def test_switch_to_broad_search_after_day_lookup_failure():
    ops = [op("get_customer_appointments"), op("check_day_availability", "d1", "http_503"),
           op("search_availability", "w")]
    assert classify_goal(ops)[-1] == "switch_to_broad_search"


def test_first_search_without_failure_is_base_second_is_other():
    ops = [op("search_availability", "w1"), op("search_availability", "w2")]
    assert classify_goal(ops) == ["base", "other"]


def test_restart():
    ops = [op("get_customer_appointments"), op("check_day_availability", "d1", "timeout"),
           op("get_customer_appointments")]
    assert classify_goal(ops)[-1] == "restart"


def test_write_after_failed_write_is_other_and_defer_is_base():
    ops = [op("reschedule_appointment", "s1", "http_409"), op("reschedule_appointment", "s2"),
           op("defer_goal", "r")]
    assert classify_goal(ops) == ["base", "other", "base"]


def test_same_call_retry_takes_precedence_over_restart():
    ops = [op("check_day_availability", "d1"), op("get_customer_appointments", "c", "timeout"),
           op("get_customer_appointments", "c")]
    assert classify_goal(ops)[-1] == "same_call_retry"
