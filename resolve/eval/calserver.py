"""A local CalDAV server (Radicale) for the technicians' calendars, with one login (HTTP basic auth).

    uv run python -m resolve.eval.calserver --port 5232 --folder var/calendar

Used by the compose profile (resolve-calendar), the recovery check and the demo. Any calendar client (Thunderbird,
Apple Calendar, DAVx5) can subscribe to http://127.0.0.1:5232/fielddesk/tech-<id>/ with the same login. A hosted
CalDAV provider replaces it by RESOLVE_CALDAV_URL / USER / PASSWORD alone.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

USER = "fielddesk"


def command(port: int, folder: Path, password: str, host: str = "127.0.0.1") -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    users = folder / "users"
    users.write_text(f"{USER}:{password}\n")
    users.chmod(0o600)
    return [sys.executable, "-m", "radicale", "--server-hosts", f"{host}:{port}",
            "--storage-filesystem-folder", str(folder / "collections"), "--auth-type", "htpasswd",
            "--auth-htpasswd-filename", str(users), "--auth-htpasswd-encryption", "plain",
            "--rights-type", "owner_only", "--logging-level", "warning"]


class CalServer:
    """Radicale as a child process; `start` waits until it answers, `stop` terminates it."""

    def __init__(self, port: int, folder: Path, password: str = "local-only"):
        self.port, self.folder, self.password = port, Path(folder), password
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{USER}/"

    async def start(self, timeout_s: float = 10.0) -> CalServer:
        self.proc = await asyncio.create_subprocess_exec(*command(self.port, self.folder, self.password),
                                                         stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.DEVNULL)
        async with httpx.AsyncClient(timeout=1.0) as client:
            for _ in range(int(timeout_s * 10)):
                try:
                    await client.get(f"http://127.0.0.1:{self.port}/")
                    return self
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
        raise RuntimeError(f"calendar server did not start on port {self.port}")

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()
        self.proc = None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5232)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--folder", default="var/calendar")
    a = ap.parse_args()
    cmd = command(a.port, Path(a.folder), os.environ.get("RESOLVE_CALDAV_PASSWORD", "local-only"), a.host)
    os.execv(cmd[0], cmd)


if __name__ == "__main__":
    main()
