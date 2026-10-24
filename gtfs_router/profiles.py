"""Who travels on the vehicles, from a MATSim simulation: the distributions of the attributes of the simulated
passengers (age, sex, trip purpose, employment, travelcard, car availability, income, home municipality) by line, pair
of stations and period of the day (morning peak, evening peak, off-peak).

Each public transport leg of output_legs.csv.gz is split into the sections of its route (output_transitSchedule.xml.gz)
between its boarding and alighting stops; its person (output_persons.csv.gz) and the purpose of its trip (the activity
at the end of output_trips.csv.gz) count on each section. A ride adds up its sections: the people on board with the
traveller, weighted by the sections they share. With too few simulated people (MIN_PEOPLE), the same sections all day,
else the whole line in the period (each leg once), else the whole line all day. The lines and stations are matched as
for the crowding (line name, station codes)."""
import gzip
import hashlib
import logging
import os
import pickle
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .crowding import _seconds, _station

logger = logging.getLogger(__name__)
VERSION = 1
FILES = ("output_legs.csv.gz", "output_trips.csv.gz", "output_persons.csv.gz", "output_transitSchedule.xml.gz")
MIN_PEOPLE = 30   # simulated people at least for a profile
PERIODS = ("Morning peak", "Off-peak", "Evening peak")


def period_of(seconds):
    """0 morning peak (06:00-09:00), 2 evening peak (16:00-19:00), 1 off-peak."""
    hour = int(seconds) // 3600 % 24
    return 0 if 6 <= hour < 9 else 2 if 16 <= hour < 19 else 1


# the dimensions: (name, [category labels]), and how the code of each person or trip is found
DIMENSIONS = [
    ("Age", ["0–15", "16–24", "25–44", "45–64", "65+"]),
    ("Sex", ["Women", "Men"]),
    ("Trip purpose", ["Work", "Education", "Shopping", "Leisure", "Going home", "Other"]),
    ("Employment", ["Employed", "Not employed"]),
    ("Travelcard", ["GA", "Half-fare", "Regional pass", "Regional + half-fare", "None"]),
    ("Car available", ["Always", "Never"]),
    ("Income per person (CHF/month)", ["< 3000", "3000–4999", "5000–6999", "7000+"]),
    ("Home", ["Urban core", "Urban", "Suburban", "Rural", "Abroad"]),
]
OFFSETS = np.cumsum([0] + [len(c) for _, c in DIMENSIONS])
PURPOSES = {"work": 0, "work_secondary": 0, "education": 1, "education_secondary": 1, "shop": 2, "leisure": 3,
            "home": 4, "home_secondary": 4}


def _codes(persons, purpose):
    """The category of each dimension (-1: unknown), columns in the order of DIMENSIONS."""
    age = pd.to_numeric(persons["age"], errors = "coerce")
    income = pd.to_numeric(persons["incomePerCapita"], errors = "coerce")
    pick = lambda values, mapping: values.map(mapping).fillna(-1).astype(int).to_numpy()
    return np.column_stack([
        np.where(age.isna(), -1, np.digitize(age.fillna(0), [16, 25, 45, 65])),
        pick(persons["sex"], {"f": 0, "m": 1}),
        purpose.map(PURPOSES).fillna(5).astype(int).to_numpy(),
        pick(persons["employed"], {"true": 0, "false": 1}),
        pick(persons["subscriptions"], {"GA": 0, "HT": 1, "VA": 2, "VA+HT": 3, "none": 4}),
        pick(persons["carAvail"], {"always": 0, "never": 1}),
        np.where(income.isna(), -1, np.digitize(income.fillna(0), [3000, 5000, 7000])),
        pick(persons["municipalityType"], {"urbancore": 0, "urban": 1, "suburban": 2, "rural": 3, "crossborder": 4}),
    ])


