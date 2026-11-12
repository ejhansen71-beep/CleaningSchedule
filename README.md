# CleaningSchedule

Builds tomorrow's routes for the cleaning teams: who goes where, in what order, with start/end
times, travel times and time in each home, honoring each customer's time window.

## Run it
Open Terminal in this folder and run:

    python3 build_schedule.py

There are 6 teams (1-6, listed in `all_teams` in `config.json`). Not all work every day, and each
can have 2 or 3 people on a given day, so it asks:
1. **Which teams are working today** (e.g. 1,2,4,6, or `all`)
2. **Which of those have 3 people** (e.g. 1,2) - the rest have 2

and then schedules **tomorrow**. Your last answers are offered as defaults next time.
No questions asked: `python3 build_schedule.py --teams 1,2,4,6 --large 1,2`
A different day: `--date 2026-11-16`. Longer search: `--seconds 180`.

Results print on screen and are saved in `output/schedule_<date>.md` and `.csv`.
Python 3.9+ only, no extra packages.

## Customer file (`customers.csv`)
Your real `customers.csv` is not stored in git (it has customer names). A sample is in
`customers.example.csv`; copy it to `customers.csv` to start a new copy of this project.

Replace it each time (or point to another with `--customers file.csv`). Columns:
Name, City, Team, Time In Home, Arrive After, Arrive No Later than, Depart before, Type.
* **City**: `Homer Glen, IL` (state optional; defaults to IL).
* **Team**: the client's preferred team. Blank = any team. A customer is put on their preferred team
  unless that would break a time window; the team is changed before any arrival time is, and the
  change is noted in the Notes column. (Type I and O customers still only go on their allowed teams.)
* **Time In Home**: minutes for a **2-person** team. A 3-person team's time is multiplied by 2/3.
* **Type**: I, O, W, A or M. Priority: I and O (same rank) > W > A > M. Between customers of the
  same rank, the one with the longer Time In Home takes precedence.
* You can paste straight from Excel (tab separated) into the file too.

## Rules it follows (change them in `config.json`)
* Office is Shorewood, IL. Every team leaves at 08:00, drives straight from stop to stop (no
  waiting), and must be back by 17:30.
* Arriving before "Arrive After", starting after "Arrive No Later than", or finishing after
  "Depart before" counts as a violation, in minutes, weighted I=8, O=8, W=4, A=2, M=1. It minimizes
  violations first, then total driving time.
* Every team that is working today gets work. Type I and O customers can only go on Teams 1, 2, 4 or 6
  (`priority_allowed_teams` in `config.json`).
* Same-city legs are 10 minutes plus the day/rush adjustment.

## Travel times follow the day of the week and time of day
The schedule date's weekday and each team's departure time set the drive time.
* **With a Google Maps API key** (set environment variable `GOOGLE_MAPS_API_KEY`, Routes API
  enabled): uses Google's predicted traffic for that date, hour by hour. Cached in `cache/`.
* **Without a key**: a built-in estimate: typical weekday minutes between cities, plus the day
  adjustment (Friday +10, Thursday +3, weekends -3) and +5 in rush hours (07:00-09:00 and
  15:30-18:30). All of that is in `config.json` under `traffic`.
  New cities need a line in `city_coords.json` (latitude, longitude).
`--estimate` forces the built-in estimate even when a key is set.

## Files
build_schedule.py (run this), solver.py (route search), travel.py (travel times),
config.json, customers.csv, city_coords.json.

This folder is a git repository. Each run's `output/`, `cache/` and `last_run.json` are not tracked.
