#!/usr/bin/env python3
"""SCC Recount Watch: fetch Santa Clara County results from Clarity, find close
contests, and write the static site to site/.

Usage:
  python update.py                 # check Clarity; rebuild only if the version changed
  python update.py --force         # rebuild even if the version is unchanged
  python update.py --file detailxml.zip --version 386706   # build from a local file
  python update.py --render-only   # re-render site/ from data/results.json
"""
import argparse
import html
import io
import json
import math
import os
import re
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")
SITE_DIR = os.path.join(ROOT, "site")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
RESULTS_PATH = os.path.join(DATA_DIR, "results.json")
PACIFIC = ZoneInfo("America/Los_Angeles")
USER_AGENT = "SCC-Recount-Watch/1.0 (+https://github.com/rkudeshi/SCC-Recount-Watch)"
WRITE_IN = re.compile(r"^write[- ]?ins?$", re.I)


# ---------------------------------------------------------------- fetching

def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def current_version(base):
    text = http_get(f"{base}/current_ver.txt").decode("utf-8-sig").strip()
    if not text.isdigit():
        raise ValueError(f"unexpected current_ver.txt contents: {text[:80]!r}")
    return text


def read_detail_xml(blob):
    """Accept either a zip containing detail.xml or the raw XML bytes."""
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            return z.read(name)
    return blob


# ---------------------------------------------------------------- parsing

def clean_name(s):
    return (s or "").replace('""', '"').strip()


def measure_threshold(name):
    n = name.lower()
    if "2/3" in n or "66.67" in n or "two-thirds" in n:
        return 2 / 3, "two-thirds"
    if "55%" in n:
        return 0.55, "55%"
    return 0.5, "majority"


def parse_detail(xml_bytes):
    root = ET.fromstring(xml_bytes)
    meta = {
        "election_name": (root.findtext("ElectionName") or "").strip(),
        "results_timestamp": (root.findtext("Timestamp") or "").strip(),
    }
    contests = []
    for c in root.findall("Contest"):
        under = over = 0
        for vt in c.findall("VoteType"):
            if vt.get("name") == "Undervotes":
                under = int(vt.get("votes") or 0)
            elif vt.get("name") == "Overvotes":
                over = int(vt.get("votes") or 0)
        choices = [
            {"name": clean_name(ch.get("text")), "party": ch.get("party") or "",
             "votes": int(ch.get("totalVotes") or 0)}
            for ch in c.findall("Choice")
        ]
        contests.append({
            "key": c.get("key"),
            "name": clean_name(c.get("text")),
            "vote_for": int(c.get("voteFor") or 0),
            "is_question": c.get("isQuestion") == "true",
            "precincts_reporting": c.get("precinctsReporting"),
            "precincts_total": c.get("precinctsReported"),
            "undervotes": under,
            "overvotes": over,
            "choices": choices,
        })
    return meta, contests


# ---------------------------------------------------------------- analysis

def excluded_reason(name, cfg):
    for pat in cfg["exclude_name_patterns"]:
        if re.search(pat, name, re.I):
            return "statewide, congressional or legislative"
    for sub in cfg["exclude_contests"]:
        if sub.lower() in name.lower():
            return "crosses county lines (config list)"
    return None


def is_measure(c):
    names = {ch["name"].upper() for ch in c["choices"]}
    return c["is_question"] or c["vote_for"] == 0 or names <= {"YES", "NO", "BONDS YES", "BONDS NO"}


def tier_for(margin, pct, t):
    # Automatic recount is strictly less than the threshold; the other tiers are "within".
    if margin < t["recount_votes"] or pct < t["recount_pct"]:
        return 1
    if margin <= t["very_close_votes"] or pct <= t["very_close_pct"]:
        return 2
    if margin <= t["watch_votes"] or pct <= t["watch_pct"]:
        return 3
    return 0


