"""Run once per minute from systemd; no browser timer or open app is needed."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from pywebpush import WebPushException, webpush
from requests import Session

from .database import connect
from .push import push_config
from .security import local_day_bounds, resolve_timezone, utc_iso_from

logger = logging.getLogger(__name__)
# Brief outages can recover just after 20:00. Never catch up later that evening.
REMINDER_WINDOW_MINUTES = 10


class PushSession(Session):
    def request(self, *args: Any, **kwargs: Any):
        # Subscription URLs must never redirect a signed request to another host.
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


def send_reminder(subscription: dict[str, Any], day: str) -> None:
    with PushSession() as session:
        webpush(subscription_info={"endpoint": subscription["endpoint"],
                    "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}},
                data=json.dumps({"title": "语境学词", "body": "今天还没有学习，花几分钟学几个单词吧。",
                    "tag": f"learning-reminder-{day}", "url": "/?notification=study#study"}, ensure_ascii=False),
                vapid_private_key=os.environ["WEB_PUSH_PRIVATE_KEY"],
                vapid_claims={"sub": os.environ["WEB_PUSH_SUBJECT"]},
                ttl=600, timeout=10, requests_session=session)


def run_reminders(now: datetime | None = None, *, dry_run: bool = False) -> dict[str, int]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    counts = {"eligible": 0, "sent": 0, "failed": 0, "expired": 0}
    if not push_config()["enabled"]:
        return counts
    stamp = utc_iso_from(now)
    with connect() as conn:
        rows = conn.execute("""SELECT p.*,u.timezone FROM push_subscriptions p
            JOIN users u ON u.id=p.user_id WHERE u.role='learner'""").fetchall()
        if not dry_run:
            conn.execute("DELETE FROM push_deliveries WHERE local_day < ?", ((now - timedelta(days=32)).date().isoformat(),))
    for row in rows:
        subscription = dict(row)
        tz = resolve_timezone(subscription["timezone"])
        local = now.astimezone(tz)
        if local.hour != 20 or local.minute >= REMINDER_WINDOW_MINUTES:
            continue
        day = local.date().isoformat()
        start, end = local_day_bounds(local.date(), tz)
        endpoint_hash = hashlib.sha256(subscription["endpoint"].encode()).hexdigest()
        identity = (subscription["user_id"], day, endpoint_hash)
        with connect() as conn:
            # Serialize claiming with subscription changes and concurrent timers.
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM push_subscriptions WHERE endpoint=? AND user_id=? AND updated_at=?",
                (subscription["endpoint"], subscription["user_id"], subscription["updated_at"])).fetchone()
            learned = conn.execute("SELECT 1 FROM review_history WHERE user_id=? AND review_time>=? AND review_time<? LIMIT 1",
                (subscription["user_id"], start, end)).fetchone()
            delivered = conn.execute("SELECT * FROM push_deliveries WHERE user_id=? AND local_day=? AND endpoint_hash=?", identity).fetchone()
            if (not current or learned or (delivered and
                    (delivered["status"] != "failed" or delivered["attempts"] >= 3
                     or delivered["updated_at"] > utc_iso_from(now - timedelta(seconds=60))))):
                continue
            counts["eligible"] += 1
            if dry_run:
                continue
            conn.execute("""INSERT INTO push_deliveries(user_id,local_day,endpoint_hash,status,updated_at)
                VALUES(?,?,?,'claimed',?) ON CONFLICT(user_id,local_day,endpoint_hash) DO UPDATE SET
                status='claimed',attempts=push_deliveries.attempts+1,updated_at=excluded.updated_at""", (*identity, stamp))
        try:
            send_reminder(subscription, day)
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            with connect() as conn:
                if status in (404, 410):
                    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=? AND updated_at=?",
                        (subscription["endpoint"], subscription["user_id"], subscription["updated_at"]))
                    counts["expired"] += 1
                # Without an HTTP response delivery is ambiguous; do not resend.
                if status is not None:
                    conn.execute("UPDATE push_deliveries SET status='failed' WHERE user_id=? AND local_day=? AND endpoint_hash=?", identity)
            counts["failed"] += 1
            # Never log capability URLs, key material, or response bodies.
            logger.warning("Reminder delivery failed for user %s (HTTP %s)", subscription["user_id"], status)
        except Exception as exc:
            # A timeout may happen after acceptance. Keep the claim to avoid a
            # second notification; a future day's reminder remains eligible.
            counts["failed"] += 1
            logger.warning("Reminder delivery failed for user %s (%s)", subscription["user_id"], type(exc).__name__)
        else:
            with connect() as conn:
                conn.execute("UPDATE push_deliveries SET status='sent' WHERE user_id=? AND local_day=? AND endpoint_hash=?", identity)
            counts["sent"] += 1
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logger.info("Learning reminder run: %s", run_reminders(dry_run=args.dry_run))
