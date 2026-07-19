#!/usr/bin/env python3
"""Seat-availability watcher for The Odyssey IMAX 70mm at Cinemark Dallas XD and IMAX.

Watches for:
  1. Newly available seats (excluding rows A-D) in showtimes between MIN/MAX hour
  2. Entirely new dates gaining Odyssey IMAX 70mm showtimes

All state lives in state.json next to this script; alerts append to alerts.log.
Notification is a single function (`notify`) — swap in Discord/text/etc. later.

Usage:
  python3 watch.py --once             # single sweep
  python3 watch.py                    # loop forever
  python3 watch.py --dates 2026-08-08 # restrict to specific dates (testing)
  python3 watch.py --report           # print availability from state (no network)
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ config
THEATER_SLUG = "tx-dallas/cinemark-dallas-xd-and-imax"
MOVIE_ID = "104867"  # The Odyssey IMAX 70MM
MOVIE_NAME = "The Odyssey IMAX 70MM"
EXCLUDED_ROWS = {"A", "B", "C", "D"}
MIN_HOUR = 9   # drop shows starting before 9:00am (kills the 7:45am and 2:30am)
MAX_HOUR = 23  # inclusive; drop shows starting after 11:59pm
POLL_MINUTES = 5           # sleep between sweeps (a full sweep itself takes ~20 min)
DATE_SCAN_EVERY = 3        # probe for brand-new dates every Nth cycle
REQUEST_GAP_SECONDS = 8.0  # politeness delay between requests (+0-4s jitter).
                           # Empirically cinemark.com allows ~60-70 requests per
                           # ~10 min window (429 beyond that), so hold ~6/min.
BACKOFF_SCHEDULE = [120, 300, 900]  # seconds after 429/403/5xx (or Retry-After if larger)

BASE = "https://www.cinemark.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
ALERT_LOG = HERE / "alerts.log"
LOCK_FILE = HERE / "watcher.pid"

SHOWTIME_LINK = re.compile(
    r'/TicketSeatMap/\?TheaterId=(\d+)&(?:amp;)?ShowtimeId=(\d+)&(?:amp;)?'
    r'CinemarkMovieId=' + MOVIE_ID + r'&(?:amp;)?Showtime=([\d\-T:]+)'
)
AVAILABLE_SEAT = re.compile(
    r'<button[^>]*class="seatAvailable seatBlock"[^>]*info="([A-Z]+),(\d+),'
)
DATE_VALUE = re.compile(r'data-datevalue="(\d{4}-\d{2}-\d{2})"')


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
    })
    retry_after = 0
    for attempt, backoff in enumerate([0, *BACKOFF_SCHEDULE]):
        if backoff:
            wait = max(backoff, retry_after)
            log(f"rate-limited/blocked, backing off {wait}s (attempt {attempt})")
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            time.sleep(REQUEST_GAP_SECONDS + 4 * random.random())
            return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 403, 500, 502, 503):
                raise
            try:
                retry_after = min(int(e.headers.get("Retry-After", "0")), 1800)
            except ValueError:
                retry_after = 0
        except (urllib.error.URLError, TimeoutError):
            pass  # transient network hiccup — retry on the same schedule
    raise RuntimeError(f"gave up fetching {url} after {len(BACKOFF_SCHEDULE)} backoffs")


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(title: str, message: str) -> None:
    """Alert sink: stdout + alerts.log + macOS notification + optional hook.

    To add a channel later (ntfy/Discord/SMS), drop an executable named
    `notify-hook` next to this script; it is called as: notify-hook TITLE MESSAGE
    """
    log(f"ALERT: {title} — {message}")
    with ALERT_LOG.open("a") as f:
        f.write(f"{datetime.now().isoformat()}  {title}: {message}\n")
    try:
        safe_msg = message.replace('"', "'")
        safe_title = title.replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass  # notification is best-effort; the log line is the record
    hook = HERE / "notify-hook"
    if hook.exists() and os.access(hook, os.X_OK):
        try:
            subprocess.run([str(hook), title, message], capture_output=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            log(f"WARN: notify-hook failed: {e!r}")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"dates": {}, "seats": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def theater_page_dates() -> list[str]:
    html = fetch(f"{BASE}/theatres/{THEATER_SLUG}")
    return sorted(set(DATE_VALUE.findall(html)))


def odyssey_showtimes(date: str) -> dict:
    """Return {showtime_id: iso_datetime} for the movie on the given strip date."""
    html = fetch(f"{BASE}/theatres/{THEATER_SLUG}?showDate={date}")
    return {sid: iso for _theater, sid, iso in SHOWTIME_LINK.findall(html)}


def qualifying(iso: str) -> bool:
    hour = datetime.fromisoformat(iso).hour
    return MIN_HOUR <= hour <= MAX_HOUR


def good_seats(theater_id: str, showtime_id: str, iso: str) -> list[str]:
    """Available seats not in the excluded front rows, e.g. ['F12', 'F13']."""
    url = (f"{BASE}/TicketSeatMap/?TheaterId={theater_id}&ShowtimeId={showtime_id}"
           f"&CinemarkMovieId={MOVIE_ID}&Showtime={iso}")
    html = fetch(url)
    if "seatBlock" not in html:
        log(f"WARN: seat map for showtime {showtime_id} returned no seat markup "
            f"(blocked or page changed?)")
        return []
    return sorted(
        f"{row}{num}" for row, num in AVAILABLE_SEAT.findall(html)
        if row not in EXCLUDED_ROWS
    )


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%-I:%M%p").lower()


def prune_past(state: dict) -> None:
    today = date_cls.today().isoformat()
    stale = [d for d in state["dates"] if d < today]
    for d in stale:
        for sid in state["dates"][d]["showtimes"]:
            state["seats"].pop(sid, None)
        del state["dates"][d]


def report(state: dict) -> None:
    print(f"\n{MOVIE_NAME} @ Cinemark Dallas XD and IMAX (TheaterId 207)")
    print(f"Filters: rows {'-'.join(sorted(EXCLUDED_ROWS))} excluded, "
          f"shows between {MIN_HOUR}:00 and {MAX_HOUR}:59 only\n")
    tracked = {d: v for d, v in sorted(state["dates"].items()) if v["showtimes"]}
    if not tracked:
        print("No dates tracked yet — run a sweep first.")
        return
    print(f"On-sale dates with showtimes: {min(tracked)} → {max(tracked)} "
          f"({len(tracked)} dates)\n")
    any_seats = False
    for d, info in tracked.items():
        lines = []
        for sid, iso in sorted(info["showtimes"].items(), key=lambda kv: kv[1]):
            if not qualifying(iso):
                continue
            seats = state["seats"].get(sid, [])
            if seats:
                lines.append(f"    {fmt_time(iso):>8}  {len(seats):>3} seats: "
                             f"{', '.join(seats[:14])}{'…' if len(seats) > 14 else ''}")
        if lines:
            any_seats = True
            print(f"  {d} ({datetime.fromisoformat(d).strftime('%a')})")
            print("\n".join(lines))
    if not any_seats:
        print("No qualifying seats anywhere right now. The watcher will alert "
              "when one opens.")


def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # raises if no such process
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def sweep(state: dict, scan_dates: bool, only_dates: list[str] | None) -> None:
    first_run = not state["dates"]
    prune_past(state)

    # --- date discovery -------------------------------------------------
    if scan_dates or first_run or only_dates:
        strip = only_dates or theater_page_dates()
        for date in strip:
            known = state["dates"].get(date)
            if known and known.get("showtimes"):
                continue  # already tracking; showtime IDs are stable
            try:
                shows = odyssey_showtimes(date)
            except Exception as e:  # noqa: BLE001 — skip this date, keep sweeping
                log(f"WARN: date probe {date} failed: {e!r}")
                continue
            if shows:
                state["dates"][date] = {"showtimes": shows}
                if not first_run:
                    times = ", ".join(sorted(fmt_time(i) for i in shows.values()))
                    notify("New Odyssey date!",
                           f"{MOVIE_NAME} added for {date}: {times}")
            else:
                state["dates"].setdefault(date, {"showtimes": {}})
        log(f"date scan: tracking {sum(1 for d in state['dates'].values() if d['showtimes'])} "
            f"dates with showtimes")
        save_state(state)

    # --- seat scan ------------------------------------------------------
    watch = [
        (date, sid, iso)
        for date, info in sorted(state["dates"].items())
        for sid, iso in sorted(info["showtimes"].items())
        if qualifying(iso) and (not only_dates or date in only_dates)
    ]
    total_good = 0
    for i, (date, sid, iso) in enumerate(watch):
        try:
            seats = good_seats("207", sid, iso)
        except Exception as e:  # noqa: BLE001 — skip this showtime, keep sweeping
            log(f"WARN: seat check {date} {fmt_time(iso)} failed: {e!r}")
            continue
        if i % 10 == 9:
            save_state(state)
        total_good += len(seats)
        prev = set(state["seats"].get(sid, []))
        fresh = [s for s in seats if s not in prev]
        state["seats"][sid] = seats
        if fresh and not first_run:
            notify(f"Seat open {date} {fmt_time(iso)}",
                   f"{len(fresh)} new: {', '.join(fresh[:8])}"
                   + (f" (+{len(fresh) - 8} more)" if len(fresh) > 8 else ""))
        if seats:
            log(f"  {date} {fmt_time(iso)}: {len(seats)} good seats "
                f"({', '.join(seats[:10])}{'…' if len(seats) > 10 else ''})")
    log(f"seat scan: {len(watch)} showtimes checked, {total_good} qualifying seats total")
    if first_run:
        log("first run — baseline recorded, no alerts fired")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single sweep, then exit")
    ap.add_argument("--dates", nargs="*", help="restrict to specific YYYY-MM-DD dates")
    ap.add_argument("--report", action="store_true",
                    help="print current availability from state.json and exit (no network)")
    args = ap.parse_args()

    if args.report:
        report(load_state())
        return

    if not args.once and not acquire_lock():
        print(f"another watcher is already running (pid in {LOCK_FILE}); exiting")
        sys.exit(1)

    cycle = 0
    while True:
        state = load_state()
        try:
            sweep(state, scan_dates=(cycle % DATE_SCAN_EVERY == 0), only_dates=args.dates)
        except Exception as e:  # noqa: BLE001 — keep the loop alive on transient errors
            log(f"ERROR during sweep: {e!r}")
        save_state(state)
        if args.once:
            break
        cycle += 1
        sleep_s = POLL_MINUTES * 60 + random.uniform(-30, 30)
        log(f"sleeping {sleep_s / 60:.1f} min")
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