@dataclass
class Profiles:
    by_pair: dict       # (line, from station, to station, period) -> (people on board, people boarding, counts by category)
    by_line: dict       # (line, period) -> (legs, counts by category)
    overall: np.ndarray  # period x categories: all the public transport legs
    source: str

    def ride(self, net, ride):
        """The profile of a ride: {period, all_day, scope, people, dimensions: [{name, items: [{label, share, average}]}]},
        or None without enough simulated people. The first with MIN_PEOPLE: the sections of the ride in its period, the
        same sections all day, the whole line in the period, the whole line all day."""
        line = str(net.routes["name"].iat[net.trip_route[ride.trip]])
        period = period_of(ride.dep)
        stations = [_station(net.stop_ids[s]) for s in ride.stops]
        sections = [(a, b) for a, b in zip(stations, stations[1:]) if a != b]
        ride_counts, ride_people = np.zeros((len(PERIODS), OFFSETS[-1])), np.zeros(len(PERIODS))
        for p in range(len(PERIODS)):
            for a, b in sections:
                found = self.by_pair.get((line, a, b, p))
                if found is not None:
                    ride_counts[p] += found[2]
                    ride_people[p] += found[0] if ride_people[p] == 0 else found[1]   # on board at the first section, then boarding
        line_counts, line_people = np.zeros((len(PERIODS), OFFSETS[-1])), np.zeros(len(PERIODS))
        for p in range(len(PERIODS)):
            found = self.by_line.get((line, p))
            if found is not None:
                line_people[p], line_counts[p] = found[0], found[1]
        for scope, counts, people in (("ride", ride_counts, ride_people), ("line", line_counts, line_people)):
            if people[period] >= MIN_PEOPLE:
                return self._result(period, False, scope, people[period], counts[period], self.overall[period])
            if people.sum() >= MIN_PEOPLE:
                return self._result(period, True, scope, people.sum(), counts.sum(axis = 0), self.overall.sum(axis = 0))
        return None

    @staticmethod
    def _result(period, all_day, scope, people, counts, overall):
        return {"period": PERIODS[period], "all_day": all_day, "scope": scope, "people": int(people),
                "dimensions": _shares(counts, overall)}


def _shares(counts, overall):
    result = []
    for d, (name, labels) in enumerate(DIMENSIONS):
        part, base = counts[OFFSETS[d]:OFFSETS[d + 1]], overall[OFFSETS[d]:OFFSETS[d + 1]]
        if part.sum() == 0:
            continue
        result.append({"name": name, "items": [{"label": label, "share": round(float(s), 3), "average": round(float(a), 3)}
                                               for label, s, a in zip(labels, part / part.sum(), base / max(base.sum(), 1))]})
    return result


