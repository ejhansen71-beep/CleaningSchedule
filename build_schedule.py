#!/usr/bin/env python3
"""Build the daily cleaning-team schedule.

Usage:
    python3 build_schedule.py                       # asks which teams are working, schedules tomorrow
    python3 build_schedule.py --date 2026-11-16     # a specific day
    python3 build_schedule.py --teams 1,2,4,6 --large 1,2   # no questions asked
    python3 build_schedule.py --customers my_week.csv --seconds 120

See README.md for the customer file format and the travel-time options.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

from solver import Problem, solve
from travel import DAY_NAMES, build_travel

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_RUN = os.path.join(HERE, 'last_run.json')


def hm(text):
    """Accepts 08:00, 0800, 800 (spaces ignored) and returns minutes after midnight."""
    t = text.strip().replace(' ', '').replace(':', '')
    if not t.isdigit() or not 3 <= len(t) <= 4:
        raise ValueError(f"'{text}' is not a time like 0800 or 08:00")
    h, m = int(t[:-2]), int(t[-2:])
    if h > 23 or m > 59:
        raise ValueError(f"'{text}' is not a valid time")
    return h * 60 + m


def fmt(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


# ----------------------------------------------------------------------------- input
def load_customers(path, cfg):
    with open(path, newline='', encoding='utf-8-sig') as f:
        text = f.read()
    first = text.splitlines()[0]
    delim = '\t' if '\t' in first else ','
    rows = list(csv.reader(text.splitlines(), delimiter=delim))
    header = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            for k, h in enumerate(header):
                if h.startswith(n):
                    return k
        raise SystemExit(f"Customer file is missing a '{names[0]}' column.")

    has_code = any(h == 'code' for h in header)
    if has_code:
        ix = dict(name=col('name'), city=col('city'), team=col('team'), mins=col('time in home'),
                  code=header.index('code'), type=col('type'))
        codes = {str(k).strip().upper(): v for k, v in cfg['arrival_codes'].items()}
    else:        # older files with the three time columns spelled out
        ix = dict(name=col('name'), city=col('city'), team=col('team'), mins=col('time in home'),
                  after=col('arrive after'), late=col('arrive no later'), dep=col('depart'), type=col('type'))
    customers = []
    for line_no, r in enumerate(rows[1:], 2):
        if not any(x.strip() for x in r):
            continue
        r = list(r) + [''] * (len(header) + 4 - len(r))
        name = r[ix['name']].strip().rstrip(',').strip()
        city = r[ix['city']].strip().rstrip(',').strip()
        if ',' not in city:
            city = f"{city}, {cfg['default_state']}"
        team = r[ix['team']].strip()
        try:
            if has_code:
                code = r[ix['code']].strip().upper()
                if code == 'C':     # custom: the three times follow the Code column
                    k = ix['code']
                    after, late, depart = (hm(r[k + 1]), hm(r[k + 2]), hm(r[k + 3]))
                elif code in codes:
                    after, late, depart = (hm(x) for x in codes[code])
                else:
                    raise SystemExit(f"Line {line_no} ({name}): unknown arrival code '{code}'. "
                                     f"Use C or one of {', '.join(codes)}.")
            else:
                after, late, depart = hm(r[ix['after']]), hm(r[ix['late']]), hm(r[ix['dep']])
        except ValueError as e:
            raise SystemExit(f"Line {line_no} ({name}): {e}")
        customers.append(dict(
            name=name, city=city, pref=team if team else None,
            minutes=int(r[ix['mins']]), after=after, late=late,
            depart=depart, type=r[ix['type']].strip().upper()))
    for c in customers:
        if c['type'] not in cfg['type_weights']:
            raise SystemExit(f"{c['name']}: Type '{c['type']}' must be one of {list(cfg['type_weights'])}.")
    return customers


def load_last():
    try:
        with open(LAST_RUN) as f:
            return json.load(f)
    except Exception:
        return {}


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def get_teams(args, cfg):
    """Which of the company's teams work today, and how many people are on each (2 or 3)."""
    roster = [str(t) for t in cfg['all_teams']]
    last = load_last()
    teams_text, large_text = args.teams, args.large
    if teams_text is None:
        if not sys.stdin.isatty():
            teams_text = ",".join(last.get('teams', []))
            large_text = ",".join(last.get('large', [])) if large_text is None else large_text
            if not teams_text:
                raise SystemExit("Pass --teams and --large, or run from a terminal so I can ask.")
        else:
            print(f"Teams: {', '.join(roster)}")
            teams_text = ask("Which teams are working today (comma separated, or 'all')",
                             ",".join(last.get('teams', [])) or None)
            if teams_text.strip().lower() == 'all':
                teams_text = ",".join(roster)
            chosen = [t.strip() for t in teams_text.split(',') if t.strip()]
            large_text = ask(f"Of those, which have {cfg['large_team_size']} people? "
                             f"(the rest have {cfg['base_team_size']}; blank = none)",
                             ",".join(t for t in last.get('large', []) if t in chosen) or None)
    if teams_text.strip().lower() == 'all':
        teams_text = ",".join(roster)
    labels = [t.strip() for t in teams_text.split(',') if t.strip()]
    large = [t.strip() for t in (large_text or '').split(',') if t.strip()]
    for t in labels:
        if t not in roster:
            raise SystemExit(f"Team '{t}' isn't one of your teams ({', '.join(roster)}). Edit all_teams in config.json to change the list.")
    if len(set(labels)) != len(labels):
        raise SystemExit("Each team can only be listed once.")
    for t in large:
        if t not in labels:
            raise SystemExit(f"Team {t} is marked as {cfg['large_team_size']} people but isn't working today ({', '.join(labels)}).")
    with open(LAST_RUN, 'w') as f:
        json.dump({'teams': labels, 'large': large}, f)
    return {t: (cfg['large_team_size'] if t in large else cfg['base_team_size']) for t in labels}


