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
import gzip
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
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
PACIFIC = ZoneInfo("America/Los_Angeles")
USER_AGENT = "SCC-Recount-Watch/1.0 (+https://github.com/rkudeshi/SCC-Recount-Watch)"
WRITE_IN = re.compile(r"^write[- ]?ins?$", re.I)


# ---------------------------------------------------------------- fetching

def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return body


def current_version(base):
    """Find the newest results version. Clarity's current_ver.txt is the usual
    source; fall back to other spellings and to version numbers in the landing page."""
    root = base.split("/CA/")[0]
    path = base[len(root):]
    candidates = [
        f"{base}/current_ver.txt",
        f"{root}/{path}/current_ver.txt",  # the double-slash form Clarity links use
        f"{base}/current_ver.txt?nocache={int(datetime.now().timestamp())}",
    ]
    for url in candidates:
        try:
            text = http_get(url).decode("utf-8-sig", "replace").strip()
            print(f"  {url} -> {text[:60]!r}")
            if text.isdigit():
                return text
        except Exception as e:  # noqa: BLE001
            print(f"  {url} -> error {e}")
    for url in (f"{base}/", f"{base}/index.html"):
        try:
            page = http_get(url).decode("utf-8", "replace")
            print(f"  {url} -> {len(page)} bytes: {page[:200]!r}")
            found = re.findall(re.escape(path.rsplit("/", 1)[-1]) + r"/(\d{5,7})/", page) + re.findall(r"(?:ver|version)\D{0,5}(\d{5,7})", page, re.I)
            if found:
                return max(found, key=int)
        except Exception as e:  # noqa: BLE001
            print(f"  {url} -> error {e}")
    raise ValueError("could not find the current results version")


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
    """Two decimals, but four near the automatic recount line (e.g. 0.2489%)."""
    if any(abs(p - line) < 0.01 for line in PCT_LINES):
        return f"{p:.4f}%"
    return f"{p:.2f}%"


def fmt_time(dt):
    return dt.strftime("%b %-d, %Y, %-I:%M %p %Z")


def fmt_short_time(dt):
    # e.g. "Tue Nov 10, 5:06 PM PST"
    return dt.strftime("%a %b %-d, %-I:%M %p %Z")


def parse_clarity_time(s):
    # e.g. "11/10/2026 5:06:37 PM PST"
    try:
        dt = datetime.strptime(" ".join(s.split()[:3]), "%m/%d/%Y %I:%M:%S %p")
        return dt.replace(tzinfo=PACIFIC)
    except ValueError:
        return None


# ---------------------------------------------------------------- history

def snapshot_of(results, version):
    """Compact per-contest record of one results update."""
    snap = {"version": str(version), "results_timestamp": results.get("results_timestamp", ""), "contests": {}}
    for a in results["contests"]:
        rec = {"name": a["name"], "margin": a["margin"], "pct": round(a["pct"], 6), "ballots": a["ballots_cast"]}
        if a["type"] == "candidate":
            rec["winner"], rec["loser"] = a["last_winner"], a["first_loser"]
        else:
            rec["passing"] = a["passing"]
        snap["contests"][str(a["key"])] = rec
    return snap


def add_snapshot(hist, snap):
    snaps = [s for s in hist.get("snapshots", []) if s["version"] != snap["version"]]
    snaps.append(snap)
    snaps.sort(key=lambda s: parse_clarity_time(s["results_timestamp"]) or datetime.min.replace(tzinfo=PACIFIC))
    hist["snapshots"] = snaps
    return hist


TIER_WORDS = {1: "automatic recount", 2: "very close", 3: "watch list", 0: "not close"}


def contest_points(hist, key, t):
    pts = []
    for snap in hist.get("snapshots", []):
        rec = snap["contests"].get(str(key))
        if not rec:
            continue
        dt = parse_clarity_time(snap["results_timestamp"])
        pts.append({**rec, "tier": tier_for(rec["margin"], rec["pct"], t), "dt": dt,
                    "label": dt.strftime("%b %-d") if dt else snap["version"]})
    # Same-day updates: add the time so labels stay distinct.
    for i, p in enumerate(pts):
        if any(q is not p and q["label"] == p["label"] for q in pts) and p["dt"]:
            p["label"] = p["dt"].strftime("%b %-d %-I%p").replace("AM", "a").replace("PM", "p")
    return pts


