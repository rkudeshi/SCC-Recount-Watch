# SCC Recount Watch

A small static site listing Santa Clara County contests that are inside, or close to, the automatic recount margin.

Live site: https://rkudeshi.github.io/SCC-Recount-Watch/

## How it works

- `.github/workflows/update.yml` runs hourly (at :17 past the hour). It reads Clarity's `current_ver.txt` for election 127570. If the version is new, it downloads `reports/detailxml.zip`, recalculates margins, commits `data/` and `site/`, and deploys `site/` to GitHub Pages. If the download fails, the last good data stays up.
- `update.py` does the fetching, parsing and page rendering. `python update.py --file detailxml.zip` builds from a local file.
- `config.json` holds the tier thresholds, whether to include ballot measures, and the list of contests to leave out.

## Margin rules

- Margin = votes for the last winner minus votes for the first loser (write-ins ignored).
- Ballots cast in a contest = (candidate votes + undervotes + overvotes) ÷ seats.
- Official percentage = margin ÷ ballots cast. The results-site percentage = margin ÷ candidate votes.
- Ballot measures: margin = extra votes needed to flip the outcome against its threshold (majority, 55% or two-thirds). For a majority measure this equals Yes minus No.
- Tiers (set in `config.json`): automatic recount under 25 votes or under 0.25%; very close within 100 votes or 0.5%; keep an eye on within 200 votes or 1.5%. Percentages near a tier line show four decimals.

## Leaving out contests

Statewide, congressional and legislative contests are left out by name pattern. Other contests that cross county lines go in `exclude_contests` in `config.json` (a case-insensitive part of the contest name). Pushing a change to `config.json` rebuilds the site.

## Tests

`python -m unittest discover tests`