def analyze_candidate(c, t):
    n = max(c["vote_for"], 1)
    all_votes = sum(ch["votes"] for ch in c["choices"])
    ballots = round((all_votes + c["undervotes"] + c["overvotes"]) / n)
    ranked = sorted([ch for ch in c["choices"] if not WRITE_IN.match(ch["name"])],
                    key=lambda ch: -ch["votes"])
    write_in = [ch for ch in c["choices"] if WRITE_IN.match(ch["name"])]
    if len(ranked) <= n:
        return None  # nobody loses
    margin = ranked[n - 1]["votes"] - ranked[n]["votes"]
    pct = 100 * margin / ballots if ballots else 0
    site_pct = 100 * margin / all_votes if all_votes else 0
    rows = [{"name": ch["name"], "votes": ch["votes"], "winner": i < n} for i, ch in enumerate(ranked)]
    rows += [{"name": "Write-in", "votes": ch["votes"], "winner": False, "write_in": True} for ch in write_in]
    for r in rows:
        r["pct_ballots"] = 100 * r["votes"] / ballots if ballots else 0
        r["pct_votes"] = 100 * r["votes"] / all_votes if all_votes else 0
    return {
        "type": "candidate", "name": c["name"], "vote_for": n,
        "rows": rows, "candidate_votes": all_votes,
        "undervotes": c["undervotes"], "overvotes": c["overvotes"], "ballots_cast": ballots,
        "margin": margin, "pct": pct, "site_pct": site_pct,
        "tier": tier_for(margin, pct, t),
        "last_winner": ranked[n - 1]["name"], "first_loser": ranked[n]["name"],
    }


def analyze_measure(c, t):
    votes = {ch["name"].upper().replace("BONDS ", ""): ch["votes"] for ch in c["choices"]}
    yes, no = votes.get("YES", 0), votes.get("NO", 0)
    total = yes + no
    if not total:
        return None
    thr, thr_label = measure_threshold(c["name"])
    share = yes / total
    passing = share > thr if thr == 0.5 else share >= thr
    # Votes that would flip the outcome, counted the same way as a candidate
    # margin: extra NO votes needed to sink it, or extra YES votes needed to pass it.
    if passing:
        margin = (yes - thr * total) / thr
    else:
        margin = (thr * total - yes) / (1 - thr)
    margin = round(margin + 1e-9)
    ballots = yes + no + c["undervotes"] + c["overvotes"]
    pct = 100 * margin / ballots if ballots else 0
    rows = [{"name": "Yes", "votes": yes, "winner": passing},
            {"name": "No", "votes": no, "winner": not passing}]
    for r in rows:
        r["pct_ballots"] = 100 * r["votes"] / ballots if ballots else 0
        r["pct_votes"] = 100 * r["votes"] / total
    return {
        "type": "measure", "name": c["name"], "threshold": thr, "threshold_label": thr_label,
        "passing": passing, "yes_share": 100 * share,
        "rows": rows, "candidate_votes": total,
        "undervotes": c["undervotes"], "overvotes": c["overvotes"], "ballots_cast": ballots,
        "margin": margin, "pct": pct, "site_pct": 100 * share - 100 * thr,
        "tier": tier_for(margin, pct, t),
    }


def analyze(meta, contests, cfg):
    t = cfg["tiers"]
    included, excluded, skipped = [], [], []
    for c in contests:
        reason = excluded_reason(c["name"], cfg)
        if reason:
            excluded.append({"name": c["name"], "reason": reason})
            continue
        if is_measure(c):
            if not cfg.get("include_ballot_measures", True):
                excluded.append({"name": c["name"], "reason": "ballot measure"})
                continue
            a = analyze_measure(c, t)
        else:
            a = analyze_candidate(c, t)
        if a is None:
            skipped.append({"name": c["name"], "reason": "uncontested"})
            continue
        a["key"] = c["key"]
        a["precincts_reporting"] = c["precincts_reporting"]
        a["precincts_total"] = c["precincts_total"]
        included.append(a)
    included.sort(key=lambda a: (a["tier"] or 9, a["pct"], a["margin"]))
    return {**meta, "contests": included, "excluded": excluded, "skipped": skipped}


# ---------------------------------------------------------------- rendering

def tier_info(t):
    return {
        1: ("Automatic recount", f"Margin under {t['recount_votes']} votes or under {t['recount_pct']:g}% of ballots cast"),
        2: ("Very close", f"Within {t['very_close_votes']} votes or {t['very_close_pct']:g}% of ballots cast"),
        3: ("Keep an eye on", f"Within {t['watch_votes']} votes or {t['watch_pct']:g}% of ballots cast"),
    }


TIER_INFO = {}
PCT_LINES = []

E = html.escape


def fmt_n(n):
    return f"{n:,}"


def fmt_pct(p, d=2):
    return f"{p:.{d}f}%"


def fmt_margin_pct(p):
    """Two decimals, but four when the value sits near a tier line (e.g. 0.2489%)."""
    if any(abs(p - line) < 0.01 for line in PCT_LINES):
        return f"{p:.4f}%"
    return f"{p:.2f}%"


def fmt_time(dt):
    return dt.strftime("%b %-d, %Y, %-I:%M %p %Z")


def fmt_short_time(dt):
    # e.g. "TUE, Nov 10, 5:06 PM PST"
    return dt.strftime("%a").upper() + dt.strftime(", %b %-d, %-I:%M %p %Z")