def mv(p):
    return f"{fmt_n(p['margin'])} vote{'' if p['margin'] == 1 else 's'}, {fmt_margin_pct(p['pct'])}"


def history_sentence(pts):
    if len(pts) < 2:
        return ""
    segs = []
    for p in pts:
        if segs and segs[-1]["tier"] == p["tier"]:
            segs[-1]["last"] = p
        else:
            segs.append({"tier": p["tier"], "first": p, "last": p})
    first, last = pts[0], pts[-1]
    if len(segs) == 1:
        word = TIER_WORDS[segs[0]["tier"]].capitalize()
        out = f"{word} since {first['label']}; margin went from {mv(first)} to {mv(last)}."
    else:
        parts = []
        for i, sg in enumerate(segs[-3:]):
            word = TIER_WORDS[sg["tier"]]
            if i == 0:
                parts.append(f"{word.capitalize()} on {sg['first']['label']} ({mv(sg['first'])})")
            else:
                parts.append(f"{word} by {sg['first']['label']} ({mv(sg['first'])})")
        out = ", then ".join(parts) + "."
    # Note changes in who holds the last winning spot (or a measure flipping); keep the last two.
    notes = []
    for prev, cur in zip(pts, pts[1:]):
        if "winner" in cur and cur.get("winner") != prev.get("winner") and prev.get("winner"):
            notes.append(f" {cur['winner']} passed {prev['winner']} for the last winning spot by {cur['label']}.")
        if "passing" in cur and cur["passing"] != prev.get("passing"):
            notes.append(f" It flipped to {'passing' if cur['passing'] else 'failing'} by {cur['label']}.")
    return out + "".join(notes[-2:])


def history_chart(pts, t):
    """Small bar chart of the official margin % for each update, with the 0.25% line."""
    if len(pts) < 2:
        return ""
    n = len(pts)
    W, H, top, bottom = 300, 74, 14, 16
    # Cap the scale so a large early margin doesn't flatten the recent, close ones.
    vmax = max(min(max(p["pct"] for p in pts), t["watch_pct"] * 1.4), t["recount_pct"] * 1.3)
    slot = W / n
    bw = min(34, slot * 0.6)
    ph = H - top - bottom
    y = lambda v: top + ph - ph * min(v, vmax) / vmax
    out = [f'<svg class="hist" viewBox="0 0 {W} {H}" role="img" aria-label="Margin at each update">']
    yl = y(t["recount_pct"])
    out.append(f'<line class="rl" x1="0" x2="{W}" y1="{yl:.1f}" y2="{yl:.1f}"/>')
    step = max(1, math.ceil(n / 8))
    for i, p in enumerate(pts):
        cx = slot * i + slot / 2
        yy = y(p["pct"])
        out.append(f'<rect class="b{p["tier"]}" x="{cx - bw / 2:.1f}" y="{yy:.1f}" width="{bw:.1f}" '
                   f'height="{max(top + ph - yy, 1):.1f}"><title>{E(p["label"])}: {E(mv(p))}</title></rect>')
        if n <= 8 or i % step == 0 or i == n - 1:
            out.append(f'<text class="bv" x="{cx:.1f}" y="{yy - 3:.1f}" text-anchor="middle">{fmt_margin_pct(p["pct"]).rstrip("%")}</text>')
            out.append(f'<text class="bl" x="{cx:.1f}" y="{H - 4}" text-anchor="middle">{E(p["label"])}</text>')
    out.append("</svg>")
    return "".join(out)


def history_block(pts, t):
    if len(pts) < 2:
        return ""
    return (f'<div class="history"><p><b>History:</b> {E(history_sentence(pts))}</p>'
            f'{history_chart(pts, t)}<p class="muted cap">Margin as % of ballots cast at each day\'s update. Dashed line: {t["recount_pct"]:g}% automatic recount line.</p></div>')


