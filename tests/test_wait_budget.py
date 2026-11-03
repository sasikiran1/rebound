"""The wait tool sends nothing, so it does not spend the goal's tool-call budget."""

import httpx

from harness.adapter import GoalScope, GoalState, ToolAdapter, load_tools
from harness.events import EventLog

SCOPE = GoalScope("tr", 3, 11, 101, ["R03"])
APPT = {"appointment_id": 101, "customer_id": 11, "status": "scheduled", "service": "hvac_repair"}


async def test_wait_does_not_spend_the_tool_budget(tmp_path):
    async def no_sleep(_s):
        return None

    a = ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=APPT))), load_tools("prompts/tools_v1_wait.json"),
        EventLog(tmp_path, trial_id="t"), sleep=no_sleep)
    st = GoalState()
    for _ in range(5):
        await a.call(SCOPE, st, "wait", {"seconds": 2})
    await a.call(SCOPE, st, "get_appointment", {"appointment_id": 101})
    assert (st.waits, st.tool_calls, st.ops) == (5, 1, 6)
