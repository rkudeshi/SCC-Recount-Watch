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
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")  # full results for each version, for the dated pages
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
        # Each choice's votes, so the cards can chart how the results moved.
        rec["votes"] = {r["name"]: r["votes"] for r in a["rows"] if not r.get("write_in")}
        snap["contests"][str(a["key"])] = rec
    return snap


def add_snapshot(hist, snap):
    snaps = [s for s in hist.get("snapshots", []) if s["version"] != snap["version"]]
    snaps.append(snap)
    snaps.sort(key=lambda s: parse_clarity_time(s["results_timestamp"]) or datetime.min.replace(tzinfo=PACIFIC))
    hist["snapshots"] = snaps
    return hist


def line_votes(ballots, t):
    """Smallest margin that is NOT an automatic recount: under this many votes triggers one.
    The rule is under 25 votes or under 0.25% of ballots cast, so the larger of the two applies."""
    return max(t["recount_votes"], math.ceil(t["recount_pct"] / 100 * ballots - 1e-9))


def in_range(margin, ballots, t):
    return margin < line_votes(ballots, t)


GROUPS = {
    1: ("In recount range", "Margin under 25 votes or under 0.25% of ballots cast"),
    2: ("Were in range, out for now", ""),
    3: ("Getting closer", ""),
    4: ("Close", ""),
}


def contest_points(hist, key, t):
    pts = []
    for snap in hist.get("snapshots", []):
        rec = snap["contests"].get(str(key))
        if not rec:
            continue
        dt = parse_clarity_time(snap["results_timestamp"])
        line = line_votes(rec["ballots"], t)
        pts.append({**rec, "version": snap["version"], "line": line, "ratio": rec["margin"] / line,
                    "in": rec["margin"] < line, "dt": dt,
                    "label": dt.strftime("%b %-d") if dt else snap["version"]})
    for p in pts:
        if p["dt"] and any(q is not p and q["label"] == p["label"] for q in pts):
            p["label"] = p["dt"].strftime("%b %-d %-I%p").replace("AM", "a").replace("PM", "p")
    return pts


def current_point(a, version, t):
    line = line_votes(a["ballots_cast"], t)
    p = {"name": a["name"], "margin": a["margin"], "pct": a["pct"], "ballots": a["ballots_cast"],
         "version": str(version), "line": line, "ratio": a["margin"] / line, "in": a["margin"] < line}
    if a["type"] == "candidate":
        p["winner"], p["loser"] = a["last_winner"], a["first_loser"]
    else:
        p["passing"] = a["passing"]
    p["votes"] = {r["name"]: r["votes"] for r in a["rows"] if not r.get("write_in")}
    return p


def group_for(pts, t):
    """pts: history points ending with the current one."""
    cur, prev = pts[-1], pts[:-1]
    if cur["in"]:
        return 1
    if any(p["in"] for p in prev):
        return 2
    w = t.get("watch_ratio", 6)
    if cur["ratio"] <= w or cur["pct"] < t.get("close_pct", 2):
        m = [p["margin"] for p in pts]
        # Steady shrinking in votes: down at each of the last two updates, and
        # down at least 25% compared with three updates ago.
        if len(m) >= 4 and m[-1] < m[-2] < m[-3] and m[-1] <= 0.75 * m[-4]:
            return 3
        return 4
    return 0


def votes(n):
    return f"{fmt_n(n)} vote{'' if n == 1 else 's'}"


def short_label(name, all_names=()):
    if name in ("Yes", "No"):
        return name
    last = name.replace('"', "").split()[-1]
    if sum(1 for n in all_names if n.replace('"', "").split()[-1] == last) > 1:
        return name
    return last


def date_labels(pts):
    out, prev_month = [], None
    for p in pts:
        d = p.get("dt")
        if d:
            out.append(d.strftime("%b %-d") if d.month != prev_month else d.strftime("%-d"))
            prev_month = d.month
        else:
            out.append(p.get("label", ""))
    return out


