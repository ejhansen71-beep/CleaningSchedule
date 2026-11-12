"""Route/assignment search for the cleaning teams.

Rules (all configurable in config.json):
  * Every team leaves the office at day_start and every team must be back by day_end.
  * Teams drive straight from stop to stop: no waiting.  Arriving before "Arrive After",
    starting after "Arrive No Later Than", or finishing after "Depart Before" are
    violations (counted in minutes, weighted by customer type: I > W > A > M).
  * Time In Home in the customer file is for a team of base_team_size people; a larger team
    finishes proportionally faster (time * base / team size).
  * A customer's Team number is a preference: they are scheduled on that team unless that would
    cause a time-window violation (a team change costs less than any window violation).
    A blank Team means any team.
  * Priority types (I and O, same rank) may only go on the teams listed in
    priority_allowed_teams; every team must get work (optional).
  * Within the same rank, the customer with the longer Time In Home takes precedence
    (tiny extra weight per minute in home).
Objective: fewest weighted violation minutes first, then the least total driving.
"""
import math
import multiprocessing as mp
import random
import time

BIG = 1_000_000


def _hm(text):
    h, m = text.split(':')
    return int(h) * 60 + int(m)


class Problem:
    def __init__(self, customers, team_sizes, travel, cfg):
        self.customers = customers
        self.team_sizes = team_sizes          # {label: people}
        self.teams = list(team_sizes)
        self.travel = travel
        self.office = cfg['office']
        self.day_start = _hm(cfg['day_start'])
        self.day_end = _hm(cfg['day_end'])
        self.base = cfg['base_team_size']
        self.weights = cfg['type_weights']
        self.use_all = cfg['use_all_teams']
        self.pref_pen = cfg.get('team_preference_penalty', 500)
        self.memo = {}
        tie = cfg.get('same_rank_tiebreak_per_minute', 0)
        for c in customers:
            c['w'] = self.weights[c['type']] + c['minutes'] * tie
        self.priority_types = set(cfg.get('priority_types', []))
        allowed = [t for t in self.teams if t in [str(x) for x in cfg.get('priority_allowed_teams', [])]]
        self.priority_warning = None
        if self.priority_types and not allowed:
            self.priority_warning = ("None of the teams working today are in priority_allowed_teams; "
                                     "Type " + "/".join(sorted(self.priority_types)) + " customers may go on any team.")
            allowed = list(self.teams)
        self.priority_teams = allowed
        self.priority_set = set(allowed)

    def duration(self, cust, team):
        return int(math.floor(cust['minutes'] * self.base / self.team_sizes[team] + 0.5))

    def run_team(self, team, route):
        """Simulate a team's day. Returns (weighted_violation, travel_minutes, back_minute, stops)."""
        key = (team, route)
        hit = self.memo.get(key)
        if hit is not None:
            return hit
        cur, prev = self.day_start, self.office
        pen = travel = 0
        stops = []
        for i in route:
            c = self.customers[i]
            leg = self.travel.minutes(prev, c['city'], cur)
            travel += leg
            start = cur + leg
            end = start + self.duration(c, team)
            early = max(0, c['after'] - start)
            late = max(0, start - c['late'])
            over = max(0, end - c['depart'])
            w = c['w']
            pen += (early + late + over) * w
            stops.append((i, leg, start, end, early, late, over))
            cur, prev = end, c['city']
        leg = self.travel.minutes(prev, self.office, cur)
        travel += leg
        back = cur + leg
        if back > self.day_end:
            pen += (back - self.day_end) * 100
        res = (pen, travel, back, stops, leg)
        if len(self.memo) < 400000:
            self.memo[key] = res
        return res

    def cost(self, assign):
        total = 0
        for team in self.teams:
            route = assign[team]
            if not route:
                if self.use_all:
                    total += BIG
                continue
            pen, travel, _, _, _ = self.run_team(team, tuple(route))
            total += pen * 1000 + travel
            for i in route:
                pref = self.customers[i]['pref']
                if pref and pref != team:
                    total += self.pref_pen
            if team not in self.priority_set:
                if any(self.customers[i]['type'] in self.priority_types for i in route):
                    total += BIG
        return total


