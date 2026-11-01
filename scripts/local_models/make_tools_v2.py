"""Write the two tool-information intervention schemas from prompts/tools_v1.json (run once, output committed).

tools_v2_time.json: check_day_availability and search_availability gain optional start_after / start_before (HH:MM).
tools_v2_page.json: the same two tools gain an optional offset (pagination); responses already report the total.
Nothing else changes: names, descriptions of other tools and required arguments are identical to v1.
"""

import copy
import json
from pathlib import Path

v1 = json.loads(Path("prompts/tools_v1.json").read_text())
EXTRA = {
    "time": {"start_after": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$",
                             "description": "Optional. Only slots starting at or after this local time (HH:MM)."},
             "start_before": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$",
                              "description": "Optional. Only slots starting before this local time (HH:MM)."}},
    "page": {"offset": {"type": "integer", "minimum": 0,
                        "description": "Optional. Skip this many slots, to see results beyond a truncated list."}},
}
for name, extra in EXTRA.items():
    tools = copy.deepcopy(v1)
    for t in tools:
        if t["name"] in ("check_day_availability", "search_availability"):
            t["parameters"]["properties"].update(extra)
    Path(f"prompts/tools_v2_{name}.json").write_text(json.dumps(tools, indent=2) + "\n")
    print("wrote", f"prompts/tools_v2_{name}.json")