def build_profiles(folder, cache_dir = None):
    """The profiles of the simulation of the output folder, or None without its legs, trips and persons."""
    paths = [os.path.join(folder, f) for f in FILES]
    if not all(os.path.exists(p) for p in paths):
        return None
    if cache_dir:
        stamp = f"{VERSION}|{MIN_PEOPLE}|" + "|".join(f"{os.path.abspath(p)}:{os.path.getmtime(p)}" for p in paths)
        cache = os.path.join(cache_dir, f"profiles_{hashlib.md5(stamp.encode()).hexdigest()[:10]}.pkl")
        if os.path.exists(cache):
            logger.info(f"Profiles from the cache {cache}")
            with open(cache, "rb") as f:
                return pickle.load(f)

    logger.info(f"Profiles of the travellers of the simulation {folder}")
    legs_path, trips_path, persons_path, schedule_path = paths
    legs = pd.read_csv(legs_path, sep = ";", dtype = str, usecols = ["person", "trip_id", "dep_time", "wait_time", "access_stop_id",
                                                                    "egress_stop_id", "transit_route"])
    legs = legs[legs["transit_route"].notna()].reset_index(drop = True)
    trips = pd.read_csv(trips_path, sep = ";", dtype = str, usecols = ["trip_id", "end_activity_type"]).set_index("trip_id")
    persons = pd.read_csv(persons_path, sep = ";", dtype = str).drop_duplicates("person").set_index("person")
    persons = persons.reindex(legs["person"])
    codes = _codes(persons, trips["end_activity_type"].reindex(legs["trip_id"]).reset_index(drop = True))
    legs["period"] = [period_of(_seconds(d) + _seconds(w)) for d, w in zip(legs["dep_time"], legs["wait_time"].fillna("00:00:00"))]
    logger.info(f"  {len(legs)} public transport legs")

    # the stops of each route, and its line
    stops, line_name, route = [], None, None
    lines = {}
    for event, el in ET.iterparse(gzip.open(schedule_path), events = ("start", "end")):
        if event == "start" and el.tag == "transitLine":
            line_name = el.get("name") or el.get("id")
        elif event == "start" and el.tag == "transitRoute":
            route, k = el.get("id"), 0
            lines[route] = line_name
        elif event == "end" and el.tag == "stop" and route is not None:
            stops.append((route, k, el.get("refId").split(".link:")[0]))
            k += 1
        elif event == "end" and el.tag == "transitRoute":
            el.clear()
    stops = pd.DataFrame(stops, columns = ["route", "k", "stop"])

    # the positions of the boarding and alighting stops in the route (the first alighting after the boarding)
    legs["leg"] = np.arange(len(legs))
    legs["board"], legs["alight"] = legs["access_stop_id"].str.split(".link:").str[0], legs["egress_stop_id"].str.split(".link:").str[0]
    on = legs[["leg", "transit_route", "board"]].merge(stops, left_on = ["transit_route", "board"], right_on = ["route", "stop"])
    on = on.groupby("leg")["k"].min()
    off = legs[["leg", "transit_route", "alight"]].merge(stops, left_on = ["transit_route", "alight"], right_on = ["route", "stop"])
    off = off.assign(first = off["leg"].map(on))
    off = off[off["k"] > off["first"]].groupby("leg")["k"].min()
    legs["k0"], legs["k1"] = legs["leg"].map(on), legs["leg"].map(off)
    ok = legs["k0"].notna() & legs["k1"].notna()
    logger.info(f"  {ok.sum()} legs matched to the stops of their route")
    matched = legs[ok]
    line = matched["transit_route"].map(lines).to_numpy()

    # the sections of each leg: from the stop k to k + 1, k0 <= k < k1
    n = (matched["k1"] - matched["k0"]).astype(int).to_numpy()
    which = np.repeat(np.arange(len(matched)), n)
    k = np.repeat(matched["k0"].astype(int).to_numpy(), n) + (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n))
    first = k == np.repeat(matched["k0"].astype(int).to_numpy(), n)
    route_stops = stops.set_index(["route", "k"])["stop"]
    routes = matched["transit_route"].to_numpy()[which]
    a = route_stops.reindex(pd.MultiIndex.from_arrays([routes, k])).map(_station).to_numpy()
    b = route_stops.reindex(pd.MultiIndex.from_arrays([routes, k + 1])).map(_station).to_numpy()
    sections = pd.DataFrame({"line": line[which], "a": a, "b": b, "period": matched["period"].to_numpy()[which]})
    keep = (sections["a"] != sections["b"]).to_numpy()
    sections, which, first = sections[keep], which[keep], first[keep]
    key, keys = pd.MultiIndex.from_frame(sections).factorize()
    leg_codes = codes[matched["leg"].to_numpy()]

    def add_up(rows, groups, n_groups):
        counts = np.zeros((n_groups, OFFSETS[-1]), np.int32)
        for d in range(len(DIMENSIONS)):
            c = leg_codes[rows, d]
            np.add.at(counts, (groups[c >= 0], OFFSETS[d] + c[c >= 0]), 1)
        return counts

    counts = add_up(which, key, len(keys))
    on_board = np.bincount(key, minlength = len(keys))
    boarding = np.bincount(key, weights = first, minlength = len(keys)).astype(int)
    by_pair = {tuple(kk): (int(p), int(q), c) for kk, p, q, c in zip(keys, on_board, boarding, counts)}

    line_key, line_keys = pd.MultiIndex.from_arrays([line, matched["period"].to_numpy()]).factorize()
    line_counts = add_up(np.arange(len(matched)), line_key, len(line_keys))
    n_legs = np.bincount(line_key, minlength = len(line_keys))
    by_line = {tuple(kk): (int(m), c) for kk, m, c in zip(line_keys, n_legs, line_counts)}
    overall = add_up(np.arange(len(matched)), matched["period"].to_numpy(), len(PERIODS))

    profiles = Profiles(by_pair = by_pair, by_line = by_line, overall = overall, source = folder)
    logger.info(f"  {len(by_pair)} line-section-periods, {len(by_line)} line-periods; "
                f"{(on_board >= MIN_PEOPLE).mean():.0%} of the line-section-periods with {MIN_PEOPLE} people or more")
    if cache_dir:
        os.makedirs(cache_dir, exist_ok = True)
        with open(cache, "wb") as f:
            pickle.dump(profiles, f, protocol = pickle.HIGHEST_PROTOCOL)
    return profiles