def _initial(p, rng):
    a = {t: [] for t in p.teams}
    for i, c in enumerate(p.customers):
        if c['pref'] in p.team_sizes and (c['type'] not in p.priority_types or c['pref'] in p.priority_set):
            t = c['pref']
        elif c['type'] in p.priority_types:
            t = rng.choice(p.priority_teams)
        else:
            t = rng.choice(p.teams)
        a[t].append(i)
    for t in a:
        rng.shuffle(a[t])
    return a


def _neighbor(p, a, rng):
    b = {t: list(v) for t, v in a.items()}
    r = rng.random()
    t1 = rng.choice(p.teams)
    if r < 0.45:                                  # move one customer
        if not b[t1]:
            return None
        k = rng.randrange(len(b[t1]))
        i = b[t1].pop(k)
        c = p.customers[i]
        if c['pref'] in p.team_sizes and rng.random() < 0.3:
            t2 = c['pref']
        elif c['type'] in p.priority_types:
            t2 = rng.choice(p.priority_teams)
        else:
            t2 = rng.choice(p.teams)
        b[t2].insert(rng.randint(0, len(b[t2])), i)
    elif r < 0.65:                                # swap customers between two teams
        if len(p.teams) < 2:
            return None
        t2 = rng.choice([t for t in p.teams if t != t1])
        if not b[t1] or not b[t2]:
            return None
        x, y = rng.randrange(len(b[t1])), rng.randrange(len(b[t2]))
        b[t1][x], b[t2][y] = b[t2][y], b[t1][x]
    elif r < 0.85:                                # swap stops inside a team
        if len(b[t1]) < 2:
            return None
        x, y = rng.sample(range(len(b[t1])), 2)
        b[t1][x], b[t1][y] = b[t1][y], b[t1][x]
    else:                                         # reverse a stretch of a route
        if len(b[t1]) < 3:
            return None
        x, y = sorted(rng.sample(range(len(b[t1])), 2))
        b[t1][x:y + 1] = reversed(b[t1][x:y + 1])
    return b


def _polish(p, a, cost):
    """Greedy improvement: apply any single move/swap that lowers the cost."""
    improved = True
    while improved:
        improved = False
        for t1 in p.teams:
            for x in range(len(a[t1])):
                i = a[t1][x]
                for t2 in p.teams:
                    for pos in range(len(a[t2]) + 1):
                        if t1 == t2 and pos in (x, x + 1):
                            continue
                        b = {t: list(v) for t, v in a.items()}
                        b[t1].pop(x)
                        pos2 = pos - 1 if (t1 == t2 and pos > x) else pos
                        b[t2].insert(pos2, i)
                        c2 = p.cost(b)
                        if c2 < cost:
                            a, cost, improved = b, c2, True
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                break
    return a, cost


def _worker(args):
    problem, seed, seconds, iters = args
    rng = random.Random(seed)
    deadline = time.time() + seconds
    best, best_cost = None, float('inf')
    while time.time() < deadline:
        a = _initial(problem, rng)
        cur = problem.cost(a)
        for it in range(iters):
            temp = 3000 * (1 - it / iters) + 1
            b = _neighbor(problem, a, rng)
            if b is None:
                continue
            c2 = problem.cost(b)
            if c2 < cur or rng.random() < math.exp((cur - c2) / temp):
                a, cur = b, c2
                if cur < best_cost:
                    best, best_cost = {t: list(v) for t, v in a.items()}, cur
        if len(problem.memo) > 350000:
            problem.memo.clear()
    best, best_cost = _polish(problem, best, best_cost)
    return best_cost, best


def solve(problem, seconds=90, workers=None):
    workers = workers or max(1, (mp.cpu_count() or 2) - 1)
    problem.memo.clear()
    jobs = [(problem, 1000 + k, seconds, 15000) for k in range(workers)]
    if workers == 1:
        results = [_worker(jobs[0])]
    else:
        with mp.Pool(workers) as pool:
            results = pool.map(_worker, jobs)
    results.sort(key=lambda r: r[0])
    cost, best = results[0]
    return cost, best