def results_chart(a, pts):
    """Each choice's share of the vote after each update, on the same x-axis as the margin chart.
    Names sit at the start of each line, inside the chart."""
    pts = [p for p in pts if p.get("votes")]
    if len(pts) < 2:
        return ""
    names = ["Yes"] if a["type"] == "measure" else [r["name"] for r in a["rows"] if not r.get("write_in")]
    winners = {r["name"] for r in a["rows"] if r["winner"]}
    share = lambda p, nm: 100 * p["votes"].get(nm, 0) / (sum(p["votes"].values()) or 1)
    thr = 100 * a["threshold"] if a["type"] == "measure" else None
    vals = [share(p, nm) for p in pts for nm in names] + ([thr] if thr else [])
    if thr:
        # Centre on the threshold so the distance to it reads honestly.
        d = max(max(abs(v - thr) for v in vals) * 1.25, 5)
        lo, hi = thr - d, thr + d
    else:
        lo, hi = min(vals), max(vals)
        pad = max((hi - lo) * 0.1, (10 - (hi - lo)) / 2, 0)  # at least a 10-point range
        lo, hi = lo - pad, hi + pad
    span = (hi - lo) or 1
    n = len(pts)
    W, side, top = 320, 14, 16
    ph = 44 + 10 * max(0, len(names) - 2)
    bot = top + ph
    x = lambda i: side + (W - 2 * side) * i / (n - 1)
    y = lambda v: bot - (v - lo) / span * ph
    out = []
    if thr:
        out.append(f'<line class="rl" x1="{side}" x2="{W - side}" y1="{y(thr):.1f}" y2="{y(thr):.1f}"/>')
    order = sorted(names, key=lambda nm: (nm in winners or a["type"] == "measure"))
    for nm in order:  # leaders drawn last, on top
        role = "w" if nm in winners or a["type"] == "measure" else "l"
        line = " ".join(f"{x(i):.1f},{y(share(p, nm)):.1f}" for i, p in enumerate(pts))
        out.append(f'<polyline class="cl {role}" points="{line}"/>')
    labs = sorted([y(share(pts[0], nm)) - 4, short_label(nm, names),
                   "w" if nm in winners or a["type"] == "measure" else "l"] for nm in names)
    if thr:
        labs = sorted(labs + [[y(thr) - 4, f"{thr:.0f}% needed", "thr"]])
    for i in range(1, len(labs)):
        labs[i][0] = max(labs[i][0], labs[i - 1][0] + 10)
    for ly, txt, role in labs:
        anchor, lx = ("end", W - side) if role == "thr" else ("start", side - 4)
        out.append(f'<text class="lab {role}" x="{lx}" y="{ly:.1f}" text-anchor="{anchor}">{E(txt)}</text>')
    dy = max(bot, labs[-1][0] if labs else bot) + 12
    step = (W - 2 * side) / (n - 1)
    for i, (p, lab) in enumerate(zip(pts, date_labels(pts))):
        out.append(f'<text class="d" x="{x(i):.1f}" y="{dy:.0f}" text-anchor="middle">{E(lab)}</text>')
        d = p.get("dt")
        full = d.strftime("%a %b %-d") if d else p.get("label", "")
        parts = [f"{short_label(nm, names)} {share(p, nm):.1f}%" for nm in sorted(names, key=lambda k: -share(p, k))]
        out.append(f'<rect class="hit" x="{x(i) - step / 2:.1f}" y="0" width="{step:.1f}" height="{dy}" '
                   f'data-tip="{E(full + ": " + ", ".join(parts))}" data-x="{x(i):.1f}" data-y="{top}"/>')
    svg = (f'<svg class="spark res" viewBox="0 0 {W} {dy + 2:.0f}" role="img" '
           f'aria-label="Share of the vote after each update">{"".join(out)}</svg>')
    return f'<div class="sparkwrap">{svg}<div class="tip" hidden></div></div>'


def sparkline(pts):
    """Margin in votes at each update (labelled), with the recount line dashed underneath."""
    if len(pts) < 2:
        return ""
    n = len(pts)
    W, H, top, bottom, side = 320, 62, 13, 14, 14
    vmax = max(max(p["margin"] for p in pts), max(p["line"] for p in pts)) * 1.05 or 1
    ph = H - top - bottom
    x = lambda i: side + (W - 2 * side) * i / (n - 1)
    y = lambda v: top + ph - ph * v / vmax
    out = [f'<svg class="spark" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Vote margin after each day\'s update; dashed line is the automatic recount line">']
    base = top + ph
    out.append(f'<line class="base" x1="{side}" x2="{W - side}" y1="{base}" y2="{base}"/>')
    rl = " ".join(f"{x(i):.1f},{y(p['line']):.1f}" for i, p in enumerate(pts))
    out.append(f'<polyline class="rl" points="{rl}"/>')
    ml = " ".join(f"{x(i):.1f},{y(p['margin']):.1f}" for i, p in enumerate(pts))
    out.append(f'<polyline class="ml" points="{ml}"/>')
    prev_month = None
    for i, p in enumerate(pts):
        cx, cy = x(i), y(p["margin"])
        d = p.get("dt")
        full = (d.strftime("%a %b %-d") if d else p.get("label", ""))
        tip = f'{full}: {votes(p["margin"])}, {fmt_margin_pct(p["pct"])}' + (" (in recount range)" if p["in"] else "")
        out.append(f'<circle class="{"pin" if p["in"] else "pt"}" cx="{cx:.1f}" cy="{cy:.1f}" r="{3.5 if p["in"] else 2.5}"/>')
        out.append(f'<text class="v{" in" if p["in"] else ""}" x="{cx:.1f}" y="{cy - 5:.1f}" text-anchor="middle">{fmt_n(p["margin"])}</text>')
        if d:
            lab = d.strftime("%b %-d") if d.month != prev_month else d.strftime("%-d")
            prev_month = d.month
        else:
            lab = p.get("label", "")
        out.append(f'<text class="d" x="{cx:.1f}" y="{H - 2}" text-anchor="middle">{E(lab)}</text>')
        # Wide invisible target so a tap anywhere in the day's column works on phones.
        out.append(f'<rect class="hit" x="{cx - (W - 2 * side) / (n - 1) / 2:.1f}" y="0" '
                   f'width="{(W - 2 * side) / (n - 1):.1f}" height="{H}" data-tip="{E(tip)}" data-x="{cx:.1f}" data-y="{cy:.1f}"/>')
    out.append("</svg>")
    return f'<div class="sparkwrap">{"".join(out)}<div class="tip" hidden></div></div>'