def render_contest(a, hist_html=""):
    tier = a["tier"]
    cls = f"contest t{tier}" if tier else "contest"
    np_ = int(a.get("precincts_total") or a.get("precincts_reporting") or 0)
    precincts = f"{fmt_n(np_)} precinct{'' if np_ == 1 else 's'}"
    if a["type"] == "candidate":
        sub = f"Vote for {a['vote_for']} · {precincts}"
        margin_line = (f"Margin: {fmt_n(a['margin'])} votes, {fmt_margin_pct(a['pct'])} of ballots cast")
        compare = f"({fmt_pct(a['site_pct'])} of candidate votes only)"
        col_label, pv_label, total_label = "Candidate", "% of candidate votes", "Candidate votes"
    else:
        sub = f"Needs {a['threshold_label']} Yes · currently {'passing' if a['passing'] else 'failing'} · {precincts}"
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
        ("Overvotes", fmt_n(a["overvotes"])),
        ("Undervotes", fmt_n(a["undervotes"])),
        ("Ballots cast", fmt_n(a["ballots_cast"])),
    ]
    for j, (k, v) in enumerate(totals):
        rows.append(f'<tr class="tot{" first" if j == 0 else ""}"><td>{k}</td><td>{v}</td><td></td><td></td></tr>')

    return f"""<div class="{cls}" id="c{E(str(a['key']))}">
<h3>{E(a['name'])}</h3>
<p class="muted">{E(sub)}</p>
<div class="margin"><span class="big">{E(margin_line)}</span><br><span class="cmp">{E(compare)}</span></div>
{hist_html}
<div class="tbl"><table>
<tr><th>{col_label}</th><th>Votes</th><th>% of ballots cast</th><th>{pv_label}</th></tr>
{''.join(rows)}
</table></div>
</div>"""


def short_name(a):
    # "Measure Q - Cambrian School District, Parcel Tax - 2/3rds ..." -> "Measure Q - Cambrian School District"
    if a["type"] == "measure":
        return " - ".join(a["name"].split(" - ")[:2]).split(",")[0]
    return a["name"]


