"""How crowded the vehicles are, from a MATSim simulation.

The simulation tells, for each simulated vehicle, how many people board and leave it at each stop
(pt_passenger_counts.csv.gz). Going along the stops of its route (output_transitSchedule.xml.gz) and adding up boardings
minus alightings gives its load between each pair of consecutive stops. Dividing by the sample rate scales it to the real
population (a 10 % simulation: loads × 10). Dividing by the capacity of the vehicle, seats plus standing room
(output_transitVehicles.xml.gz), gives its occupancy.

The simulation may use another timetable than the router (another year, other trip ids), so its vehicles cannot be
matched one by one. Instead, the loads are averaged by line name, pair of stations and hour of departure, and a ride of
the router looks up its line, stations and hours in these averages. A "station" here is the stop id before its first
':' (8516274:0:2 -> 8516274), so that the platforms of a station count as one. A simulated vehicle that carries nobody
counts with a load of 0.

Building the averages takes a while for Switzerland, so the result is kept in the cache folder.
"""

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

logger  = logging.getLogger(__name__)
VERSION = 1   # change it when the way the crowding is computed changes, so that the old cache files are not used
# the occupancy (load / capacity) from which a segment is at level 2 (some crowding), and at level 3 (crowded)
LEVELS  = (0.5, 0.8)
FILES   = ("output_transitSchedule.xml.gz", "output_transitVehicles.xml.gz", "pt_passenger_counts.csv.gz")


def _station(stop_id):
    """The station of a stop id of the simulation or of the timetable: 8516274:0:2.link:pt_8516274:0:2 -> 8516274."""
    
    return str(stop_id).split(".link:")[0].split(":")[0]


def _seconds(hms):
    """ "08:15:30" -> 29730 seconds after midnight."""
    
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


@dataclass
class Crowding:
    """The average loads of the simulation, to look up the rides of the router.

    Each value is a tuple (load, occupancy, capacity, vehicles): the average number of people on board, the average
    share of the capacity they fill, the average capacity, and the number of simulated vehicles averaged."""
    
    by_pair: dict           # (line name, from station, to station, hour) -> (load, occupancy, capacity, vehicles)
    by_stop: dict           # (line name, from station, hour) -> the same, whatever the next station
    scale:   float          # 1 / the sample rate
    source:  str            # the simulation folder

    def segment(self, line, from_station, to_station, hour):
        """The average (load, occupancy, capacity, vehicles) of a line between two stations at an hour, or None.

        When the simulation has no vehicle of the line there at that hour, the hour before and the hour after are tried,
        then the hour within the day (25 -> 1, for the times after midnight of the timetable), and last the departures
        from the first station whatever the next one (the simulated vehicle may skip a stop the timetable serves)."""
        
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
        """The crowding of a ride of the router, or None when the simulation knows nothing about it.

        Returns the values of its most crowded segment ({level, load, occupancy, capacity}), plus "segments" (the same
        for each segment between two stops, None where unknown) and "known" (the share of the segments found)."""
        
        line     = str(net.routes["name"].iat[net.trip_route[ride.trip]])
        begin    = net.t_start[ride.trip] + ride.pos
        segments = []
        
        for k in range(len(ride.stops) - 1):
            a, b  = ride.stops[k], ride.stops[k + 1]
            hour  = int(net.t_dep[begin + k]) // 3600
            found = self.segment(line, _station(net.stop_ids[a]), _station(net.stop_ids[b]), hour)
            
            segments.append(None if found is None else {"load": round(found[0]), "occupancy": round(found[1], 2),
                                                        "capacity": round(found[2]), "level": level(found[1])})
        
        known = [s for s in segments if s is not None]
        if not known:
            return None
        
        worst = max(known, key = lambda s: s["occupancy"])
        return {**worst, "segments": segments, "known": len(known) / len(segments)}


def level(occupancy):
    """The level shown by the person icons: 1 below 50 % of the capacity, 2 (some crowding) below 80 %, else 3 (crowded)."""
    
    return 1 + sum(occupancy >= t for t in LEVELS)