def parse_clarity_time(s):
    # e.g. "11/10/2026 5:06:37 PM PST"
    try:
        dt = datetime.strptime(" ".join(s.split()[:3]), "%m/%d/%Y %I:%M:%S %p")
        return dt.replace(tzinfo=PACIFIC)
    except ValueError:
        return None


def render_contest(a):
    tier = a["tier"]
    cls = f"contest t{tier}" if tier else "contest"
    if a["type"] == "candidate":
        sub = f"Vote for {a['vote_for']}"
        margin_line = (f"Margin: {fmt_n(a['margin'])} votes, {fmt_margin_pct(a['pct'])} of ballots cast")
        compare = f"({fmt_pct(a['site_pct'])} of candidate votes only)"
        col_label, pv_label, total_label = "Candidate", "% of candidate votes", "Candidate votes"
    else:
        sub = f"Needs {a['threshold_label']} Yes · currently {'passing' if a['passing'] else 'failing'}"
        side = "No" if a["passing"] else "Yes"
        margin_line = (f"Margin: {fmt_n(a['margin'])} more {side} votes would flip it, "
                       f"{fmt_margin_pct(a['pct'])} of ballots cast")
        compare = (f"(Yes at {fmt_pct(a['yes_share'])} of votes, needs "
                   f"{fmt_pct(100 * a['threshold'])})")
        col_label, pv_label, total_label = "Choice", "% of votes", "Total votes"

    rows = []
    n_rows = [r for r in a["rows"] if not r.get("write_in")]
    for i, r in enumerate(a["rows"]):
        classes = []
        if a["type"] == "candidate" and r["winner"] and (i + 1 < len(n_rows)) and not n_rows[i + 1]["winner"]:
            classes.append("cut")
        if r.get("write_in"):
            classes.append("wi")
        name = E(r["name"]) + (" ✓" if a["type"] == "measure" and r["winner"] else "")
        rows.append(
            f'<tr class="{" ".join(classes)}"><td>{name}</td><td>{fmt_n(r["votes"])}</td>'
            f'<td>{fmt_pct(r["pct_ballots"])}</td><td>{fmt_pct(r["pct_votes"])}</td></tr>')
    totals = [
        (total_label, fmt_n(a["candidate_votes"])),
        ("Undervotes", fmt_n(a["undervotes"])),
        ("Overvotes", fmt_n(a["overvotes"])),
        ("Ballots cast", fmt_n(a["ballots_cast"])),
    ]
    for j, (k, v) in enumerate(totals):
        rows.append(f'<tr class="tot{" first" if j == 0 else ""}"><td>{k}</td><td>{v}</td><td></td><td></td></tr>')

    return f"""<div class="{cls}" id="c{E(str(a['key']))}">
<h3>{E(a['name'])}</h3>
<p class="muted">{E(sub)}</p>
<div class="margin"><span class="big">{E(margin_line)}</span><br><span class="cmp">{E(compare)}</span></div>
<div class="tbl"><table>
<tr><th>{col_label}</th><th>Votes</th><th>% of ballots cast</th><th>{pv_label}</th></tr>
{''.join(rows)}
</table></div>
</div>"""


def render(results, state, cfg):
    t = cfg["tiers"]
    TIER_INFO.update(tier_info(t))
    PCT_LINES[:] = [t["recount_pct"], t["very_close_pct"], t["watch_pct"]]
    # Re-tier from saved numbers so a config change applies without a new download.
    contests = results["contests"]
    for a in contests:
        a["tier"] = tier_for(a["margin"], a["pct"], t)
    contests.sort(key=lambda a: (a["tier"] or 9, a["pct"], a["margin"]))
    counts = {k: sum(1 for a in contests if a["tier"] == k) for k in (1, 2, 3)}
    res_time = parse_clarity_time(results.get("results_timestamp", ""))
    res_time_s = (f'<span title="{fmt_time(res_time)}">{fmt_short_time(res_time)}</span>'
                  if res_time else E(results.get("results_timestamp", "")))
    checked = datetime.fromisoformat(state["last_checked"]).astimezone(PACIFIC)
    repo = os.environ.get("GITHUB_REPOSITORY", "rkudeshi/SCC-Recount-Watch")

    tiles = "".join(
        f'<a class="tile t{k}" href="#tier{k}"><b>{counts[k]}</b><span>{TIER_INFO[k][0]}</span></a>'
        for k in (1, 2, 3))

    recap_items = "".join(
        f'<li><a href="#c{E(str(a["key"]))}">{E(a["name"])}</a>: {fmt_n(a["margin"])} votes / {fmt_margin_pct(a["pct"])}</li>'
        for a in contests if a["tier"] == 1)
    recap = f"<ul>{recap_items}</ul>" if recap_items else "<p>None right now.</p>"

    sections = []
    for k in (1, 2, 3):
        items = [a for a in contests if a["tier"] == k]
        body = "".join(render_contest(a) for a in items) or '<p class="none">No contests in this group right now.</p>'
        sections.append(f"""<section id="tier{k}">
<h2 class="sec t{k}">{TIER_INFO[k][0]} <small>{TIER_INFO[k][1]}</small></h2>
{body}
</section>""")

    others = [a for a in contests if a["tier"] == 0]
    others.sort(key=lambda a: a["pct"])
    other_rows = "".join(
        f'<tr><td>{E(a["name"])}</td><td>{fmt_n(a["margin"])}</td><td>{fmt_margin_pct(a["pct"])}</td></tr>'
        for a in others)
    excl = "".join(f"<li>{E(x['name'])} <span class='muted'>({E(x['reason'])})</span></li>"
                   for x in results["excluded"] + results["skipped"])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCC Recount Watch</title>
