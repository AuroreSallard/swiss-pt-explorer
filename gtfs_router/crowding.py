"""Crowding of the vehicles from a MATSim simulation: the load of each simulated vehicle between consecutive stops
(boardings minus alightings of pt_passenger_counts.csv.gz along the stops of its route in output_transitSchedule.xml.gz,
multiplied by the inverse of the sample rate), against its capacity (output_transitVehicles.xml.gz).

The simulation can have another timetable than the router (another year): the loads are averaged by line name, pair of
stations (the stop ids before the first ':') and hour of departure, and the rides of the router look them up the same
way. A vehicle without passengers counts with a load of 0."""
import gzip
import hashlib
import logging
import os
import pickle
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
VERSION = 1
# occupancy (load / capacity) from which a segment is in the level 2 (some crowding) and 3 (crowded)
LEVELS = (0.5, 0.8)
FILES = ("output_transitSchedule.xml.gz", "output_transitVehicles.xml.gz", "pt_passenger_counts.csv.gz")


def _station(stop_id):
    """8516274:0:2.link:pt_8516274:0:2 -> 8516274"""
    return str(stop_id).split(".link:")[0].split(":")[0]


def _seconds(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


@dataclass
class Crowding:
    by_pair: dict           # (line name, from station, to station, hour) -> (load, occupancy, capacity, vehicles)
    by_stop: dict           # (line name, from station, hour) -> the same, whatever the next station
    scale: float
    source: str

    def segment(self, line, from_station, to_station, hour):
        """(load, occupancy, capacity, vehicles) of the line between two stations at the hour (the next or previous
        hour if needed), or None."""
        for h in (hour, hour - 1, hour + 1, hour % 24):
            found = self.by_pair.get((line, from_station, to_station, h))
            if found is not None:
                return found
        for h in (hour, hour % 24):
            found = self.by_stop.get((line, from_station, h))
            if found is not None:
                return found
        return None

    def ride(self, net, ride):
        """Crowding of a ride of the router: {level, load, occupancy, capacity, segments} (the most crowded segment), or
        None without simulated data on it."""
        line = str(net.routes["name"].iat[net.trip_route[ride.trip]])
        begin = net.t_start[ride.trip] + ride.pos
        segments = []
        for k in range(len(ride.stops) - 1):
            a, b = ride.stops[k], ride.stops[k + 1]
            hour = int(net.t_dep[begin + k]) // 3600
            found = self.segment(line, _station(net.stop_ids[a]), _station(net.stop_ids[b]), hour)
            segments.append(None if found is None else {"load": round(found[0]), "occupancy": round(found[1], 2),
                                                        "capacity": round(found[2]), "level": level(found[1])})
        known = [s for s in segments if s is not None]
        if not known:
            return None
        worst = max(known, key = lambda s: s["occupancy"])
        return {**worst, "segments": segments, "known": len(known) / len(segments)}


def level(occupancy):
    """1 low, 2 some crowding, 3 crowded."""
    return 1 + sum(occupancy >= t for t in LEVELS)


def build_crowding(folder, sample = 0.1, cache_dir = None):
    """The crowding of the simulation of the output folder; sample: the sample rate of the population (0.1: 10 %)."""
    paths = [os.path.join(folder, f) for f in FILES]
    if cache_dir:
        stamp = f"{VERSION}|{sample}|" + "|".join(f"{os.path.abspath(p)}:{os.path.getmtime(p)}" for p in paths)
        cache = os.path.join(cache_dir, f"crowding_{hashlib.md5(stamp.encode()).hexdigest()[:10]}.pkl")
        if os.path.exists(cache):
            logger.info(f"Crowding from the cache {cache}")
            with open(cache, "rb") as f:
                return pickle.load(f)

    logger.info(f"Crowding of the simulation {folder} (sample {sample})")
    schedule_path, vehicles_path, counts_path = paths

    # the capacity of each vehicle
    text = gzip.open(vehicles_path, "rt").read()
    capacity_of_type = {t: int(seats) + int(standing) for t, seats, standing in re.findall(
        r'<vehicleType id="([^"]+)">.*?<capacity seats="(\d+)" standingRoomInPersons="(\d+)"', text, re.S)}
    capacity = {v: capacity_of_type.get(t, np.nan) for v, t in re.findall(r'<vehicle id="([^"]+)" type="([^"]+)"', text)}

    # the stops of each route (with their offsets) and its departures
    stops, departures = [], []
    line_name = route = None
    for event, el in ET.iterparse(gzip.open(schedule_path), events = ("start", "end")):
        if event == "start" and el.tag == "transitLine":
            line_name = el.get("name") or el.get("id")
        elif event == "start" and el.tag == "transitRoute":
            route, k = el.get("id"), 0
        elif event == "end" and el.tag == "stop" and route is not None:
            offset = el.get("departureOffset") or el.get("arrivalOffset") or "00:00:00"
            stops.append((route, k, el.get("refId").split(".link:")[0], _seconds(offset)))
            k += 1
        elif event == "end" and el.tag == "departure":
            departures.append((route, line_name, el.get("vehicleRefId"), _seconds(el.get("departureTime"))))
        elif event == "end" and el.tag == "transitRoute":
            el.clear()
    stops = pd.DataFrame(stops, columns = ["route", "k", "stop", "offset"])
    departures = pd.DataFrame(departures, columns = ["route", "line", "vehicle", "time"])

    # each vehicle at each stop of its route, with its boardings and alightings
    visits = departures.merge(stops, on = "route").sort_values(["vehicle", "k"])
    counts = pd.read_csv(counts_path, sep = ";", usecols = ["stop_id", "vehicle_id", "boardings", "alightings"])
    counts["stop"] = counts["stop_id"].str.split(".link:").str[0]
    counts = counts.groupby(["vehicle_id", "stop"])[["boardings", "alightings"]].sum()
    visits = visits.join(counts, on = ["vehicle", "stop"])
    # a stop visited twice by a vehicle (a loop): its passengers at the first visit
    visits.loc[visits.duplicated(["vehicle", "stop"]), ["boardings", "alightings"]] = 0
    visits[["boardings", "alightings"]] = visits[["boardings", "alightings"]].fillna(0)
    visits["load"] = (visits["boardings"] - visits["alightings"]).groupby(visits["vehicle"]).cumsum() / sample

    # the segments: from each stop to the next one of the vehicle
    nxt = visits.groupby("vehicle")["stop"].shift(-1)
    seg = visits.assign(to = nxt)[nxt.notna()]
    seg = seg.assign(from_station = seg["stop"].map(_station), to_station = seg["to"].map(_station),
                     hour = (seg["time"] + seg["offset"]) // 3600, capacity = seg["vehicle"].map(capacity))
    seg = seg[seg["from_station"] != seg["to_station"]]
    seg["occupancy"] = seg["load"].clip(lower = 0) / seg["capacity"]

    def table(keys):
        grouped = seg.groupby(keys).agg(load = ("load", "mean"), occupancy = ("occupancy", "mean"),
                                        capacity = ("capacity", "mean"), vehicles = ("vehicle", "nunique"))
        return {key: tuple(row) for key, row in zip(grouped.index, grouped.itertuples(index = False))}

    crowding = Crowding(by_pair = table(["line", "from_station", "to_station", "hour"]), by_stop = table(["line", "from_station", "hour"]),
                        scale = 1 / sample, source = folder)
    boarded = counts["boardings"].sum()
    logger.info(f"  {len(departures)} departures, {int(boarded)} boardings (x{1 / sample:g}), {len(crowding.by_pair)} line-segment-hours; "
                f"occupancy over {LEVELS[1]:.0%} on {(seg['occupancy'] >= LEVELS[1]).mean():.1%} of the vehicle segments")

    if cache_dir:
        os.makedirs(cache_dir, exist_ok = True)
        with open(cache, "wb") as f:
            pickle.dump(crowding, f, protocol = pickle.HIGHEST_PROTOCOL)
    return crowding
