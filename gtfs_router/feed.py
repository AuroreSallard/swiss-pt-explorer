"""Reading a GTFS feed and building the arrays of the router: the stops, the connections (a vehicle going from one stop
to the next) sorted by departure time, the stops of each trip, and the walking links between stops.

The whole feed is read once (all its trips, with the days each one runs: the calendar of its service) and kept in the
cache; the network of a date (Network.on) keeps the connections of the trips running on it, the stops, trips and walks
staying those of the whole feed (the same indices on every date)."""
import dataclasses
import hashlib
import logging
import os
import pickle
import zipfile
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
VERSION = 2   # of the network built, in the name of the cache


def mode_of(route_type):
    t = int(route_type)
    basic = {0: "tram", 1: "metro", 2: "rail", 3: "bus", 4: "boat", 5: "cable car", 6: "cable car", 7: "funicular", 11: "bus", 12: "rail"}
    if t in basic:
        return basic[t]
    for low, high, mode in ((100, 199, "rail"), (200, 299, "bus"), (400, 499, "metro"), (700, 899, "bus"), (900, 999, "tram"),
                            (1000, 1099, "boat"), (1300, 1399, "cable car"), (1400, 1499, "funicular")):
        if low <= t <= high:
            return mode
    return "other"


def to_seconds(values):
    if len(values) == 0:
        return np.zeros(0, np.int32)
    parts = values.astype(str).str.split(":", expand = True)
    return (parts[0].astype(int) * 3600 + parts[1].astype(int) * 60 + parts[2].astype(int)).to_numpy(np.int32)


def station_code(stop_id):
    """The station code of a stop id: 8501120:0:1 -> 8501120, Parent8501120 -> 8501120."""
    return str(stop_id).replace("Parent", "").split(":")[0]


def day_of(date):
    """YYYYMMDD of a date given as YYYYMMDD, YYYY-MM-DD, a Timestamp..."""
    return pd.Timestamp(str(date)).strftime("%Y%m%d")


class _Tables:
    """The .txt tables of a feed in a zip or a folder, read as strings."""

    def __init__(self, path):
        self.path = path

    def has(self, name):
        if os.path.isdir(self.path):
            return os.path.exists(os.path.join(self.path, f"{name}.txt"))
        with zipfile.ZipFile(self.path) as z:
            return f"{name}.txt" in z.namelist()

    def read(self, name, usecols = None, chunk_filter = None):
        def read(handle):
            reader = pd.read_csv(handle, dtype = str, usecols = (lambda c: c in usecols) if usecols else None,
                                 keep_default_na = False, na_values = [""], chunksize = 2_000_000)
            return pd.concat([chunk_filter(c) if chunk_filter else c for c in reader], ignore_index = True)

        if os.path.isdir(self.path):
            return read(os.path.join(self.path, f"{name}.txt"))
        with zipfile.ZipFile(self.path) as z, z.open(f"{name}.txt") as f:
            return read(f)


def service_calendar(tables):
    """(service ids, first day YYYYMMDD, bool array service x day: whether it runs) of calendar and calendar_dates."""
    cal = tables.read("calendar") if tables.has("calendar") else None
    cd = tables.read("calendar_dates", ["service_id", "date", "exception_type"]) if tables.has("calendar_dates") else None
    ids, bounds = [], []
    if cal is not None:
        ids.append(cal["service_id"])
        bounds += [pd.to_datetime(cal["start_date"], format = "%Y%m%d").min(), pd.to_datetime(cal["end_date"], format = "%Y%m%d").max()]
    if cd is not None:
        cd_day = pd.to_datetime(cd["date"], format = "%Y%m%d")
        ids.append(cd["service_id"])
        bounds += [cd_day.min(), cd_day.max()]
    services = pd.Index(pd.unique(pd.concat(ids, ignore_index = True)))
    first, last = min(bounds), max(bounds)
    days = pd.date_range(first, last)
    runs = np.zeros((len(services), len(days)), bool)
    if cal is not None:
        start = (pd.to_datetime(cal["start_date"], format = "%Y%m%d") - first).dt.days.to_numpy()
        end = (pd.to_datetime(cal["end_date"], format = "%Y%m%d") - first).dt.days.to_numpy()
        week = cal[WEEKDAYS].astype(int).to_numpy().astype(bool)
        d = np.arange(len(days))
        np.logical_or.at(runs, services.get_indexer(cal["service_id"]),
                         week[:, days.weekday] & (d >= start[:, None]) & (d <= end[:, None]))
    if cd is not None:
        s, d, kind = services.get_indexer(cd["service_id"]), (cd_day - first).dt.days.to_numpy(), cd["exception_type"].astype(int).to_numpy()
        runs[s[kind == 1], d[kind == 1]] = True
        runs[s[kind == 2], d[kind == 2]] = False
    return services, first.strftime("%Y%m%d"), runs


