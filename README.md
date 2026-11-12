# Santa Clara County Automatic Recount Watch

A small static site listing Santa Clara County contests that are inside, or close to, the automatic recount margin.

Live site: https://rkudeshi.github.io/SCC-Recount-Watch/

## How it works

- Clarity returns empty responses to GitHub-hosted runners, so a scheduled Claude task runs `python update.py --record-check --once-per-day` every 30 minutes from 4 PM to 11:30 PM Pacific (`--once-per-day` stops it for the day once new results are loaded) and pushes `data/` and `site/` to `main`. It reads Clarity's `current_ver.txt` for election 127570; if the version is new it downloads `reports/detailxml.zip` and recalculates margins. If the download fails, the last good data stays up.
- `.github/workflows/update.yml` deploys `site/` to GitHub Pages whenever `data/`, `update.py` or `config.json` change on `main`, or when run by hand.
- `update.py` does the fetching, parsing and page rendering. `python update.py --file detailxml.zip` builds from a local file.
- `config.json` holds the tier thresholds, whether to include ballot measures, and the list of contests to leave out.

## Margin rules

- Margin = votes for the last winner minus votes for the first loser (write-ins ignored).
- Ballots cast in a contest = (candidate votes + undervotes + overvotes) ÷ seats.
- Official percentage = margin ÷ ballots cast. The results-site percentage = margin ÷ candidate votes.
- Ballot measures: margin = extra votes needed to flip the outcome against its threshold (majority, 55% or two-thirds). For a majority measure this equals Yes minus No.
- Recount line for each contest: under 25 votes or under 0.25% of ballots cast, whichever is larger.
- Groups, from the day-by-day history: in recount range now; was in range at an earlier update but is out now; getting closer (margin in votes fell at each of the last two updates and is down at least 25% from three updates ago); close but not narrowing (within `watch_ratio`, default 6, times the recount line otherwise). Percentages near the 0.25% line show four decimals.
- Each contest card has a sparkline of the margin in votes after each day's update, with the recount line dashed, and a sentence noting when it entered or left recount range and any lead changes.

## History

`data/history.json` keeps one compact snapshot per results update. Each new update is added automatically. `python update.py --backfill VERSION ...` adds older updates (the county does not list past version numbers, so they were found by checking each number). The page shows a one-sentence history and a bar chart of the margin for each contest that has more than one snapshot, plus a "No longer close" section.

## Leaving out contests

Statewide, congressional and legislative contests are left out by name pattern. Other contests that cross county lines go in `exclude_contests` in `config.json` (a case-insensitive part of the contest name). Pushing a change to `config.json` rebuilds the site.

## Tests

`python -m unittest discover tests`
