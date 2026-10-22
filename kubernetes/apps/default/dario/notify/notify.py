#!/usr/bin/env python3
"""Daily dario check, run by the dario-notify CronJob. Runbook: docs/dario.md

Anthropic ends a Claude seat's refresh token ~28 days after the browser grant
that minted it, however often it refreshes, and only a person can approve a
new grant. dario keeps that calendar itself (grant_level on /admin/accounts:
warn from day 21, urgent from day 26) but can only say so on stdout. This
turns that verdict, a seat failing auth, or a non-200 /health into an email
and an ntfy push. Silent while all is well. Stdlib only.
"""

import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate

DAY_MS = 86_400_000
# Worst first. Subject (ASCII: it is also an HTTP header), ntfy priority, ntfy tag.
LEVELS = {
    "down": ("dario is down - re-grant now", "urgent", "rotating_light"),
    "urgent": ("dario: re-grant today", "high", "warning"),
    "warn": ("dario: re-grant this week", "default", "hourglass_flowing_sand"),
}


def fetch(path, token=None):
    req = urllib.request.Request(os.environ["DARIO_URL"].rstrip("/") + path)
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def day(ms):
    dt = datetime.fromtimestamp(ms / 1000).astimezone()
    return f"{dt:%a %b} {dt.day}"


def check():
    """Return (level, lines, aliases): the worst level found (None when all is
    well), one line per problem, and the seats that need a re-grant."""
    found, lines, aliases = set(), [], set()

    status, _ = fetch("/health")
    if status != 200:
        found.add("down")
        lines.append(f"The proxy is not serving: /health answered {status}, so clients get 401/503 errors.")

    status, body = fetch("/admin/accounts", os.environ["DARIO_ADMIN_TOKEN"])
    if status != 200:
        lines.append(f"/admin/accounts answered {status}: {body[:200].decode(errors='replace')}")
        return "down", lines, aliases
    accounts = json.loads(body)["accounts"]
    if not accounts:
        found.add("down")
        lines.append("No Claude seat is configured.")

    now = time.time() * 1000
    for acct in accounts:
        alias, grant, wall = acct["alias"], acct.get("grant_level"), acct.get("refresh_wall_at")
        if grant in ("warn", "urgent") and wall:
            if wall > now:
                when = f"~{(wall - now) / DAY_MS:.0f}d to the ~28d wall ({day(wall)})"
            else:
                when = f"{(now - wall) / DAY_MS:.0f}d past the ~28d wall ({day(wall)})"
            found.add(grant)
            aliases.add(alias)
            lines.append(f'Seat "{alias}": grant {acct["grant_age_days"]}d old, {when}.')
        elif grant == "unknown":
            found.add("warn")
            aliases.add(alias)
            lines.append(f'Seat "{alias}": grant age unknown; re-grant it to start the clock.')
        failures = acct.get("consecutive_auth_failures") or 0
        if failures >= 3:
            found.add("urgent")
            aliases.add(alias)
            lines.append(f'Seat "{alias}": {failures} auth failures in a row.')

    return next((level for level in LEVELS if level in found), None), lines, aliases


def message(lines, aliases):
    fixes = ["task dario:login" + ("" if alias == "main" else f" SEAT={alias}") for alias in sorted(aliases)]
    return "\n".join([
        *lines,
        "",
        "From the home-ops repo:",
        *(f"  {fix}" for fix in fixes or ["task dario:status"]),
        "Approve in a browser signed in to the Claude account behind the seat, then paste the code back.",
        f"Runbook: {os.environ['RUNBOOK_URL']}",
    ])


def send_mail(subject, text):
    msg = EmailMessage()
    msg["Subject"] = f"[home-ops] {subject}"
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = os.environ["MAIL_TO"]
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text)
    host, port = os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])
    # 465 is implicit TLS; anything else (the relay uses 587) upgrades with STARTTLS.
    with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(host, port, timeout=30) as smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(msg)


def push(subject, text, priority, tag):
    req = urllib.request.Request(os.environ["NTFY_URL"], data=text.encode(), headers={
        "Title": subject,
        "Priority": priority,
        "Tags": tag,
        "Click": os.environ["RUNBOOK_URL"],
    })
    urllib.request.urlopen(req, timeout=15).close()


def main():
    try:
        level, lines, aliases = check()
    except Exception as err:  # unreachable, DNS, a body that isn't JSON: all mean "go look"
        level, lines, aliases = "down", [f"The check could not read dario: {err}"], set()
    if level is None:
        print("ok: /health is 200 and every seat is inside its grant window")
        return 0

    subject, priority, tag = LEVELS[level]
    text = message(lines, aliases)
    print(f"{subject}\n{text}")
    failed = False
    for name, send in (("email", lambda: send_mail(subject, text)),
                       ("ntfy", lambda: push(subject, text, priority, tag))):
        try:
            send()
            print(f"sent via {name}")
        except Exception as err:
            failed = True
            print(f"{name} failed: {err}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