def section(key, title, body, open_):
    return (f'<details class="sec" data-sec="{key}"{" open" if open_ else ""}>'
            f'<summary><h4 class="sh">{E(title)}</h4></summary>\n{body}\n</details>')


def margin_text(a):
    return f"{votes(a['margin'])} / {fmt_margin_pct(a['pct'])}"


def mini_spark(pts):
    """Tiny margin trend for the summary list (no text, so copy-paste stays clean)."""
    m = [p["margin"] for p in pts]
    if len(m) < 2:
        return ""
    W, H = 64, 14
    hi = max(max(m), max(p["line"] for p in pts)) or 1
    xy = [(1 + (W - 4) * i / (len(m) - 1), H - 1 - (H - 3) * v / hi) for i, v in enumerate(m)]
    ly = H - 1 - (H - 3) * pts[-1]["line"] / hi
    dot = "pin" if pts[-1]["in"] else "pt"
    return (f'<svg class="mini" viewBox="0 0 {W} {H}" width="{W}" height="{H}" aria-hidden="true">'
            f'<line class="rl" x1="1" x2="{W - 3}" y1="{ly:.1f}" y2="{ly:.1f}"/>'
            f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in xy)}"/>'
            f'<circle class="{dot}" cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="2"/></svg>')


def render_contest(a, pts, anchor=True):
    g = a.get("group", 0)
    cls = f"contest g{g}" if g else "contest g0"
    np_ = int(a.get("precincts_total") or a.get("precincts_reporting") or 0)
    sub = f"{fmt_n(np_)} precinct{'' if np_ == 1 else 's'} · {fmt_n(a['ballots_cast'])} ballots cast"
    if a["type"] == "candidate":
        sub = f"Vote for {a['vote_for']} · {sub}"
        col_label, pv_label, share_title = f"Candidate<br><small>(vote for {a['vote_for']})</small>", "% of candidate votes", "Share of candidate votes"
    else:
        sub = f"Needs {a['threshold_label']} Yes · {sub}"
        col_label, pv_label, share_title = "Choice", "% of votes", "Yes share of the vote"

    rows = []
    n_rows = [r for r in a["rows"] if not r.get("write_in")]
    for i, r in enumerate(a["rows"]):
        classes = []
        if a["type"] == "candidate" and r["winner"] and (i + 1 < len(n_rows)) and not n_rows[i + 1]["winner"]:
            classes.append("cut")
        if r.get("write_in"):
            classes.append("wi")
        name = ("Write-ins" if r.get("write_in") else E(r["name"])) + (" ✓" if a["type"] == "measure" and r["winner"] else "")
        rows.append(
            f'<tr class="{" ".join(classes)}"><td>{name}</td><td>{fmt_n(r["votes"])}</td>'
            f'<td>{fmt_pct(r["pct_ballots"])}</td><td>{fmt_pct(r["pct_votes"])}</td></tr>')
        if "cut" in classes:
            # The gap between the last winner and the first loser, so nobody has to do the math.
            rows.append(f'<tr class="mg"><td>Margin</td><td>{fmt_n(a["margin"])}</td>'
                        f'<td>{fmt_margin_pct(a["pct"])}</td><td>{fmt_pct(a["site_pct"])}</td></tr>')
    b = a["ballots_cast"] or 1
    for j, (k, v) in enumerate((("Overvotes", a["overvotes"]), ("Undervotes", a["undervotes"]))):
        rows.append(f'<tr class="ou{" first" if j == 0 else ""}"><td>{k}</td><td>{fmt_n(v)}</td>'
                    f'<td>{fmt_pct(100 * v / b)}</td><td></td></tr>')
    rows.append(f'<tr class="bc"><td>Ballots cast</td><td>{fmt_n(a["ballots_cast"])}</td><td></td><td></td></tr>')

    margin_body = (f'<div class="margin"><span class="big">{E(margin_text(a))}</span></div>\n{sparkline(pts)}')
    table = (f'<div class="tbl"><table>\n<tr><th>{col_label}</th><th>Votes</th><th>% of ballots cast</th>'
             f'<th>{pv_label}</th></tr>\n{"".join(rows)}\n</table></div>')
    chart = results_chart(a, pts)
    secs = [section("m", "Margin", margin_body, True)]
    if chart:
        secs.append(section("s", share_title, chart, True))
    secs.append(section("r", "Results", table, True))
    idattr = f' id="c{E(str(a["key"]))}"' if anchor else ""
    return f"""<div class="{cls}"{idattr}>
<div class="chead"><a class="totop" href="#top" aria-label="Back to top">↑ Top</a><h3>{E(a['name'])}</h3>
<p class="muted">{E(sub)}</p></div>
{chr(10).join(secs)}
</div>"""


def nc_item(a, pts):
    """A 'Not close' contest: one line that opens into its full card."""
    return (f'<li><details class="nc" id="c{E(str(a["key"]))}"><summary><span class="nm">{E(a["name"])}</span><span class="cp">: </span>'
            f'<span class="num">{votes(a["margin"])} / {fmt_margin_pct(a["pct"])}{mini_spark(pts)}</span></summary>\n'
            f'{render_contest(a, pts, anchor=False)}</details></li>')