<style>
:root{{--bg:#f6f6f6;--box:#fff;--fg:#111;--muted:#666;--line:#ddd;--cut:#111;--link:#0645ad;
  --t1:#f8d4d2;--t1s:#b52a24;--t2:#fde7bd;--t2s:#9a5d00;--t3:#dbe6f3;--t3s:#2f5d8f}}
@media (prefers-color-scheme: dark){{:root{{--bg:#111;--box:#1b1b1b;--fg:#eee;--muted:#9a9a9a;--line:#333;--cut:#eee;--link:#8ab4f8;
  --t1:#5a2321;--t1s:#ff9b95;--t2:#4d3a15;--t2s:#f2c06a;--t3:#203246;--t3s:#a9c6e8;color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:15px;line-height:1.45;padding:16px}}
main{{max-width:700px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}}
p{{margin:4px 0}}
.muted{{color:var(--muted);font-size:13px}}
.times{{background:var(--box);border:2px solid var(--fg);border-radius:4px;padding:10px 12px;margin:12px 0;display:grid;gap:4px}}
.times div{{font-size:17px}}
.times b{{font-size:17px}}
.tiles{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}}
.tile{{border-radius:4px;padding:10px;color:var(--fg);text-decoration:none;display:block}}
.tile b{{display:block;font-size:30px;line-height:1.1}}
.tile span{{font-size:14px;font-weight:600}}
.tile.t1{{background:var(--t1)}} .tile.t2{{background:var(--t2)}} .tile.t3{{background:var(--t3)}}
h2.sec{{font-size:22px;margin:36px 0 12px;padding:10px 12px;border-radius:4px;border-bottom:4px solid}}
h2.sec small{{display:block;font-size:14px;font-weight:normal}}
h2.t1{{background:var(--t1);border-color:var(--t1s)}} h2.t2{{background:var(--t2);border-color:var(--t2s)}} h2.t3{{background:var(--t3);border-color:var(--t3s)}}
h3{{font-size:17px;margin:0}}
.contest{{background:var(--box);border:1px solid var(--line);border-radius:4px;padding:12px;margin-bottom:12px}}
.margin{{margin:6px 0 8px;padding:5px 8px;border-radius:4px}}
.contest.t1 .margin{{background:var(--t1)}} .contest.t2 .margin{{background:var(--t2)}} .contest.t3 .margin{{background:var(--t3)}}
.margin .big{{font-size:15px;font-weight:600}}
.margin .cmp{{font-size:13px;color:var(--muted)}}
.recap{{background:var(--t1);border-radius:4px;padding:10px 12px;margin:12px 0}}
.recap ul{{margin:4px 0 0;padding-left:20px}}
.recap li{{margin:2px 0}}
.recap p{{margin:4px 0 0}}
.tbl{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right}}
th:first-child,td:first-child{{text-align:left}}
th{{font-size:12px;color:var(--muted);font-weight:normal;vertical-align:bottom}}
tr.cut td{{border-bottom:3px solid var(--cut)}}
tr.wi td{{color:var(--muted)}}
tr.tot td{{color:var(--muted);font-size:13px;border-bottom:0}}
tr.first td{{border-top:1px solid var(--muted)}}
.none{{color:var(--muted)}}
details{{margin-top:24px}}
summary{{cursor:pointer;font-weight:600}}
a{{color:var(--link)}}
@media (max-width:440px){{body{{padding:12px}}.tile b{{font-size:24px}}.tile span{{font-size:12px}}.margin .big{{font-size:14px}}th,td{{padding:4px 3px}}}}
</style>
</head>
<body>
<main>
<h1>SCC Recount Watch</h1>
<p>Santa Clara County contests at or near the automatic recount margin. {E(results.get('election_name',''))}.</p>

<div class="times">
<div>Results as of: <b>{res_time_s}</b> <span class="muted">(update {E(str(state['version']))})</span></div>
<div>Last checked: <b id="checked" data-ts="{state['last_checked']}" title="{fmt_time(checked)}">{fmt_time(checked)}</b></div>
</div>

<div class="tiles">{tiles}</div>

<div class="recap">
<b>Automatic recount:</b>
{recap}
</div>

<p class="muted">Margin = last winner minus first loser. The official percentage divides by all ballots cast in the contest's area, including people who skipped the contest (votes + undervotes + overvotes, divided by seats). The county results site divides by candidate votes only, so its percentage is usually larger. In vote-for-2 or vote-for-3 contests it can be smaller.</p>

{''.join(sections)}

<details>
<summary>Other county contests ({len(others)}, not close)</summary>
<div class="tbl"><table>
<tr><th>Contest</th><th>Margin</th><th>% of ballots cast</th></tr>
{other_rows}
</table></div>
</details>

<details>
<summary>Contests not included ({len(results['excluded']) + len(results['skipped'])})</summary>
<ul>{excl}</ul>
</details>

<p class="muted" style="margin-top:24px">Source: <a href="{E(state['source_url'])}">Santa Clara County official results</a>. Checked about once an hour. Unofficial tracker; margins change as the county counts more ballots.</p>
</main>
<script>
// Show "last checked" as time ago (exact time on hover), using the most recent
// hourly run even when nothing new was published.
(function(){{
  var el=document.getElementById('checked');
  var when=new Date(el.dataset.ts);
  function ago(){{
    var m=Math.max(0,Math.round((Date.now()-when)/60000));
    var s=m<1?'just now':m<60?m+(m==1?' minute':' minutes')+' ago':
      (function(h){{return h<48?h+(h==1?' hour':' hours')+' ago':Math.round(h/24)+' days ago'}})(Math.round(m/60));
    el.textContent=s;
    el.title=when.toLocaleString('en-US',{{timeZone:'America/Los_Angeles',weekday:'short',month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}});
  }}
  ago(); setInterval(ago,60000);
  fetch('https://api.github.com/repos/{repo}/actions/workflows/update.yml/runs?per_page=1&status=completed')
    .then(function(r){{return r.ok?r.json():null}})
    .then(function(j){{
      if(!j||!j.workflow_runs||!j.workflow_runs.length)return;
      var t=new Date(j.workflow_runs[0].updated_at);
      if(t>when){{when=t;ago();}}
    }}).catch(function(){{}});
}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- main

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")


def write_site(results, state, cfg):
    os.makedirs(SITE_DIR, exist_ok=True)
    with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
        f.write(render(results, state, cfg))
    write_json(os.path.join(SITE_DIR, "results.json"), {"state": state, **results})


def set_output(changed):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
    print(f"changed={changed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--version")
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = cfg["election_url"].rstrip("/")

    if args.render_only:
        results = load_json(RESULTS_PATH, None)
        if not results:
            sys.exit("no data/results.json to render")
        write_site(results, state, cfg)
        set_output(True)
        return

    try:
        if args.file:
            version = args.version or "local"
            with open(args.file, "rb") as f:
                blob = f.read()
        else:
            version = current_version(base)
            if version == state.get("version") and not args.force:
                print(f"version {version} unchanged; nothing to do")
                set_output(False)
                return
            blob = http_get(f"{base}/{version}/{cfg['report_path']}", timeout=180)
        meta, contests = parse_detail(read_detail_xml(blob))
        if not contests:
            raise ValueError("no contests found in report")
        results = analyze(meta, contests, cfg)
    except Exception as e:  # keep the last good data
        print(f"::warning::update failed, keeping last good data: {e}")
        set_output(False)
        return

    state = {
        "version": version,
        "last_checked": now,
        "source_url": f"{base}/",
    }
    write_json(RESULTS_PATH, results)
    write_json(STATE_PATH, state)
    write_site(results, state, cfg)
    counts = {k: sum(1 for a in results["contests"] if a["tier"] == k) for k in (1, 2, 3)}
    print(f"version {version}: {len(results['contests'])} contests, tiers {counts}")
    set_output(True)


if __name__ == "__main__":
    main()