# ----------------------------------------------------------------------------- output
def describe(problem, assign, travel_source, target):
    p = problem
    lines_md = []
    rows_csv = []
    notes_total = 0
    moved = 0
    total_travel = 0
    finishes = []
    day = DAY_NAMES[target.weekday()]
    head = (f"# Schedule for {day} {target.strftime('%B')} {target.day}, {target.year}\n\n"
            f"Travel times: {travel_source}. Teams leave {fmt(p.day_start)}, no waiting between stops, "
            f"back by {fmt(p.day_end)}.\n")
    lines_md.append(head)
    lines_md.append("| Team (people) | Stop | Travel | Start–End | Time in home | Customer | Notes |")
    lines_md.append("|---|---|---|---|---|---|---|")
    for t in p.teams:
        route = tuple(assign[t])
        label = f"**Team {t}** ({p.team_sizes[t]})"
        if not route:
            lines_md.append(f"| {label} | – | | | | no customers | |")
            continue
        pen, travel, back, stops, ret = p.run_team(t, route)
        total_travel += travel
        for n, (i, leg, start, end, early, late, over) in enumerate(stops, 1):
            c = p.customers[i]
            notes = []
            if early:
                notes.append(f"arrives {early} min before 'Arrive after'")
            if late:
                notes.append(f"{late} min after 'Arrive no later than'")
            if over:
                notes.append(f"{over} min past 'Depart before'")
            if notes:
                notes_total += 1
            if c['pref'] and c['pref'] != t:
                moved += 1
                if c['pref'] in p.team_sizes:
                    notes.insert(0, f"prefers Team {c['pref']}; moved to Team {t}")
                else:
                    notes.insert(0, f"prefers Team {c['pref']} (not working today)")
            who = f"{c['name']} ({c['city'].split(',')[0]}, {c['type']})"
            note = "; ".join(notes)
            lines_md.append(f"| {label if n == 1 else ''} | {n} | {leg} | {fmt(start)}–{fmt(end)} | "
                            f"{end - start} | {who} | {('**' + note + '**') if note else ''} |")
            rows_csv.append([t, p.team_sizes[t], n, c['name'], c['city'], c['type'], leg,
                             fmt(start), fmt(end), end - start, note])
        flag = "" if back <= p.day_end else " **after cutoff**"
        last_out = stops[-1][3]
        finishes.append((t, last_out, any(p.customers[i]['far'] for i in route)))
        lines_md.append(f"| | Return | {ret} | back {fmt(back)}{flag} | | | |")
        rows_csv.append([t, p.team_sizes[t], 'Return', '', p.office, '', ret, fmt(back), '', '', ''])
    if finishes:
        target = p.finish_target
        bal = [(t, f) for t, f, far in finishes if not far]
        lines_md.append("\nLast customer finished: " + ", ".join(
            f"Team {t} {fmt(f)}{' (outlying region)' if far else ''}" for t, f, far in finishes) + ".")
        if len(bal) > 1:
            lines_md.append(f"Balanced teams (only towns within {p.radius:g} miles of the office): "
                            f"spread {max(f for _, f in bal) - min(f for _, f in bal)} min"
                            + (f"; after {fmt(target)}: " + ", ".join(t for t, f in bal if f > target) if any(f > target for _, f in bal) else "")
                            + ". Teams with an outlying region keep their route.")
    lines_md.append(f"\nTotal driving: {total_travel} minutes. "
                    + ("Every customer's time window is met." if notes_total == 0
                       else f"{notes_total} stop(s) outside a customer window (bold above)."))
    if moved:
        lines_md.append(f"{moved} customer(s) are not on their preferred team (see the Notes column).")
    return "\n".join(lines_md) + "\n", rows_csv, total_travel, notes_total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--customers', default=os.path.join(HERE, 'customers.csv'))
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--date', help='YYYY-MM-DD. Default: tomorrow.')
    ap.add_argument('--teams', help='Team numbers/names, e.g. 1,2,3,6')
    ap.add_argument('--large', help='Which of those teams have the larger crew, e.g. 1,2')
    ap.add_argument('--seconds', type=int, help='How long to search (default from config.json).')
    ap.add_argument('--estimate', action='store_true', help='Skip Google and use the built-in travel estimate.')
    ap.add_argument('--workers', type=int, help='CPU processes to use (default: all but one).')
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    tz = ZoneInfo(cfg['timezone'])
    target = dt.date.fromisoformat(args.date) if args.date else (dt.datetime.now(tz).date() + dt.timedelta(days=1))

    customers = load_customers(args.customers, cfg)
    team_sizes = get_teams(args, cfg)

    cities = [cfg['office']] + sorted({c['city'] for c in customers if c['city'] != cfg['office']})
    travel, source = build_travel(cities, cfg, target, force_estimate=args.estimate)
    print(f"\nScheduling {len(customers)} customers on {len(team_sizes)} teams for "
          f"{DAY_NAMES[target.weekday()]} {target} ({source}).")

    problem = Problem(customers, team_sizes, travel, cfg)
    if problem.priority_warning:
        print("Note: " + problem.priority_warning)
    seconds = args.seconds or cfg['search_seconds']
    print(f"Searching for the best routes ({seconds} seconds)...")
    cost, assign = solve(problem, seconds=seconds, workers=args.workers)
    if cost >= 1_000_000:
        print("Could not satisfy the basic rules (every team used / Type I and O on their allowed teams). Check the team setup.")

    md, rows, total_travel, outside = describe(problem, assign, source, target)
    print("\n" + md)

    out_dir = os.path.join(HERE, 'output')
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"schedule_{target.isoformat()}")
    with open(base + '.md', 'w', encoding='utf-8') as f:
        f.write(md)
    with open(base + '.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['Team', 'Team size', 'Stop', 'Customer', 'City', 'Type', 'Travel min', 'Start',
                    'End', 'Time in home', 'Notes'])
        w.writerows(rows)
    print(f"Saved {base}.md and {base}.csv")


if __name__ == '__main__':
    main()
