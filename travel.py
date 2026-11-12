"""Travel-time model.

Travel time between two cities depends on the day of the week and the time of day
the team is on the road.  Two sources are supported:

1. Google Maps Routes API (real traffic predictions for the target date).  Used when an
   API key is available (environment variable named in config.json, default
   GOOGLE_MAPS_API_KEY).  Results are cached per date in ./cache/.
2. Built-in estimate (no internet / key needed).  Typical weekday drive times between
   the cities Molly Maid already serves, a distance model for any other city in
   city_coords.json, plus a day-of-week and rush-hour adjustment taken from config.json.

Either way the result is a table: for each departure bin (half hour for the estimate,
hourly for Google) the minutes between every pair of cities.
"""
import datetime as dt
import json
import math
import os
import urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))

# Typical weekday drive minutes (with ordinary traffic) between cities already served.
_CODES = {'SH': 'shorewood', 'HG': 'homer glen', 'RS': 'riverside', 'WC': 'westchester',
          'CS': 'countryside', 'JO': 'joliet', 'EL': 'elwood', 'RO': 'romeoville',
          'BR': 'braidwood', 'LO': 'lockport', 'NL': 'new lenox', 'BE': 'beverly'}
_RAW = """SH HG 35;SH RS 55;SH WC 55;SH CS 45;SH JO 15;SH EL 20;SH RO 25;SH BR 30;SH LO 22;SH NL 25;SH BE 55;
HG RS 30;HG WC 30;HG CS 25;HG JO 25;HG EL 35;HG RO 20;HG BR 50;HG LO 18;HG NL 20;HG BE 30;
RS WC 12;RS CS 12;RS JO 40;RS EL 55;RS RO 35;RS BR 70;RS LO 35;RS NL 40;RS BE 25;
WC CS 12;WC JO 40;WC EL 55;WC RO 35;WC BR 70;WC LO 35;WC NL 40;WC BE 30;
CS JO 35;CS EL 50;CS RO 28;CS BR 65;CS LO 28;CS NL 33;CS BE 30;
JO EL 15;JO RO 15;JO BR 30;JO LO 12;JO NL 12;JO BE 45;
EL RO 25;EL BR 20;EL LO 25;EL NL 22;EL BE 55;
RO BR 40;RO LO 12;RO NL 25;RO BE 40;
BR LO 40;BR NL 35;BR BE 65;
LO NL 20;LO BE 40;NL BE 40"""
BASE = {}
for _p in _RAW.replace('\n', '').split(';'):
    _a, _b, _m = _p.split()
    BASE[frozenset((_CODES[_a], _CODES[_b]))] = int(_m)

# Distance model fitted to the table above: minutes = A + B * straight-line miles.
MODEL_A, MODEL_B = 6.9, 1.457

DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def city_key(city):
    """'Homer Glen, IL' -> 'homer glen'."""
    return city.split(',')[0].strip().lower()


def _haversine(a, b):
    r = 3958.8
    la1, lo1 = map(math.radians, a)
    la2, lo2 = map(math.radians, b)
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _hm(text):
    h, m = text.split(':')
    return int(h) * 60 + int(m)


