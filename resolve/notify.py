"""Deliver queued customer notifications over SMTP. No model.

Notifications are written by `Store.add_turn` in the same transaction as the question or outcome they announce.
`Notifier.drain` claims due ones (FOR UPDATE SKIP LOCKED), sends each as one email, and marks it sent; a failed send
stays queued with exponential backoff (5 s, 10 s, 20 s, ...) and is marked failed after MAX_ATTEMPTS. A message may
be sent twice if the process dies between the SMTP answer and the update; each carries a stable Message-ID
(`<request>.<turn>@resolve`) so a receiving system can drop the duplicate.

Configuration: RESOLVE_SMTP_HOST, RESOLVE_SMTP_PORT (default 1025), RESOLVE_SMTP_USER / RESOLVE_SMTP_PASSWORD
(optional; uses STARTTLS when set), RESOLVE_MAIL_FROM.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage

from resolve.store import Store

MAX_ATTEMPTS = 6


class Notifier:
    def __init__(self, store: Store, host: str, port: int = 1025, sender: str = "resolve@example.test",
                 user: str | None = None, password: str | None = None, timeout_s: float = 5.0):
        self.store, self.host, self.port, self.sender = store, host, port, sender
        self.user, self.password, self.timeout_s = user, password, timeout_s

    @classmethod
    def from_env(cls, store: Store) -> Notifier | None:
        host = os.environ.get("RESOLVE_SMTP_HOST")
        if not host:
            return None
        return cls(store, host, int(os.environ.get("RESOLVE_SMTP_PORT", "1025")),
                   os.environ.get("RESOLVE_MAIL_FROM", "resolve@example.test"),
                   os.environ.get("RESOLVE_SMTP_USER"), os.environ.get("RESOLVE_SMTP_PASSWORD"))

    def _send(self, row: dict) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, row["recipient"], row["subject"]
        msg["Message-ID"] = f"<{row['request_id'].replace(':', '.')}.{row['turn_n']}@resolve>"
        msg.set_content(row["body"] + "\n\nReply to this message to answer. Request: " + row["request_id"])
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
            if self.user:
                smtp.starttls()
                smtp.login(self.user, self.password or "")
            smtp.send_message(msg)

    async def drain(self, limit: int = 20) -> dict:
        """Send every due notification (at most `limit`). Returns counts."""
        sent = failed = retry = 0
        for _ in range(limit):
            async with self.store.pool.acquire() as c, c.transaction():
                row = await c.fetchrow(
                    "SELECT * FROM resolve.notifications WHERE status = 'pending' AND next_attempt_at <= "
                    "clock_timestamp() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")
                if row is None:
                    break
                row = dict(row)
                try:
                    await asyncio.to_thread(self._send, row)
                except (OSError, smtplib.SMTPException) as exc:
                    attempts = row["attempts"] + 1
                    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                    await c.execute(
                        "UPDATE resolve.notifications SET attempts = $2, status = $3, last_error = $4, "
                        "next_attempt_at = clock_timestamp() + make_interval(secs => $5) WHERE id = $1",
                        row["id"], attempts, status, f"{type(exc).__name__}: {str(exc)[:200]}",
                        5.0 * 2 ** (attempts - 1))
                    failed += status == "failed"
                    retry += status == "pending"
                    continue
                await c.execute("UPDATE resolve.notifications SET status = 'sent', attempts = attempts + 1, "
                                "sent_at = clock_timestamp(), last_error = NULL WHERE id = $1", row["id"])
                sent += 1
        return {"sent": sent, "retry": retry, "failed": failed}