@dataclass
class Network:
    date: Optional[str]         # YYYYMMDD of the network of a date (Network.on), None for the whole feed
    # stops (served by the feed)
    stop_ids: np.ndarray
    stop_names: np.ndarray
    stop_lat: np.ndarray
    stop_lon: np.ndarray
    change_s: np.ndarray        # time to change vehicles at the same stop
    # connections, sorted by departure time
    c_dep_stop: np.ndarray
    c_arr_stop: np.ndarray
    c_dep: np.ndarray
    c_arr: np.ndarray
    c_trip: np.ndarray
    c_pos: np.ndarray           # position of the departure stop in its trip
    # trips: their stops (CSR) and descriptions
    t_start: np.ndarray
    t_stop: np.ndarray
    t_arr: np.ndarray
    t_dep: np.ndarray
    trip_ids: np.ndarray
    trip_route: np.ndarray      # index in routes
    routes: pd.DataFrame        # route_id, name, mode, agency
    # walking between stops (CSR)
    f_start: np.ndarray
    f_to: np.ndarray
    f_dur: np.ndarray
    # the days the trips run (whole feed only)
    first_day: Optional[str] = None          # YYYYMMDD, the day of service_days[:, 0]
    trip_service: Optional[np.ndarray] = None   # row of service_days of each trip
    service_days: Optional[np.ndarray] = None   # bool, service x day

    @property
    def dates(self):
        """(first, last) YYYYMMDD of the dates of the network."""
        if self.service_days is None:
            return self.date, self.date
        last = pd.Timestamp(self.first_day) + pd.Timedelta(days = self.service_days.shape[1] - 1)
        return self.first_day, last.strftime("%Y%m%d")

    def on(self, date):
        """The network of a date: the connections of the trips running on it (the stops, trips and walks of the whole
        feed). A trip of the day before running after midnight (times after 24:00) is not in it."""
        date = day_of(date)
        if self.service_days is None:
            if date != self.date:
                raise ValueError(f"The network is the one of {self.date}, not {date}")
            return self
        first, last = self.dates
        day = (pd.Timestamp(date) - pd.Timestamp(first)).days
        if not first <= date <= last:
            raise ValueError(f"{date} is outside the feed ({first} to {last})")
        keep = self.service_days[:, day][self.trip_service][self.c_trip]
        return dataclasses.replace(self, date = date, c_dep_stop = self.c_dep_stop[keep], c_arr_stop = self.c_arr_stop[keep],
                                   c_dep = self.c_dep[keep], c_arr = self.c_arr[keep], c_trip = self.c_trip[keep], c_pos = self.c_pos[keep],
                                   first_day = None, trip_service = None, service_days = None)

    def departures(self):
        """The departures of each stop on a day (on average over the dates of the whole feed)."""
        if self.service_days is None:
            return np.bincount(self.c_dep_stop, minlength = len(self.stop_ids))
        per_day = self.service_days.mean(axis = 1)[self.trip_service][self.c_trip]
        return np.bincount(self.c_dep_stop, weights = per_day, minlength = len(self.stop_ids))

    def stops_near(self, lat, lon, radius_m):
        """Indices of the stops within radius_m of a point, and their beeline distances."""
        k = 111_320
        c = np.cos(np.radians(lat))
        d = np.hypot((self.stop_lat - lat) * k, (self.stop_lon - lon) * k * c)
        near = np.flatnonzero(d <= radius_m)
        return near, d[near]

    def stop_index(self, stop_id):
        """Indices of the stops of a stop id, or of all the platforms of a station code (and the stop of the code itself:
        some feeds serve both 8507000 and 8507000:0:6)."""
        code = station_code(stop_id)
        exact = np.flatnonzero(self.stop_ids == stop_id)
        if len(exact) and code != str(stop_id):
            return exact
        return np.flatnonzero(np.array([station_code(s) == code for s in self.stop_ids]))


def _inside(area, lon, lat, margin_m):
    """Whether the points are within margin_m of the polygons of the file."""
    import geopandas as gpd
    import shapely
    polygons = gpd.read_file(area).to_crs(2056).union_all().buffer(margin_m)
    points = gpd.GeoSeries(gpd.points_from_xy(lon, lat), crs = 4326).to_crs(2056)
    shapely.prepare(polygons)
    return shapely.contains_xy(polygons, points.x.to_numpy(), points.y.to_numpy())


