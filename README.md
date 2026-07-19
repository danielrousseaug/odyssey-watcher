# cinemark-seat-alert

Email alerts when seats open up at a sold-out Cinemark showing.

Point it at any Cinemark theater and movie, say which rows and showtimes you
would actually accept, and it emails you when a matching seat frees up — or
when new dates go on sale. Runs entirely on GitHub Actions: no server, no email
provider, no API keys, nothing to pay for.

Built to catch cancellations for The Odyssey in IMAX 70mm, which sold out
weeks ahead at all the theaters that can project it. Good seats reappear all
the time — someone returns two tickets, a hold expires — and they go to
whoever happens to be looking. This looks every 30 minutes so you don't.

## How it works

Cinemark's site is fully server-rendered; every date, showtime, and seat is in
the HTML of three plain GET requests. On a schedule, a GitHub Actions job:

1. fetches the theater page (every date on sale) and each date's showtimes,
2. fetches the seat map for each showing that passes your filters,
3. diffs availability against the previous run — the state is a JSON snapshot
   the job commits back to this repo,
4. on a newly opened seat or a newly listed date, opens an issue that
   @mentions you. GitHub delivers the email.

It paces itself to about six requests a minute with backoff, because Cinemark
rate-limits somewhere around 60-70 requests per ten minutes. A full sweep of
~90 showtimes takes about 20 unhurried minutes.

## Setup

1. Fork this repo. Keep the fork public — public repos get unlimited free
   Actions minutes.
2. Edit `config.toml`: your theater, movie, and seat preferences (below).
3. Delete `state.json` and `alerts.log` — they belong to this repo's hunt.
4. Enable workflows on your fork (Actions tab, one button).
5. Run the `watch` workflow once by hand (Actions → watch → Run workflow).
   The first sweep records a quiet baseline; alerts start with the second.

Emails arrive via GitHub notifications, which are on by default — check that
github.com/settings/notifications points at an inbox you read.

## Configuration

Everything lives in `config.toml`:

| key | meaning |
|---|---|
| `theater` | slug from the theater page URL: `cinemark.com/theatres/<slug>` |
| `movie_id` | numeric id for the movie (finding it: below) |
| `movie_name` | only used in alert text |
| `timezone` | the theater's IANA timezone, e.g. `America/Chicago` |
| `excluded_rows` | rows you refuse, e.g. `["A", "B", "C", "D"]` |
| `earliest_showtime` / `latest_showtime` | accept window, 24h `HH:MM`, theater-local |
| `party_size` | alert only when this many adjacent seats open together |

To find `movie_id`: open your theater's page on cinemark.com, right-click any
showtime of your movie, copy the link. It looks like
`/TicketSeatMap/?TheaterId=...&CinemarkMovieId=104867&...` — that number is it.

## Running it yourself

`watch.py` is a single file, standard library only, Python 3.11+:

    python3 watch.py --once      # one sweep
    python3 watch.py             # loop forever
    python3 watch.py --report    # current availability, from state, no network

Alerts print to stdout and append to `alerts.log`. For any other channel, drop
an executable named `notify-hook` next to the script — it gets called with
title and message. The one in this repo files GitHub issues when running under
Actions and does nothing locally.

## Caveats

- A seat that opens and re-sells within one sweep is missed. That is the price
  of polite pacing; sniping contested seats needs a faster tool than this.
- Scraping ticketing sites is generally against their terms of service. This
  reads public pages gently and buys nothing. Keep it that way.
- Not affiliated with Cinemark.
