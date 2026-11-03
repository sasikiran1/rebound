"""A minimal local SMTP server that stores every message it receives (a test inbox, not a mail provider).

    uv run python -m resolve.eval.mailsink --port 1025 --inbox var/mail/inbox.jsonl

It speaks enough SMTP for smtplib (EHLO/HELO, MAIL, RCPT, DATA, RSET, NOOP, QUIT), appends each message as one JSON
line, and never relays anything. `refuse=True` answers every MAIL with 421, to test delivery retries.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import email
import email.policy
import json
from pathlib import Path


class MailSink:
    def __init__(self, inbox: Path | None = None):
        self.inbox = inbox
        self.messages: list[dict] = []
        self.refuse = False
        self.server: asyncio.Server | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 1025) -> MailSink:
        self.server = await asyncio.start_server(self._session, host, port)
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def _store(self, sender: str, rcpts: list[str], data: bytes) -> None:
        msg = email.message_from_bytes(data, policy=email.policy.default)
        body = msg.get_body(("plain",))
        rec = {"received_at": dt.datetime.now(dt.UTC).isoformat(), "from": sender, "to": rcpts,
               "subject": str(msg["subject"] or ""), "message_id": str(msg["message-id"] or ""),
               "body": body.get_content() if body else ""}
        self.messages.append(rec)
        if self.inbox is not None:
            self.inbox.parent.mkdir(parents=True, exist_ok=True)
            with self.inbox.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str):
            writer.write((line + "\r\n").encode())
            await writer.drain()
        sender, rcpts = "", []
        await say("220 resolve-mailsink ready")
        try:
            while line := await reader.readline():
                cmd = line.decode(errors="replace").strip()
                verb = cmd[:4].upper()
                if verb == "EHLO":
                    await say("250-resolve-mailsink")
                    await say("250 8BITMIME")
                elif verb == "HELO":
                    await say("250 resolve-mailsink")
                elif verb == "MAIL":
                    if self.refuse:
                        await say("421 service not available")
                        break
                    sender, rcpts = cmd.split(":", 1)[1].strip(" <>"), []
                    await say("250 OK")
                elif verb == "RCPT":
                    rcpts.append(cmd.split(":", 1)[1].strip(" <>"))
                    await say("250 OK")
                elif verb == "DATA":
                    await say("354 end with <CRLF>.<CRLF>")
                    lines = []
                    while (chunk := await reader.readline()) not in (b".\r\n", b".\n", b""):
                        lines.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                    self._store(sender, rcpts, b"".join(lines))
                    await say("250 OK stored")
                elif verb in ("RSET", "NOOP"):
                    sender, rcpts = ("", []) if verb == "RSET" else (sender, rcpts)
                    await say("250 OK")
                elif verb == "QUIT":
                    await say("221 bye")
                    break
                else:
                    await say("502 not implemented")
        except ConnectionError:
            pass
        finally:
            writer.close()


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=1025)
    ap.add_argument("--inbox", default="var/mail/inbox.jsonl")
    a = ap.parse_args()
    sink = await MailSink(Path(a.inbox)).start(a.host, a.port)
    print(json.dumps({"mailsink": f"{a.host}:{a.port}", "inbox": a.inbox}), flush=True)
    async with sink.server:
        await sink.server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