def build_network(gtfs_path, date = None, walk_radius_m = 400, walk_speed_m_s = 1.2, detour = 1.3, default_change_s = 60, cache_dir = None,
                  area = None, area_margin_m = 100):
    """The router network of a GTFS feed (zip or folder): the whole feed, or with a date (YYYYMMDD) the network of that
    date (Network.on). The whole feed is cached in cache_dir.
    area: a file of polygons (e.g. data/network/tarifverbundkarte.geojson, the Swiss municipalities): the stops further
    than area_margin_m from them are left out; a trip leaving the area goes on from its last stop inside to the next one."""
    network = _build(gtfs_path, walk_radius_m, walk_speed_m_s, detour, default_change_s, cache_dir, area, area_margin_m)
    return network.on(date) if date is not None else network


def _build(gtfs_path, walk_radius_m, walk_speed_m_s, detour, default_change_s, cache_dir, area, area_margin_m):
    if cache_dir:
        stamp = f"{VERSION}|{os.path.abspath(gtfs_path)}|{os.path.getmtime(gtfs_path)}|{walk_radius_m}|{walk_speed_m_s}|{detour}|{default_change_s}"
        if area:
            stamp += f"|{os.path.abspath(area)}|{os.path.getmtime(area)}|{area_margin_m}"
        name = os.path.splitext(os.path.basename(os.path.normpath(gtfs_path)))[0]
        cache = os.path.join(cache_dir, f"network_{name}_{hashlib.md5(stamp.encode()).hexdigest()[:10]}.pkl")
        if os.path.exists(cache):
            logger.info(f"Network from the cache {cache}")
            with open(cache, "rb") as f:
                return pickle.load(f)

    tables = _Tables(gtfs_path)
    logger.info(f"Reading {gtfs_path}")
    services, first_day, service_days = service_calendar(tables)
    logger.info(f"  {len(services)} services, {service_days.shape[1]} days from {first_day}")
    trips = tables.read("trips", ["route_id", "service_id", "trip_id"]).drop_duplicates("trip_id").reset_index(drop = True)
    all_stops = tables.read("stops", ["stop_id", "stop_name", "stop_lat", "stop_lon"]).drop_duplicates("stop_id").dropna(subset = ["stop_lat"])
    all_stops = all_stops.reset_index(drop = True)
    trip_codes, stop_codes = pd.Index(trips["trip_id"]), pd.Index(all_stops["stop_id"])
    logger.info(f"  {len(trips)} trips, {len(all_stops)} stops")

    def compact(c):
        """The stop times as numbers: trip and stop (indices in trips and all_stops), sequence, times in seconds."""
        dep = c["departure_time"].fillna(c["arrival_time"])
        arr = c["arrival_time"].fillna(dep)
        trip, stop = trip_codes.get_indexer(c["trip_id"]), stop_codes.get_indexer(c["stop_id"])
        keep = dep.notna().to_numpy() & (trip >= 0) & (stop >= 0)
        return pd.DataFrame({"trip": trip[keep].astype(np.int32), "stop": stop[keep].astype(np.int32),
                             "seq": c["stop_sequence"][keep].astype(np.int32).to_numpy(), "arr": to_seconds(arr[keep]), "dep": to_seconds(dep[keep])})

    st = tables.read("stop_times", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], chunk_filter = compact)
    logger.info(f"  {len(st)} stop times")

    # stops: those served (and in the area)
    served = np.unique(st["stop"].to_numpy())
    stops = all_stops.iloc[served]
    if area:
        inside = _inside(area, stops["stop_lon"].astype(float).to_numpy(), stops["stop_lat"].astype(float).to_numpy(), area_margin_m)
        logger.info(f"  {(~inside).sum()} of {len(stops)} stops outside {os.path.basename(area)}")
        served, stops = served[inside], stops[inside]
    stops = stops.set_index("stop_id")
    renumber = np.full(len(all_stops), -1, np.int32)
    renumber[served] = np.arange(len(served))
    st_stop = renumber[st["stop"].to_numpy()]
    keep = st_stop >= 0
    trip, seq = st["trip"].to_numpy()[keep], st["seq"].to_numpy()[keep]
    t_stop, t_arr, t_dep = st_stop[keep], st["arr"].to_numpy()[keep], st["dep"].to_numpy()[keep]
    del st
    order = np.lexsort((seq, trip))
    trip, t_stop, t_arr, t_dep = trip[order], t_stop[order], t_arr[order], t_dep[order]
    stop_index = pd.Series(np.arange(len(stops)), index = stops.index)
    lat, lon = stops["stop_lat"].astype(float).to_numpy(), stops["stop_lon"].astype(float).to_numpy()

    # trips: those with stops left
    trip_order, t = np.unique(trip, return_inverse = True)
    t = t.astype(np.int32)
    t_start = np.concatenate([[0], np.cumsum(np.bincount(t))]).astype(np.int64)
    trips = trips.iloc[trip_order]
    trip_service = services.get_indexer(trips["service_id"])
    trip_service[trip_service < 0] = len(services)   # a service without calendar: never runs
    service_days = np.vstack([service_days, np.zeros((1, service_days.shape[1]), bool)])

    routes = tables.read("routes", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"])
    agency = tables.read("agency", ["agency_id", "agency_name"]) if tables.has("agency") else None
    if agency is not None and "agency_id" in routes.columns:
        routes = routes.merge(agency.drop_duplicates("agency_id"), on = "agency_id", how = "left")
    routes["name"] = routes["route_short_name"].fillna(routes.get("route_long_name")).fillna(routes["route_id"])
    routes["mode"] = routes["route_type"].fillna("3").map(mode_of)
    routes["agency"] = routes.get("agency_name", pd.Series("", index = routes.index)).fillna("")
    routes = routes[["route_id", "name", "mode", "agency"]].drop_duplicates("route_id").reset_index(drop = True)
    trip_route = pd.Index(routes["route_id"]).get_indexer(trips["route_id"]).astype(np.int32)

    # connections: each stop to the next one of the same trip
    pos = np.arange(len(t)) - t_start[t]
    nxt = np.arange(len(t)) + 1
    valid = np.ones(len(t), bool)
    valid[t_start[1:] - 1] = False  # the last stop of each trip
    i = np.flatnonzero(valid)
    c_dep, c_arr = t_dep[i], t_arr[nxt[i]]
    order = np.lexsort((c_arr, c_dep))
    i = i[order]

    # walking: the transfers.txt times, else the beeline distance with a detour at walking speed
    k = 111_320
    lat0 = np.radians(lat.mean())
    xy = np.column_stack([lon * k * np.cos(lat0), lat * k])
    pairs = np.array(sorted(cKDTree(xy).query_pairs(walk_radius_m)), dtype = np.int64).reshape(-1, 2)
    pairs = np.vstack([pairs, pairs[:, ::-1]])
    dur = np.hypot(*(xy[pairs[:, 0]] - xy[pairs[:, 1]]).T) * detour / walk_speed_m_s
    change = np.full(len(stops), default_change_s, np.int32)
    if tables.has("transfers"):
        tr = tables.read("transfers", ["from_stop_id", "to_stop_id", "min_transfer_time"])
        tr = tr[tr["from_stop_id"].isin(stop_index.index) & tr["to_stop_id"].isin(stop_index.index)]
        tr["min_transfer_time"] = pd.to_numeric(tr["min_transfer_time"], errors = "coerce").fillna(0)
        a, b = stop_index.loc[tr["from_stop_id"]].to_numpy(), stop_index.loc[tr["to_stop_id"]].to_numpy()
        same = a == b
        change[a[same]] = tr["min_transfer_time"].to_numpy()[same]
        rules = pd.Series(tr["min_transfer_time"].to_numpy()[~same], index = pd.MultiIndex.from_arrays([a[~same], b[~same]])).groupby(level = [0, 1]).min()
        known = pd.MultiIndex.from_arrays([pairs[:, 0], pairs[:, 1]])
        hit = known.isin(rules.index)
        dur[hit] = rules.reindex(known[hit]).to_numpy()
        extra = rules.index.difference(known)   # rules between stops further apart than the radius
        if len(extra):
            pairs = np.vstack([pairs, np.array(list(extra))])
            dur = np.concatenate([dur, rules.reindex(extra).to_numpy()])
        logger.info(f"  {len(tr)} transfers.txt rules ({same.sum()} at the same stop)")
    o = np.argsort(pairs[:, 0], kind = "stable")
    pairs, dur = pairs[o], dur[o]
    f_start = np.searchsorted(pairs[:, 0], np.arange(len(stops) + 1)).astype(np.int64)

    network = Network(
        date = None, stop_ids = stops.index.to_numpy(str), stop_names = stops["stop_name"].fillna("").to_numpy(str),
        stop_lat = lat, stop_lon = lon, change_s = change,
        c_dep_stop = t_stop[i], c_arr_stop = t_stop[nxt[i]], c_dep = c_dep[order], c_arr = c_arr[order],
        c_trip = t[i], c_pos = pos[i].astype(np.int32),
        t_start = t_start, t_stop = t_stop, t_arr = t_arr, t_dep = t_dep, trip_ids = trips["trip_id"].to_numpy(str),
        trip_route = trip_route, routes = routes,
        f_start = f_start, f_to = pairs[:, 1].astype(np.int32), f_dur = np.ceil(dur).astype(np.int32),
        first_day = first_day, trip_service = trip_service.astype(np.int32), service_days = service_days,
    )
    first, last = network.dates
    logger.info(f"  {len(stops)} stops, {len(trips)} trips, {len(i)} connections ({first} to {last}), {len(pairs)} walking links")

    if cache_dir:
        os.makedirs(cache_dir, exist_ok = True)
        with open(cache, "wb") as f:
            pickle.dump(network, f, protocol = pickle.HIGHEST_PROTOCOL)
    return network
