"""Windows-side Ollama relay, run with the Windows Python from WSL (harness/ollama.py starts it).

Ollama on this laptop listens on Windows 127.0.0.1 only, which WSL2 (NAT networking) cannot reach. Rather than
reconfigure or expose Ollama, the runner talks to this process over stdin/stdout: one JSON line per request
({"id", "path", "payload"}), one JSON line per response ({"id", "ok", "status", "elapsed_s", "body" | "error"}).
Each request is exactly one HTTP POST to 127.0.0.1:11434. No retries.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OLLAMA = "http://127.0.0.1:11434"
TIMEOUT_S = 300
lock = threading.Lock()
out = sys.stdout.buffer


def handle(line: bytes) -> None:
    msg = json.loads(line)
    t0 = time.perf_counter()
    res = {"id": msg["id"]}
    try:
        body = None if msg.get("payload") is None else json.dumps(msg["payload"]).encode()  # None: a GET
        req = urllib.request.Request(OLLAMA + msg.get("path", "/api/chat"), data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            res.update(ok=True, status=r.status, body=json.loads(r.read()))
    except urllib.error.HTTPError as exc:
        res.update(ok=False, status=exc.code, error=exc.read().decode(errors="replace")[:2000])
    except Exception as exc:  # noqa: BLE001 - reported to the runner, never retried here
        res.update(ok=False, status=None, error=f"{type(exc).__name__}: {exc}"[:2000])
    res["elapsed_s"] = time.perf_counter() - t0
    data = json.dumps(res, ensure_ascii=False).encode() + b"\n"
    with lock:
        out.write(data)
        out.flush()


def main() -> None:
    with ThreadPoolExecutor(max_workers=64) as pool:
        for line in sys.stdin.buffer:
            if line.strip():
                pool.submit(handle, line)


if __name__ == "__main__":
    main()