def render(results, state, cfg, hist=None):
    t = cfg["tiers"]
    TIER_INFO.update(tier_info(t))
    PCT_LINES[:] = [t["recount_pct"]]
    # Re-tier from saved numbers so a config change applies without a new download.
    contests = results["contests"]
    for a in contests:
        a["tier"] = tier_for(a["margin"], a["pct"], t)
    contests.sort(key=lambda a: (a["tier"] or 9, a["pct"], a["margin"]))
    counts = {k: sum(1 for a in contests if a["tier"] == k) for k in (1, 2, 3)}
    hist = hist or {}
    pts_by_key = {str(a["key"]): contest_points(hist, a["key"], t) for a in contests}
    hblock = lambda a: history_block(pts_by_key[str(a["key"])], t)
    # Contests that were in a group at some earlier update but are not close now.
    dropped = [a for a in contests if a["tier"] == 0
               and any(p["tier"] in (1, 2) for p in pts_by_key[str(a["key"])][:-1])]
    res_time = parse_clarity_time(results.get("results_timestamp", ""))
    res_time_s = (f'<span title="{fmt_time(res_time)}">{fmt_short_time(res_time)}</span>'
                  if res_time else E(results.get("results_timestamp", "")))
    checked = datetime.fromisoformat(state["last_checked"]).astimezone(PACIFIC)
    repo = os.environ.get("GITHUB_REPOSITORY", "rkudeshi/SCC-Recount-Watch")

    tiles = "".join(
        f'<a class="tile t{k}" href="#tier{k}"><b>{counts[k]}</b><span>{TIER_INFO[k][0]}</span></a>'
        for k in (1, 2, 3))

    recap_parts = []
    for k in (1, 2, 3):
        items = "".join(
            f'<li><a href="#c{E(str(a["key"]))}">{E(short_name(a))}</a>: {fmt_n(a["margin"])} votes / {fmt_margin_pct(a["pct"])}</li>'
            for a in contests if a["tier"] == k)
        body = f"<ul>{items}</ul>" if items else '<p class="muted">None right now.</p>'
        recap_parts.append(f'<h4 class="rh t{k}">{TIER_INFO[k][0]} ({counts[k]})</h4>{body}')
    if dropped:
        items = "".join(
            f'<li><a href="#c{E(str(a["key"]))}">{E(short_name(a))}</a>: now {fmt_n(a["margin"])} votes / {fmt_margin_pct(a["pct"])}</li>'
            for a in dropped)
        recap_parts.append(f'<h4 class="rh t0">No longer close ({len(dropped)})</h4><ul>{items}</ul>')
    recap = "".join(recap_parts)

    sections = []
    for k in (1, 2, 3):
        items = [a for a in contests if a["tier"] == k]
        body = "".join(render_contest(a, hblock(a)) for a in items) or '<p class="none">No contests in this group right now.</p>'
        sections.append(f"""<section id="tier{k}">
<h2 class="sec t{k}">{TIER_INFO[k][0]} <small>{TIER_INFO[k][1]}</small></h2>
{body}
</section>""")

    if dropped:
        sections.append(f"""<section id="dropped">
<h2 class="sec t0">No longer close <small>Were an automatic recount or very close at an earlier update</small></h2>
{"".join(render_contest(a, hblock(a)) for a in dropped)}
</section>""")

    others = [a for a in contests if a["tier"] == 0 and a not in dropped]
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
<title>Santa Clara County Automatic Recount Watch</title>
<style>
:root{{--bg:#f6f6f6;--box:#fff;--fg:#111;--muted:#666;--line:#ddd;--cut:#111;--link:#0645ad;
  --t1:#f8d4d2;--t1s:#b52a24;--t2:#fde7bd;--t2s:#9a5d00;--t3:#dbe6f3;--t3s:#2f5d8f;--t0:#e6e6e6;--t0s:#8a8a8a}}
@media (prefers-color-scheme: dark){{:root{{--bg:#111;--box:#1b1b1b;--fg:#eee;--muted:#9a9a9a;--line:#333;--cut:#eee;--link:#8ab4f8;
  --t1:#5a2321;--t1s:#ff9b95;--t2:#4d3a15;--t2s:#f2c06a;--t3:#203246;--t3s:#a9c6e8;--t0:#2a2a2a;--t0s:#9a9a9a;color-scheme:dark}}}}
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
h2.t0{{background:var(--t0);border-color:var(--t0s)}} h2.t1{{background:var(--t1);border-color:var(--t1s)}} h2.t2{{background:var(--t2);border-color:var(--t2s)}} h2.t3{{background:var(--t3);border-color:var(--t3s)}}
h3{{font-size:17px;margin:0}}
.contest{{background:var(--box);border:1px solid var(--line);border-radius:4px;padding:12px;margin-bottom:12px}}
.margin{{margin:6px 0 8px;padding:5px 8px;border-radius:4px}}
.contest.t1 .margin{{background:var(--t1)}} .contest.t2 .margin{{background:var(--t2)}} .contest.t3 .margin{{background:var(--t3)}}
.margin .big{{font-size:15px;font-weight:600}}
.margin .cmp{{font-size:13px;color:var(--muted)}}
.recap{{background:var(--box);border:1px solid var(--line);border-radius:4px;padding:10px 12px;margin:12px 0}}
.recap h2{{font-size:18px;margin:0 0 4px}}
.recap h4{{font-size:15px;margin:10px 0 0;padding:2px 6px;border-radius:3px;display:inline-block}}
.rh.t0{{background:var(--t0)}} .rh.t1{{background:var(--t1)}} .rh.t2{{background:var(--t2)}} .rh.t3{{background:var(--t3)}}
.recap ul{{margin:4px 0 0;padding-left:20px}}
.recap li{{margin:2px 0}}
.recap p{{margin:4px 0 0}}
.tbl{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right}}
th:first-child,td:first-child{{text-align:left}}
th{{font-size:12px;color:var(--muted);font-weight:normal;vertical-align:bottom}}
tr.cut td{{border-bottom:3px solid var(--cut)}}
.history{{margin:4px 0 8px;font-size:13px}}
.history p{{margin:0 0 2px}}
.history .cap{{font-size:11px}}
svg.hist{{width:100%;max-width:420px;height:auto;display:block}}
svg.hist text{{font-size:9px;fill:var(--muted);font-family:inherit}}
svg.hist .rl{{stroke:var(--t1s);stroke-dasharray:3 2;stroke-width:1}}
svg.hist .rlt{{fill:var(--t1s)}}
svg.hist .b0{{fill:var(--t0s)}} svg.hist .b1{{fill:var(--t1s)}} svg.hist .b2{{fill:var(--t2s)}} svg.hist .b3{{fill:var(--t3s)}}
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
<h1>Santa Clara County Automatic Recount Watch</h1>
<p>Santa Clara County contests at or near the automatic recount margin. {E(results.get('election_name',''))}.</p>

<div class="times">
<div>Results as of: <b>{res_time_s}</b> <span class="muted">(update {E(str(state['version']))})</span></div>
<div>Last checked: <b id="checked" data-ts="{state['last_checked']}" title="{fmt_time(checked)}">{fmt_time(checked)}</b></div>
</div>

<div class="tiles">{tiles}</div>

<div class="recap">
<h2>Summary</h2>
{recap}
</div>

<p class="muted">Margin = last winner minus first loser. The official percentage divides by all ballots cast in the contest's area, including people who skipped the contest (votes + undervotes + overvotes, divided by seats). The county results site divides by candidate votes only, so its percentage is usually larger. In vote-for-2 or vote-for-3 contests it can be smaller.</p>

{''.join(sections)}

<details>
<summary>Other county contests ({len(others)}, never close)</summary>
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
    hist = load_json(HISTORY_PATH, {})
    with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
        f.write(render(results, state, cfg, hist))
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
    ap.add_argument("--once-per-day", action="store_true",
                    help="skip if new results were already loaded today (Pacific time)")
    ap.add_argument("--record-check", action="store_true",
                    help="when the version is unchanged, still update 'last checked' and re-render")
    ap.add_argument("--file")
    ap.add_argument("--version")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--backfill", nargs="+", metavar="VERSION",
                    help="add older results versions to data/history.json, then re-render")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = cfg["election_url"].rstrip("/")

    if args.backfill:
        hist = load_json(HISTORY_PATH, {})
        for v in args.backfill:
            try:
                blob = http_get(f"{base}/{v}/{cfg['report_path']}", timeout=180)
                meta, contests = parse_detail(read_detail_xml(blob))
                hist = add_snapshot(hist, snapshot_of(analyze(meta, contests, cfg), v))
                print(f"  added {v} ({meta['results_timestamp']})")
            except Exception as e:  # noqa: BLE001
                print(f"  {v}: {e}")
        write_json(HISTORY_PATH, hist)
        args.render_only = True

    if args.render_only:
        results = load_json(RESULTS_PATH, None)
        if not results:
            sys.exit("no data/results.json to render")
        write_site(results, state, cfg)
        set_output(True)
        return

    today = datetime.now(PACIFIC).date().isoformat()
    if args.once_per_day and state.get("updated_on") == today:
        print(f"new results already loaded today ({today}); skipping")
        set_output(False)
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
                if args.record_check and os.path.exists(RESULTS_PATH):
                    state["last_checked"] = now
                    write_json(STATE_PATH, state)
                    write_site(load_json(RESULTS_PATH, None), state, cfg)
                    set_output(True)
                    return
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
        "updated_on": today,
        "source_url": f"{base}/",
    }
    write_json(RESULTS_PATH, results)
    write_json(STATE_PATH, state)
    write_json(HISTORY_PATH, add_snapshot(load_json(HISTORY_PATH, {}), snapshot_of(results, version)))
    write_site(results, state, cfg)
    counts = {k: sum(1 for a in results["contests"] if a["tier"] == k) for k in (1, 2, 3)}
    print(f"version {version}: {len(results['contests'])} contests, tiers {counts}")
    set_output(True)


if __name__ == "__main__":
    main()