class TravelTable:
    """Picklable table of travel minutes: minutes(a, b, depart_minute)."""

    def __init__(self, bin_minutes, table, same_city):
        self.bin_minutes = bin_minutes
        self.table = table          # {bin_index: {(a, b): minutes}}
        self.same_city = same_city  # {bin_index: minutes}
        self.lo = min(table)
        self.hi = max(table)

    def minutes(self, a, b, depart):
        k = int(depart // self.bin_minutes)
        k = max(self.lo, min(self.hi, k))
        if a == b:
            return self.same_city[k]
        return self.table[k][(a, b)]


def _extra(cfg, weekday, minute):
    """Day-of-week plus rush-hour adjustment (estimate mode and same-city legs)."""
    t = cfg['traffic']
    extra = t['day_extra_minutes'][DAY_NAMES[weekday]]
    for start, end in t['rush_windows']:
        if _hm(start) <= minute < _hm(end):
            extra += t['rush_extra_minutes']
            break
    return extra


def _load_coords():
    path = os.path.join(HERE, 'city_coords.json')
    with open(path) as f:
        data = json.load(f)
    return {k.lower(): tuple(v) for k, v in data.items() if not k.startswith('_')}


def _baseline(a, b, coords):
    key = frozenset((city_key(a), city_key(b)))
    if key in BASE:
        return BASE[key]
    ca, cb = coords.get(city_key(a)), coords.get(city_key(b))
    if ca is None or cb is None:
        missing = a if ca is None else b
        raise SystemExit(
            f"No coordinates for '{missing}'. Add it to city_coords.json (latitude, longitude), "
            f"or set the {os.environ.get('SCHEDULE_KEY_NAME', 'GOOGLE_MAPS_API_KEY')} environment variable "
            "to use live traffic data.")
    return MODEL_A + MODEL_B * _haversine(ca, cb)


def _estimate_table(cities, cfg, target):
    coords = _load_coords()
    bin_minutes = 30
    table, same = {}, {}
    base_same = cfg['same_city_minutes']
    weekday = target.weekday()
    for k in range(6 * 2, 21 * 2 + 1):          # 06:00 .. 21:00
        minute = k * bin_minutes
        extra = _extra(cfg, weekday, minute)
        t = {}
        for a in cities:
            for b in cities:
                if a != b:
                    t[(a, b)] = int(round(_baseline(a, b, coords) + extra))
        table[k] = t
        same[k] = int(round(base_same + extra))
    return TravelTable(bin_minutes, table, same)


def _google_table(cities, cfg, target, api_key):
    tz = ZoneInfo(cfg['timezone'])
    cache_dir = os.path.join(HERE, 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"travel_{target.isoformat()}.json")
    cache = {}
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            cache = json.load(f)
    bin_minutes = 60
    table, same = {}, {}
    weekday = target.weekday()
    n = len(cities)
    chunk = max(1, 600 // n)
    for hour in range(6, 21):
        key = f"{hour}|" + "|".join(cities)
        if key not in cache:
            depart = dt.datetime.combine(target, dt.time(hour, 0), tzinfo=tz)
            depart_utc = depart.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            result = {}
            for s in range(0, n, chunk):
                origins = cities[s:s + chunk]
                body = {
                    'origins': [{'waypoint': {'address': c}} for c in origins],
                    'destinations': [{'waypoint': {'address': c}} for c in cities],
                    'travelMode': 'DRIVE',
                    'routingPreference': 'TRAFFIC_AWARE',
                    'departureTime': depart_utc,
                }
                req = urllib.request.Request(
                    'https://routes.googleapis.com/distance_matrix/v2:computeRouteMatrix',
                    data=json.dumps(body).encode(),
                    headers={'Content-Type': 'application/json', 'X-Goog-Api-Key': api_key,
                             'X-Goog-FieldMask': 'originIndex,destinationIndex,duration,condition'})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    rows = json.loads(resp.read().decode())
                for row in rows:
                    if row.get('condition') != 'ROUTE_EXISTS':
                        continue
                    a = origins[row['originIndex']]
                    b = cities[row['destinationIndex']]
                    secs = float(row['duration'].rstrip('s'))
                    result[f"{a}>>{b}"] = secs / 60.0
            cache[key] = result
            with open(cache_file, 'w') as f:
                json.dump(cache, f)
        k = hour
        t = {}
        for a in cities:
            for b in cities:
                if a != b:
                    mins = cache[key].get(f"{a}>>{b}")
                    if mins is None:
                        raise SystemExit(f"Google returned no route from {a} to {b}.")
                    t[(a, b)] = int(math.ceil(mins))
        table[k] = t
        same[k] = int(round(cfg['same_city_minutes'] + _extra(cfg, weekday, hour * 60)))
    return TravelTable(bin_minutes, table, same)


def build_travel(cities, cfg, target, force_estimate=False):
    """Return (TravelTable, source description)."""
    key_name = cfg['traffic'].get('google_api_key_env', 'GOOGLE_MAPS_API_KEY')
    os.environ['SCHEDULE_KEY_NAME'] = key_name
    api_key = os.environ.get(key_name)
    day = DAY_NAMES[target.weekday()]
    if api_key and not force_estimate:
        try:
            return _google_table(cities, cfg, target, api_key), f"Google traffic predictions for {day} {target}"
        except Exception as exc:  # fall back, but say so
            print(f"  (Google travel times failed: {exc}. Using the built-in estimate instead.)")
    return _estimate_table(cities, cfg, target), f"built-in estimate for {day} {target}"