def recap_row(a, pts):
    # The hidden ": " and line break keep a copy-and-paste readable ("Name: 13 votes / 0.07%").
    return (f'<li><span class="nm"><a href="#c{E(str(a["key"]))}">{E(short_name(a))}</a>'
            f'<small>{fmt_n(a["ballots_cast"])} ballots cast</small></span><span class="cp">: </span>'
            f'<span class="num">{votes(a["margin"])} / {fmt_margin_pct(a["pct"])}{mini_spark(pts)}</span></li>')


POLICY_URL = ("https://files.santaclaracounty.gov/exjcpb1586/2025-07/"
              "board-of-supervisors-policy-manual-complete-07.25.25.pdf"
              "?VersionId=Ichcak.tSP_lnk63yOJDjAdMHv.zlxGL")
RECOUNT_FAQ_URL = "https://vote.santaclaracounty.gov/elections/recounts-and-faqs"


def short_name(a):
    # "Measure Q - Cambrian School District, Parcel Tax - 2/3rds ..." -> "Measure Q - Cambrian School District"
    if a["type"] == "measure":
        return " - ".join(a["name"].split(" - ")[:2]).split(",")[0]
    return a["name"]


def date_picker(res_time_s, pages, current):
    """The 'Results as of' box. With older pages available it opens a list of them."""
    if not pages or len(pages) < 2:
        return f'<div class="times">Results as of <b>{res_time_s}</b></div>'
    items = []
    for i, pg in enumerate(pages):
        cur = ' aria-current="page" class="cur"' if pg["version"] == current else ""
        latest = ' <span class="muted">latest</span>' if i == 0 else ""
        items.append(f'<li><a href="{E(pg["file"])}"{cur}>{E(pg["label"])}{latest}</a></li>')
    return (f'<div class="dd"><button type="button" class="times ddbtn" id="ddbtn" aria-haspopup="true" aria-expanded="false">'
            f'<span>Results as of <b>{res_time_s}</b></span><span class="caret" aria-hidden="true"></span></button>'
            f'<ul class="ddlist" id="ddlist" hidden>{"".join(items)}</ul></div>')


def render(results, state, cfg, hist=None, pages=None):
    t = cfg["tiers"]
    PCT_LINES[:] = [t["recount_pct"]]
    w = t.get("watch_ratio", 6)
    ginfo = {k: (v[0], v[1].format(w=w)) for k, v in GROUPS.items()}
    hist = hist or {}
    version = str(state["version"])
    contests = results["contests"]
    pts_by_key = {}
    for a in contests:
        a["tier"] = tier_for(a["margin"], a["pct"], t)
        a["line"] = line_votes(a["ballots_cast"], t)
        a["ratio"] = a["margin"] / a["line"]
        a["recount_votes_cfg"], a["recount_pct_cfg"] = t["recount_votes"], t["recount_pct"]
        prev = [p for p in contest_points(hist, a["key"], t) if p["version"] != version]
        cur = current_point(a, version, t)
        cur_hist = [p for p in contest_points(hist, a["key"], t) if p["version"] == version]
        if cur_hist:
            cur.update({k: cur_hist[0][k] for k in ("label", "dt")})
        else:
            cur["label"], cur["dt"] = "now", parse_clarity_time(results.get("results_timestamp", ""))
        pts = prev + [cur]
        pts_by_key[str(a["key"])] = pts
        a["group"] = group_for(pts, t)
    contests.sort(key=lambda a: (a["group"] or 9, a["ratio"], a["margin"]))
    counts = {k: sum(1 for a in contests if a["group"] == k) for k in GROUPS}
    res_time = parse_clarity_time(results.get("results_timestamp", ""))
    res_time_s = (f'<span title="{fmt_time(res_time)}">{fmt_short_time(res_time)}</span>'
                  if res_time else E(results.get("results_timestamp", "")))

    old_bar = ""
    if pages and version != pages[0]["version"]:
        when = fmt_short_time(res_time).split(",")[0] if res_time else "an earlier update"
        old_bar = (f'<div class="oldbar">You\'re looking at older results, as of {E(when)}. '
                   f'<a href="index.html">Go to the latest results</a></div>')

    tiles = "".join(
        f'<a class="tile g{k}" href="#group{k}"><b>{counts[k]}</b><span>{ginfo[k][0]}</span></a>'
        for k in GROUPS)

    recap_parts = []
    for k in GROUPS:
        items = "".join(
            recap_row(a, pts_by_key[str(a["key"])]) for a in contests if a["group"] == k)
        body = f"<ul>{items}</ul>" if items else '<p class="muted">None right now.</p>'
        recap_parts.append(f'<h4 class="rh g{k}">{ginfo[k][0]} ({counts[k]})</h4>{body}')
    others = [a for a in contests if a["group"] == 0]
    recap_parts.append(f'<details><summary>Not close ({len(others)})</summary><ul>{"".join(recap_row(a, pts_by_key[str(a["key"])]) for a in others)}</ul></details>')
    recap = "".join(recap_parts)

    sections = []
    for k in GROUPS:
        items = [a for a in contests if a["group"] == k]
        body = "".join(render_contest(a, pts_by_key[str(a["key"])]) for a in items) or '<p class="none">No contests in this group right now.</p>'
        sections.append(f"""<section id="group{k}">
<h2 class="sec g{k}">{ginfo[k][0]}{f" <small>{ginfo[k][1]}</small>" if ginfo[k][1] else ""}</h2>
{body}
</section>""")

    sections.append(f"""<section id="group0">
<h2 class="sec g0">Not close</h2>
<button type="button" id="ncall">Show all as cards</button>
<ul class="nclist">{"".join(nc_item(a, pts_by_key[str(a["key"])]) for a in others)}</ul>
</section>""")
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
  --t1:#f8d4d2;--t1s:#b52a24;--t2:#fde7bd;--t2s:#9a5d00;--t3:#dbe6f3;--t3s:#2f5d8f;--t0:#e6e6e6;--t0s:#8a8a8a;--th:#f0f0f0}}
