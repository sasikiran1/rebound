"""Re-run analysis.metrics.analyze on trial bundles WITHOUT overwriting recorded files; compare with the saved verdict.

Usage: python scripts/reanalyze_check.py <trial_dir>...
Raw checksums are verified first (any mismatch: the trial is skipped and reported). The new result is written to
<trial>/reanalysis/verdict.json. For trials with a saved verdict.json, every leaf value is compared.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from analysis.metrics import analyze  # noqa: E402
from harness.trial import verify_sums  # noqa: E402


def leaves(d, prefix=""):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from leaves(v, f"{prefix}.{k}")
    elif isinstance(d, list):
        for i, v in enumerate(d):
            yield from leaves(v, f"{prefix}[{i}]")
    else:
        yield prefix, d


for arg in sys.argv[1:]:
    d = Path(arg)
    bad = verify_sums(d) if (d / "SHA256SUMS").exists() else ["no SHA256SUMS"]
    if bad:
        print(d.name, "SKIPPED integrity", bad[:3])
        continue
    new = json.loads(json.dumps(analyze(d), default=str))
    out = d / "reanalysis"
    out.mkdir(exist_ok=True)
    (out / "verdict.json").write_text(json.dumps(new, indent=2) + "\n")
    saved_p = d / "verdict.json"
    if not saved_p.exists():
        print(d.name, "no saved verdict; reanalysis status", new["status"], new.get("invalid_reasons"),
              new.get("inconclusive_reasons"))
        continue
    saved = dict(leaves(json.loads(saved_p.read_text())))
    diff = [(k, saved.get(k), v) for k, v in leaves(new) if saved.get(k) != v]
    print(d.name, "identical" if not diff else f"{len(diff)} differences: {diff[:4]}")