def build_crowding(folder, sample = 0.1, cache_dir = None):
    """The Crowding of the simulation whose output files are in folder.

    sample: the share of the population the simulation simulated (0.1 for 10 %). cache_dir: where the result is kept, and
    found again as long as the files of the simulation and the sample rate are the same."""
    
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

    # The capacity of each vehicle: the seats and standing room of its vehicle type
    text             = gzip.open(vehicles_path, "rt").read()
    capacity_of_type = {t: int(seats) + int(standing) for t, seats, standing in re.findall(
        r'<vehicleType id="([^"]+)">.*?<capacity seats="(\d+)" standingRoomInPersons="(\d+)"', text, re.S)}
    capacity         = {v: capacity_of_type.get(t, np.nan) for v, t in re.findall(r'<vehicle id="([^"]+)" type="([^"]+)"', text)}

    # From the schedule: the stops of each route in order (with their time after the start of the route), and the
    # departures of each route (its line, the vehicle that runs it, its start time)
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
            el.clear()   # the schedule of Switzerland is large: what has been read is freed
    
    stops      = pd.DataFrame(stops, columns = ["route", "k", "stop", "offset"])
    departures = pd.DataFrame(departures, columns = ["route", "line", "vehicle", "time"])

    # Each vehicle at each stop of its route, with the people boarding and leaving there; its load after the stop is the
    # sum of the boardings minus the alightings so far, scaled to the whole population
    visits         = departures.merge(stops, on = "route").sort_values(["vehicle", "k"])
    counts         = pd.read_csv(counts_path, sep = ";", usecols = ["stop_id", "vehicle_id", "boardings", "alightings"])
    counts["stop"] = counts["stop_id"].str.split(".link:").str[0]
    counts         = counts.groupby(["vehicle_id", "stop"])[["boardings", "alightings"]].sum()
    visits         = visits.join(counts, on = ["vehicle", "stop"])
    
    # A stop visited twice by the same vehicle (a loop line): the counts of the stop are put on its first visit only
    visits.loc[visits.duplicated(["vehicle", "stop"]), ["boardings", "alightings"]] = 0
    
    visits[["boardings", "alightings"]] = visits[["boardings", "alightings"]].fillna(0)
    visits["load"]                      = (visits["boardings"] - visits["alightings"]).groupby(visits["vehicle"]).cumsum() / sample

    # Segments: each vehicle from each stop to its next one, with the hour it leaves and its occupancy
    nxt = visits.groupby("vehicle")["stop"].shift(-1)
    seg = visits.assign(to = nxt)[nxt.notna()]
    seg = seg.assign(from_station = seg["stop"].map(_station), to_station = seg["to"].map(_station),
                     hour = (seg["time"] + seg["offset"]) // 3600, capacity = seg["vehicle"].map(capacity))
    seg = seg[seg["from_station"] != seg["to_station"]]
    
    seg["occupancy"] = seg["load"].clip(lower = 0) / seg["capacity"]

    def table(keys):
        """The averages of the segments grouped by keys: {key: (load, occupancy, capacity, vehicles)}."""
        
        grouped = seg.groupby(keys).agg(load = ("load", "mean"), occupancy = ("occupancy", "mean"),
                                        capacity = ("capacity", "mean"), vehicles = ("vehicle", "nunique"))
        return {key: tuple(row) for key, row in zip(grouped.index, grouped.itertuples(index = False))}

    crowding = Crowding(by_pair = table(["line", "from_station", "to_station", "hour"]), by_stop = table(["line", "from_station", "hour"]),
                        scale = 1 / sample, source = folder)
    boarded  = counts["boardings"].sum()

    logger.info(f"  {len(departures)} departures, {int(boarded)} boardings (x{1 / sample:g}), {len(crowding.by_pair)} line-segment-hours; "
                f"occupancy over {LEVELS[1]:.0%} on {(seg['occupancy'] >= LEVELS[1]).mean():.1%} of the vehicle segments")

    if cache_dir:
        os.makedirs(cache_dir, exist_ok = True)
        with open(cache, "wb") as f:
            pickle.dump(crowding, f, protocol = pickle.HIGHEST_PROTOCOL)
    return crowding