@media (prefers-color-scheme: dark){{:root{{--bg:#111;--box:#1b1b1b;--fg:#eee;--muted:#9a9a9a;--line:#333;--cut:#eee;--link:#8ab4f8;
  --t1:#5a2321;--t1s:#ff9b95;--t2:#4d3a15;--t2s:#f2c06a;--t3:#203246;--t3s:#a9c6e8;--t0:#2a2a2a;--t0s:#9a9a9a;--th:#262626;color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:15px;line-height:1.45;padding:16px}}
main{{max-width:700px;margin:0 auto}}
h1{{font-size:26px;margin:0 0 6px;line-height:1.2}}
h1 .pre{{display:block;font-size:17px;font-weight:normal;color:var(--muted)}}
p{{margin:4px 0}}
.muted{{color:var(--muted);font-size:13px}}
.tiles{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}}
.tile{{border-radius:4px;padding:10px;color:var(--fg);text-decoration:none;display:block}}
.tile b{{display:block;font-size:30px;line-height:1.1}}
.tile span{{font-size:14px;font-weight:600}}
.tile.g1{{background:var(--t1)}} .tile.g2{{background:var(--t2)}} .tile.g3{{background:var(--t3)}} .tile.g4{{background:var(--t0)}}
h2.sec{{font-size:22px;margin:36px 0 12px;padding:10px 12px;border-radius:4px;border-bottom:4px solid}}
h2.sec small{{display:block;font-size:14px;font-weight:normal}}
h2.g1{{background:var(--t1);border-color:var(--t1s)}} h2.g2{{background:var(--t2);border-color:var(--t2s)}} h2.g3{{background:var(--t3);border-color:var(--t3s)}} h2.g4{{background:var(--t0);border-color:var(--t0s)}} h2.g0{{background:transparent;border-color:var(--line)}}
h3{{font-size:17px;margin:0}}
.contest{{background:var(--box);border:1px solid var(--line);border-radius:4px;padding:12px;margin-bottom:12px}}
.margin{{margin:6px 0 8px;padding:5px 8px;border-radius:4px}}
.margin .big{{font-size:15px;font-weight:600}}
.margin .cmp{{font-size:13px;color:var(--muted)}}
.recap{{background:var(--box);border:1px solid var(--line);border-radius:4px;padding:10px 12px;margin:12px 0}}
.recap h2{{font-size:18px;margin:0 0 4px}}
.recap h4{{font-size:15px;margin:10px 0 0;padding:2px 6px;border-radius:3px;display:inline-block}}
.rh.g1{{background:var(--t1)}} .rh.g2{{background:var(--t2)}} .rh.g3{{background:var(--t3)}} .rh.g4{{background:var(--t0)}}
.recap ul{{margin:4px 0 0;padding:0;list-style:none}}
.recap li{{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid var(--line)}}
.recap li:last-child{{border-bottom:0}}
.recap li a{{text-decoration:none;font-weight:600}} .recap li a:hover{{text-decoration:underline}}
.recap .num{{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--muted)}}
.recap p{{margin:4px 0 0}}
.tbl{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right}}
th:first-child,td:first-child{{text-align:left}}
th{{font-size:12px;color:var(--muted);font-weight:normal;vertical-align:bottom;background:var(--th)}}
tr.cut td{{border-bottom:0}}
tr.mg td{{font-size:12px;font-style:italic;color:var(--muted);padding-top:2px;padding-bottom:2px;background:var(--th);border-bottom-color:var(--th)}}
tr.cut td{{border-bottom-color:var(--th)}}
.chead{{position:relative}} .chead h3{{padding-right:52px}}
a.totop{{position:absolute;top:10px;right:12px;font-size:12px;color:var(--muted);text-decoration:none}}
.times{{background:var(--box);border:1px solid var(--line);border-radius:4px;padding:10px 12px;margin:12px 0;font-size:17px}}
details.nc{{margin:0}}
.dd{{position:relative;margin:12px 0}}
.ddbtn{{display:flex;width:100%;align-items:center;justify-content:space-between;gap:10px;margin:0;font:inherit;font-size:17px;color:var(--fg);text-align:left;cursor:pointer}}
.caret{{flex:none;border-style:solid;border-width:6px 5px 0 5px;border-color:var(--muted) transparent transparent transparent}}
.ddbtn[aria-expanded="true"] .caret{{transform:rotate(180deg)}}
.ddlist{{position:absolute;left:0;right:0;top:100%;margin:4px 0 0;padding:4px 0;list-style:none;background:var(--box);border:1px solid var(--line);border-radius:4px;box-shadow:0 4px 14px rgba(0,0,0,.15);z-index:20}}
.ddlist a{{display:block;padding:8px 12px;color:var(--fg);text-decoration:none}}
.ddlist a:hover{{background:var(--th)}}
.ddlist a.cur{{font-weight:600;background:var(--th)}}
.oldbar{{position:sticky;top:0;z-index:30;background:#ffe27a;color:#111;padding:10px 16px;margin:-16px -16px 12px;font-size:15px;text-align:center;box-shadow:0 1px 0 rgba(0,0,0,.15)}}
.oldbar a{{color:#111;font-weight:700}}
@media (max-width:440px){{.oldbar{{margin:-12px -12px 12px}}}}
#ncall{{font:inherit;font-size:13px;background:none;border:0;padding:0;color:var(--link);cursor:pointer;margin:0 0 6px}}
th small{{font-size:11px}}
.sparkwrap{{position:relative;max-width:440px;margin:6px 0 2px}}
.tip{{position:absolute;transform:translate(-50%,-100%);margin-top:-8px;background:var(--fg);color:var(--box);font-size:12px;padding:3px 7px;border-radius:3px;width:max-content;max-width:260px;pointer-events:none;z-index:2}}
.recap .nm{{display:flex;flex-direction:column}} .recap .nm small{{color:var(--muted);font-size:12px}}
.cp{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:pre}}
.recap details{{margin-top:12px}}
svg.spark{{width:100%;height:auto;display:block;overflow:visible}}
svg.spark text{{font-size:9px;fill:var(--muted);font-family:inherit;font-variant-numeric:tabular-nums}}
svg.spark text.v{{fill:var(--fg)}} svg.spark text.v.in{{fill:var(--t1s);font-weight:700}}
svg.spark .base{{stroke:var(--line);stroke-width:1}}
svg.spark .rl{{fill:none;stroke:var(--t1s);stroke-dasharray:3 3;stroke-width:1}}
svg.spark .ml{{fill:none;stroke:var(--fg);stroke-width:2;stroke-linejoin:round}}
svg.spark .pt{{fill:var(--fg)}} svg.spark .pin{{fill:var(--t1s);stroke:var(--box);stroke-width:1.5}}
svg.res .cl{{fill:none;stroke-width:2;stroke-linejoin:round}} svg.res .cl.w{{stroke:var(--fg)}} svg.res .cl.l{{stroke:var(--t0s);stroke-width:1.5}}
svg.res text.lab{{fill:var(--fg);font-size:10px}} svg.res text.lab.l{{fill:var(--muted)}}
svg.spark .hit{{fill:transparent;cursor:pointer;-webkit-tap-highlight-color:transparent}}
.contest{{padding:0 14px 6px;overflow:hidden}}
.contest.g1{{border-color:var(--t1s)}} .contest.g2{{border-color:var(--t2s)}} .contest.g3{{border-color:var(--t3s)}}
.chead{{margin:0 -14px;padding:10px 14px 9px;background:var(--th);border-bottom:1px solid var(--line)}}
.chead p.muted{{margin:2px 0 0;color:var(--fg);opacity:.75}}
.contest.g1 .chead{{background:var(--t1);border-color:var(--t1s)}} .contest.g2 .chead{{background:var(--t2);border-color:var(--t2s)}} .contest.g3 .chead{{background:var(--t3);border-color:var(--t3s)}} .contest.g4 .chead{{background:var(--t0);border-color:var(--t0s)}}
details.sec{{margin:0;padding:16px 0 10px}}
details.sec+details.sec{{border-top:1px solid var(--line)}}
details.sec>summary,details.nc>summary{{list-style:none;cursor:pointer;font-weight:normal}}
details.sec>summary{{display:flex;align-items:center;gap:6px}}
details.sec>summary::-webkit-details-marker,details.nc>summary::-webkit-details-marker{{display:none}}
details.sec>summary::before,details.nc>summary::before{{content:"";display:inline-block;border-style:solid;border-width:4px 0 4px 6px;border-color:transparent transparent transparent var(--muted);transition:transform .15s}}
details.nc>summary::before{{margin:7px 7px 0 0;flex:none;align-self:flex-start}}
details.nc>summary{{display:flex;gap:0;align-items:flex-start}}
details.nc>summary .nm{{flex:1;padding-right:12px}}
details.nc>summary .num{{display:flex;flex-direction:column;align-items:flex-end;white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--muted)}}
details.sec[open]>summary::before,details.nc[open]>summary::before{{transform:rotate(90deg)}}
details.sec[open]>summary{{margin-bottom:8px}}
h4.sh{{margin:0;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
.margin{{margin:0 0 4px;padding:0}}
.margin .big{{font-size:16px}}
p.fn{{font-size:12px;margin:8px 0 0}}
.recap .num{{display:flex;flex-direction:column;align-items:flex-end}}
svg.mini{{display:block;margin-top:3px}} svg.mini polyline{{fill:none;stroke:var(--fg);stroke-width:1.5;stroke-linejoin:round}}
svg.mini .rl{{stroke:var(--t1s);stroke-width:1;stroke-dasharray:2 2}} svg.mini .pt{{fill:var(--fg)}} svg.mini .pin{{fill:var(--t1s)}}
body{{overflow-anchor:none}}
tr.ou td{{color:var(--muted)}}
tr.ou.first td{{border-top:1px solid var(--muted)}}
tr.bc td{{font-weight:600;border-bottom:0;white-space:nowrap}}
svg.res text.lab{{paint-order:stroke;stroke:var(--box);stroke-width:3px;stroke-linejoin:round}}
svg.res text.lab.thr{{fill:var(--t1s)}}
ul.nclist{{margin:0;padding:0;list-style:none}}
ul.nclist>li{{padding:5px 0;border-bottom:1px solid var(--line)}}
details.nc>.contest{{margin:8px 0 6px}}
@media (prefers-reduced-motion:reduce){{details.sec>summary::before,details.nc>summary::before{{transition:none}}}}
tr.wi td{{color:var(--muted)}}
tr.tot td{{color:var(--muted);font-size:13px;border-bottom:0}}
tr.first td{{border-top:1px solid var(--muted)}}
.none{{color:var(--muted)}}
details{{margin-top:24px}}
summary{{cursor:pointer;font-weight:600}}
p.lede{{margin:8px 0 4px}}
details.info{{margin:0 0 10px}}
details.info>summary{{font-weight:normal;color:var(--link)}}
.infobody{{background:var(--box);border:1px solid var(--line);border-radius:4px;padding:4px 12px;margin-top:6px;font-size:14px}}
.infobody p{{margin:8px 0}}
.infobody blockquote{{margin:10px 0;padding:2px 0 2px 10px;border-left:3px solid var(--line)}}
.infobody p.src{{color:var(--muted);font-size:13px}}
a{{color:var(--link)}}
@media (max-width:440px){{body{{padding:12px}}.tile b{{font-size:24px}}.tile span{{font-size:11px}}.margin .big{{font-size:14px}}th,td{{padding:4px 3px}}}}
</style>
</head>
<body>
{old_bar}
<main id="top">
<h1><span class="pre">Santa Clara County &middot; {E(results.get('election_name','').replace(' General Election',''))}</span>Automatic Recount Watch</h1>
<p class="lede">Under county policy, local contests decided by fewer than 25 votes or less than 0.25% of ballots cast get an automatic hand recount.</p>
<details class="info">
<summary>More info</summary>
<div class="infobody">
<blockquote>&ldquo;The Registrar of Voters shall conduct, prior to certification of election results, manual recounts in all contests wholly contained within Santa Clara County (excluding those for state and federal office) where the margin of victory is either less than 0.25 percent of the ballots cast, or less than 25 votes, except for countywide races or citywide races in the City of San Jose for which machine recounts may be used.&rdquo;</blockquote>
<p class="src"><a href="{POLICY_URL}">Board of Supervisors Policy 3.63</a>, adopted February 27, 2018. The Registrar of Voters explains how recounts work in <a href="{RECOUNT_FAQ_URL}">Recounts and FAQs</a>.</p>
<p><b>Margin</b> is the votes for the last winning candidate minus the votes for the first losing candidate. For a ballot measure, it is the number of votes that would flip the outcome.</p>
<p><b>Why this page shows two percentages.</b> The official results website shows each candidate's percentage of candidate votes, which counts only the people who voted in that contest. The recount policy uses ballots cast, which also counts people who skipped the contest (undervotes) or voted for too many (overvotes). Ballots cast for a contest is (votes + undervotes + overvotes) divided by the number of seats.</p>
<p>For example, say 40,000 ballots are cast in a contest and 8,000 voters skip it, so the candidates get 32,000 votes between them. If the margin is 90 votes, the official results website shows a gap of 0.28% (90 &divide; 32,000). As a share of ballots cast it is 0.225% (90 &divide; 40,000), which is under the 0.25% recount line. That is why this page shows both. (In vote-for-2 or vote-for-3 contests the ballots-cast percentage can be the larger one.)</p>
<p><b>This is an unofficial tracker.</b> Margins change as more ballots are counted.</p>
</div>
</details>

{date_picker(res_time_s, pages, version)}

<div class="tiles">{tiles}</div>

<div class="recap">
<h2>Summary</h2>
{recap}
</div>

{''.join(sections)}

<details>
<summary>Contests not included ({len(results['excluded']) + len(results['skipped'])})</summary>
<ul>{excl}</ul>
</details>

<p class="muted" style="margin-top:24px">Source: <a href="{E(state['source_url'])}">Santa Clara County official results</a>. Checked about once an hour. Unofficial tracker; margins change as the county counts more ballots.</p>
</main>
<script>
// Tap (phones) or hover (desktop) a day on a sparkline to see its numbers.
(function(){{
  function show(r){{
    var w=r.closest('.sparkwrap'),tip=w.querySelector('.tip'),svg=w.querySelector('svg');
    var s=svg.getBoundingClientRect().width/svg.viewBox.baseVal.width;
    tip.textContent=r.getAttribute('data-tip');
    tip.hidden=false;
    var hw=tip.offsetWidth/2,sw=svg.getBoundingClientRect().width;
    tip.style.left=Math.min(Math.max(r.getAttribute('data-x')*s,hw),sw-hw)+'px';
    tip.style.top=(r.getAttribute('data-y')*s)+'px';
  }}
  function hideAll(){{document.querySelectorAll('.tip').forEach(function(t){{t.hidden=true}})}}
  document.addEventListener('click',function(e){{
    var r=e.target.closest&&e.target.closest('rect.hit');hideAll();if(r)show(r);
  }});
  // Opening or closing a section on one card does the same on every card.
  // The section heading you tapped stays put on screen while cards above it grow or shrink.
  var busy=false,anchor=null,anchorTop=0;
  document.addEventListener('click',function(e){{
    var sm=e.target.closest&&e.target.closest('details.sec>summary');
    if(sm){{anchor=sm;anchorTop=sm.getBoundingClientRect().top;}}
  }},true);
  document.addEventListener('toggle',function(e){{
    var d=e.target;if(busy||!d.matches||!d.matches('details.sec'))return;busy=true;
    document.querySelectorAll('details.sec[data-sec="'+d.dataset.sec+'"]').forEach(function(x){{if(x!==d)x.open=d.open}});
    var sm=d.querySelector('summary');
    if(anchor===sm){{window.scrollBy(0,sm.getBoundingClientRect().top-anchorTop);}}
    anchor=null;
    setTimeout(function(){{busy=false}},0);
  }},true);
  var nb=document.getElementById('ncall');
  if(nb)nb.addEventListener('click',function(){{
    var open=nb.textContent==='Show all as cards';
    document.querySelectorAll('details.nc').forEach(function(d){{d.open=open}});
    nb.textContent=open?'Show as list':'Show all as cards';
  }});
  // Date picker: a custom list of every day's results.
  var dbtn=document.getElementById('ddbtn'),dl=document.getElementById('ddlist');
  if(dbtn){{
    dbtn.addEventListener('click',function(e){{e.stopPropagation();dl.hidden=!dl.hidden;dbtn.setAttribute('aria-expanded',!dl.hidden);}});
    document.addEventListener('click',function(e){{if(!dl.hidden&&!e.target.closest('.dd')){{dl.hidden=true;dbtn.setAttribute('aria-expanded','false');}}}});
    document.addEventListener('keydown',function(e){{if(e.key==='Escape'&&!dl.hidden){{dl.hidden=true;dbtn.setAttribute('aria-expanded','false');dbtn.focus();}}}});
  }}
  // Links from the summary open a collapsed card.
  function openHash(){{var el=location.hash&&document.getElementById(location.hash.slice(1));if(el&&el.tagName==='DETAILS')el.open=true;}}
  window.addEventListener('hashchange',openHash);openHash();
  document.addEventListener('pointerover',function(e){{
    if(e.pointerType!=='mouse')return;
    var r=e.target.closest&&e.target.closest('rect.hit');if(r){{hideAll();show(r);}}
  }});
  document.addEventListener('pointerout',function(e){{
    if(e.pointerType==='mouse'&&e.target.closest&&e.target.closest('rect.hit'))hideAll();
  }});
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
    """index.html for the latest results, plus one page per earlier day (YYYY-MM-DD.html),
    each built only from what was known at that update."""
    os.makedirs(SITE_DIR, exist_ok=True)
    hist = load_json(HISTORY_PATH, {})
    snaps = hist.get("snapshots", [])
    latest = str(state["version"])
    pages = []
    for snap in reversed(snaps):
        v = snap["version"]
        if v != latest and not os.path.exists(os.path.join(ARCHIVE_DIR, f"{v}.json")):
            continue
        dt = parse_clarity_time(snap["results_timestamp"])
        pages.append({"version": v, "label": fmt_short_time(dt) if dt else v,
                      "file": "index.html" if v == latest else f"{dt.strftime('%Y-%m-%d') if dt else v}.html"})
    if not pages or pages[0]["version"] != latest:
        pages.insert(0, {"version": latest, "label": "Latest", "file": "index.html"})
    with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
        f.write(render(results, state, cfg, hist, pages))
    write_json(os.path.join(SITE_DIR, "results.json"), {"state": state, **results})
    for pg in pages[1:]:
        v = pg["version"]
        old = load_json(os.path.join(ARCHIVE_DIR, f"{v}.json"), None)
        cut = parse_clarity_time(old.get("results_timestamp", ""))
        upto = [sn for sn in snaps if cut is None or (parse_clarity_time(sn["results_timestamp"]) or cut) <= cut]
        with open(os.path.join(SITE_DIR, pg["file"]), "w") as f:
            f.write(render(old, {**state, "version": v}, cfg, {"snapshots": upto}, pages))


def archive_results(results, version):
    write_json(os.path.join(ARCHIVE_DIR, f"{version}.json"), results)


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
                res = analyze(meta, contests, cfg)
                hist = add_snapshot(hist, snapshot_of(res, v))
                archive_results(res, v)
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
    archive_results(results, version)
    write_json(STATE_PATH, state)
    write_json(HISTORY_PATH, add_snapshot(load_json(HISTORY_PATH, {}), snapshot_of(results, version)))
    write_site(results, state, cfg)
    counts = {k: sum(1 for a in results["contests"] if a["tier"] == k) for k in (1, 2, 3)}
    print(f"version {version}: {len(results['contests'])} contests, tiers {counts}")
    set_output(True)


if __name__ == "__main__":
    main()
