"""The web server: the map page of web/ and the API it calls.

    python api.py

then open http://localhost:8000. The timetable, prices and simulation are those of config.yml and config_local.yml
(or the options below). The API is described at http://localhost:8000/docs:

  GET /api/route?from=..&to=..&date=2026-03-18&time=08:00
        the journeys between two places, with their legs, prices, crowding and passenger profiles
  GET /api/stations?q=lausanne
        stations by name, for the search fields
  GET /api/info
        the dates of the timetable (first, last, default) and the area it covers
  GET /api/fares
        the fares, as the price editor shows them
  GET /api/networks, GET /api/networks/ZVV/zones
        the fare networks and their zones (names, municipalities, polygons), to choose zone subscriptions

The parameters of /api/route are described in the docstring of route() below, and at /docs.
"""
import argparse
import functools
import json
import logging
import os
import unicodedata
from typing import List, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from gtfs_router import EARLIEST_ARRIVAL, SWISS_COST, Journey, Leg, Router
from gtfs_router.feed import day_of
from gtfs_router.load import add_arguments, default_date, load, load_simulation
from gtfs_router.pricing import Traveller, parse_subscriptions
from scipy.spatial import ConvexHull

HERE = os.path.dirname(os.path.abspath(__file__))
MARGIN_S = 5 * 60   # a journey beaten by another one (leaving later, arriving earlier) is still listed if it loses at most this
logger = logging.getLogger(__name__)


def _normalized(text):
    """Lower case, without accents: "Genève" -> "geneve"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


class Stations:
    """The stations of the search fields: the stops grouped by name and proximity (the platforms of a station, the two
    sides of a bus stop...), ranked by how many departures they have."""

    def __init__(self, net, radius_m = 1000):
        """The stops of the same name within radius_m of each other are one station, at their average position."""
        k = 111_320
        departures = net.departures()
        stops = pd.DataFrame({"name": net.stop_names, "lat": net.stop_lat, "lon": net.stop_lon, "departures": departures})
        rows = []
        for name, group in stops.groupby("name", sort = False):
            clusters = []   # [lat, lon, [stop indices]]
            for s, lat, lon in zip(group.index, group["lat"], group["lon"]):
                for c in clusters:
                    if np.hypot((c[0] - lat) * k, (c[1] - lon) * k * np.cos(np.radians(lat))) <= radius_m:
                        c[2].append(s)
                        break
                else:
                    clusters.append([lat, lon, [s]])
            for _, _, members in clusters:
                rows.append({"name": name, "stops": np.array(members), "lat": float(net.stop_lat[members].mean()),
                             "lon": float(net.stop_lon[members].mean()), "departures": round(float(departures[members].sum()))})
        self.table = pd.DataFrame(rows)
        self.table["key"] = self.table["name"].map(_normalized)
        self.table = self.table.sort_values("departures", ascending = False).reset_index(drop = True)
        self.table["id"] = np.arange(len(self.table))
        self.by_id = self.table.set_index("id")
        logger.info(f"  {len(self.table)} stations")

    def search(self, query, limit = 10):
        """The stations whose name holds every word of the query: the exact name first, then the names starting with the
        query, then the others; the busiest first in each group."""
        words = _normalized(query).split()
        if not words:
            return self.table.iloc[:0]
        hit = np.ones(len(self.table), bool)
        for w in words:
            hit &= self.table["key"].str.contains(w, regex = False).to_numpy()
        found = self.table[hit]
        exact, starts = found["key"] == " ".join(words), found["key"].str.startswith(" ".join(words))
        return pd.concat([found[exact], found[starts & ~exact], found[~starts]]).head(limit)

    def get(self, station_id):
        """A station by its id (the id of /api/stations), or a 404 error."""
        if station_id not in self.by_id.index:
            raise HTTPException(404, f"No station {station_id}")
        return self.by_id.loc[station_id]


class Zones:
    """The zones of each fare network, as the map shows them to choose zone subscriptions: their name, municipalities,
    number of stops, whether they count twice, and their outline (the zone polygons of the tracks, else the outline of
    their stops)."""

    def __init__(self, net, pricing, tracks = None):
        polygons = self._polygons(tracks.zones) if tracks is not None else {}
        stops = {}
        for s, stop_id in enumerate(net.stop_ids):
            for auth, zone in pricing._stop_zones(str(stop_id)):
                if auth.id != "SBB":
                    stops.setdefault(auth.id, {}).setdefault(zone, []).append(s)
        departures = net.departures()
        self.networks = {}
        for network, zones in stops.items():
            calculator = pricing.calculators.get(network, {})
            rows = []
            for zone, members in zones.items():
                members = np.array(members)
                by_name = pd.Series(departures[members], index = net.stop_names[members]).groupby(level = 0).sum()
                names = by_name.sort_values(ascending = False, kind = "stable").index.tolist()   # the busiest places first
                shape = polygons.get((network, zone))
                rows.append({"id": zone, "name": shape["name"] if shape else names[0], "places": shape["municipalities"] if shape else names[:3],
                             "stops": len(names), "double": zone in calculator.get("double", ()),
                             "center": shape["center"] if shape else [float(net.stop_lat[members].mean()), float(net.stop_lon[members].mean())],
                             "polygons": shape["polygons"] if shape else [[self._outline(net.stop_lat[members], net.stop_lon[members])]]})
            rows.sort(key = lambda r: (not r["id"].isdigit(), int(r["id"]) if r["id"].isdigit() else 0, r["id"]))
            self.networks[network] = rows
        logger.info(f"  {len(self.networks)} fare networks, {sum(len(z) for z in self.networks.values())} zones")

    @staticmethod
    def _polygons(zones):
        """The zone polygons for the map: {(network, zone): name, municipalities, center, polygons}, the polygons as
        [[ring of [lat, lon]], ...], simplified to 25 m."""
        shapes = zones.assign(geometry = zones.geometry.simplify(25)).to_crs(4326)
        result = {}
        for network, zone, name, towns, geom in zip(shapes["network"], shapes["zone"], shapes["name"], shapes["municipalities"], shapes.geometry):
            parts = getattr(geom, "geoms", [geom])
            point = geom.representative_point()
            result[(network, zone)] = {"name": name, "municipalities": list(towns), "center": [point.y, point.x],
                                       "polygons": [[np.round(np.asarray(p.exterior.coords)[:, ::-1], 5).tolist()] for p in parts if not p.is_empty]}
        return result

    @staticmethod
    def _outline(lat, lon):
        """The convex hull of the stops ([lat, lon] points), or the stops when there are fewer than 3 of them."""
        points = np.unique(np.round(np.column_stack([lat, lon]), 5), axis = 0)
        if len(points) >= 3:
            try:
                points = points[ConvexHull(points * [1, np.cos(np.radians(lat.mean()))]).vertices]
            except Exception:   # aligned stops
                pass
        return points.tolist()


def create_app(net, pricing = None, tracks = None, alternatives_max = 6, optimize_default = "cost", crowding = None, date = None,
               profiles = None):
    """The FastAPI application: the API and the page.

    net: the Network of the whole feed (the journeys of a date are routed on net.on(date)). pricing, tracks, crowding,
    profiles: what load() and load_simulation() return (each can be None: the API then goes without it).
    alternatives_max: the most journeys a search returns. optimize_default: "cost" or "time" when a search does not
    say. date: the date shown first (else today if the timetable has it, else its first day)."""
    app = FastAPI(title = "GTFS router", description = "Public transport journeys and their prices")
    first_date, last_date = net.dates
    default_day = default_date(net, date)

    @functools.lru_cache(maxsize = 8)
    def router_on(day):
        """The Router on the network of a date (YYYYMMDD); the routers of the last 8 dates asked are kept."""
        return Router(net.on(day))
    stations = Stations(net)
    zones = Zones(net, pricing, tracks) if pricing is not None else None

    @functools.lru_cache(maxsize = 32)
    def pricing_with(changes):
        """The Pricing with the fares changed by changes, the JSON text the price editor of the page sends (the last 32
        kept); a 400 error if they are not valid."""
        try:
            return pricing.with_changes(json.loads(changes))
        except (ValueError, TypeError) as e:   # json.JSONDecodeError is a ValueError
            raise HTTPException(400, f"fares: {e}")

    def place(text):
        """A place of a search, "lat,lon" or "station:<id>": (what the router takes: (lat, lon) or the stops of the
        station, its description for the page: name, lat, lon and the station id). A place by coordinates is named after
        the nearest stop within 300 m."""
        if text.startswith("station:") and text[8:].isdigit():
            station = stations.get(int(text[8:]))
            return list(station["stops"]), {"name": station["name"], "lat": station["lat"], "lon": station["lon"], "station": int(station.name)}
        try:
            lat, lon = (float(v) for v in text.split(","))
        except ValueError:
            raise HTTPException(400, f'"{text}" is neither "lat,lon" nor "station:<id>"')
        near, dist = net.stops_near(lat, lon, 300)
        label = f"Near {net.stop_names[near[np.argmin(dist)]]}" if len(near) else f"{lat:.5f}, {lon:.5f}"
        return (lat, lon), {"name": label, "lat": lat, "lon": lon}

    def stop(s):
        """A stop for the page: name, position and id."""
        return {"name": str(net.stop_names[s]), "lat": float(net.stop_lat[s]), "lon": float(net.stop_lon[s]), "id": str(net.stop_ids[s])}

    def leave_late(journey, cost):
        """The journey leaving as late as it can: the router walks to the first vehicle right away and waits there, so
        the walk is moved to just before the vehicle leaves, and the wait before leaving is taken out of its cost."""
        legs = journey.legs
        if len(legs) > 1 and legs[0].kind == "walk" and legs[1].kind == "ride":   # from the origin (coordinates or a stop of it)
            first, ride = legs[0], legs[1]
            walk = Leg("walk", ride.dep - (first.arr - first.dep), ride.dep, first.from_stop, first.to_stop)
            return Journey(walk.dep, journey.arr, [walk] + legs[1:], journey.cost - cost.wait * (walk.dep - journey.dep))
        if legs and legs[0].kind == "ride":
            return Journey(legs[0].dep, journey.arr, legs, journey.cost - cost.wait * (legs[0].dep - journey.dep))
        return journey

    def ride_stops(leg):
        """The stops of a ride, from boarding to alighting, with their arrival and departure times."""
        begin, end = net.t_start[leg.trip], net.t_start[leg.trip + 1]
        trip_stops, dep = net.t_stop[begin:end], net.t_dep[begin:end]
        first = int(np.flatnonzero((trip_stops == leg.from_stop) & (dep == leg.dep))[0])
        rows = []
        for p in range(first, first + len(leg.stops)):
            s = int(trip_stops[p])
            rows.append(stop(s) | {"arr": hhmm(int(net.t_arr[begin + p])), "dep": hhmm(int(net.t_dep[begin + p]))})
        return rows

    def as_json(journey, origin, destination, origin_info, destination_info, traveller, via_info = (), prices = pricing):
        """A journey as the page shows it: its times, number of changes and legs (each ride with its line, stops, track,
        crowding and profile), and its price for the traveller with prices (the Pricing, with the fares of the search)."""
        legs = []
        reached = 0   # the via places given by coordinates reached (only they have walks to and from them)
        for i, leg in enumerate(journey.legs):
            if leg.kind == "stay":   # where the leg before it ended
                where = legs[-1]["to"] if legs else origin_info
                legs.append({"kind": "stay", "dep": hhmm(leg.dep), "arr": hhmm(leg.arr), "dep_s": leg.dep, "arr_s": leg.arr,
                             "duration_min": round((leg.arr - leg.dep) / 60), "from": where, "to": where,
                             "path": [[where["lat"], where["lon"]]]})
                continue
            if leg.from_stop >= 0:
                start = stop(leg.from_stop)
            else:
                start = origin_info if i == 0 or reached == 0 else via_info[reached - 1]
            if leg.to_stop >= 0:
                end = stop(leg.to_stop)
            elif i == len(journey.legs) - 1 or reached >= len(via_info):
                end = destination_info
            else:
                end, reached = via_info[reached], reached + 1
            row = {"kind": leg.kind, "dep": hhmm(leg.dep), "arr": hhmm(leg.arr), "dep_s": leg.dep, "arr_s": leg.arr,
                   "duration_min": round((leg.arr - leg.dep) / 60), "from": start, "to": end}
            if leg.kind == "ride":
                route = net.routes.iloc[net.trip_route[leg.trip]]
                stops = ride_stops(leg)
                row |= {"line": route["name"], "mode": route["mode"], "agency": route["agency"], "trip_id": str(net.trip_ids[leg.trip]),
                        "stops": stops, "path": tracks.path(net, leg) if tracks is not None else [[s["lat"], s["lon"]] for s in stops],
                        "crowding": crowding.ride(net, leg) if crowding is not None else None,
                        "profile": profiles.ride(net, leg) if profiles is not None else None}
            else:
                row["path"] = [[start["lat"], start["lon"]], [end["lat"], end["lon"]]]
            legs.append(row)
        rides = journey.rides
        result = {"dep": hhmm(journey.dep), "arr": hhmm(journey.arr), "dep_s": journey.dep, "arr_s": journey.arr,
                  "duration_min": round((journey.arr - journey.dep) / 60), "changes": max(len(rides) - 1, 0), "legs": legs,
                  "cost_min": round(journey.cost / 60, 1),
                  "crowding_level": max((l["crowding"]["level"] for l in legs if l.get("crowding")), default = None)}
        if prices is not None:
            price = prices.price(journey, net, traveller, origin if isinstance(origin, tuple) else None,
                                  destination if isinstance(destination, tuple) else None)
            result["price"] = {"chf": price.price, "rule": price.rule, "old_chf": price.old_price, "network_km": round(price.network_km, 1)}
        return result

    lat, lon = net.stop_lat[net.c_dep_stop], net.stop_lon[net.c_dep_stop]   # the extent of the departures

    iso = lambda day: pd.Timestamp(day).strftime("%Y-%m-%d")

    @app.get("/api/info")
    def info():
        """The default date, the first and last dates of the timetable, the number of stops, whether there are prices and
        which simulation gives the crowding, and the area of the departures (bounds: south-west and north-east)."""
        return {"date": iso(default_day), "first_date": iso(first_date), "last_date": iso(last_date), "stops": len(net.stop_ids), "pricing": pricing is not None,
                "crowding": crowding.source if crowding is not None else None,
                "bounds": [[float(np.percentile(lat, 1)), float(np.percentile(lon, 1))], [float(np.percentile(lat, 99)), float(np.percentile(lon, 99))]]}

    @app.get("/api/stations")
    def search_stations(q: str, limit: int = Query(10, le = 50)):
        """The stations whose name holds every word of q (without accents or case): the exact name first, then the
        names starting with it, then the others, the busiest first in each group."""
        return [{"id": int(r.id), "name": r.name, "lat": r.lat, "lon": r.lon, "departures": int(r.departures)}
                for r in stations.search(q, limit).itertuples()]

    @app.get("/api/fares")
    def fares():
        """The fares of the server, as the price editor shows them: "sbb" (minimum prices and price per km of each
        distance band), "default" (the distance-based price), "networks" (the prices of each fare network) and
        "discounts". A search can change any part of them with its fares parameter."""
        if pricing is None:
            raise HTTPException(404, "No pricing")
        return pricing.fares()

    @app.get("/api/networks")
    def networks():
        """The fare networks with zones: their id, number of zones and centre on the map."""
        if zones is None:
            return []
        return [{"id": n, "zones": len(z), "center": [float(np.mean([r["center"][0] for r in z])), float(np.mean([r["center"][1] for r in z]))]}
                for n, z in sorted(zones.networks.items())]

    @app.get("/api/networks/{network}/zones")
    def network_zones(network: str):
        """The zones of a fare network: id, name, municipalities, number of stops, whether it counts as two zones,
        centre and polygons."""
        if zones is None or network not in zones.networks:
            raise HTTPException(404, f"No fare network {network}")
        return zones.networks[network]

    @app.get("/api/route")
    def route(from_: str = Query(..., alias = "from"), to: str = Query(...), date: Optional[str] = None, time: str = Query("08:00", pattern = r"^\d{1,2}:\d{2}(:\d{2})?$"),
              alternatives: int = Query(3, ge = 1), age: int = 30, halbtax: bool = False, ga: bool = False, verbund_abo: bool = False,
              strecken_abo: bool = False, gleis7_abo: bool = False, junior_abo: bool = False, first_class: bool = False,
              student: bool = False, fares: str = "", home: Optional[str] = None, study: Optional[str] = None, subscriptions: str = "", optimize: str = Query(optimize_default, pattern = "^(cost|time)$"),
              via: List[str] = Query([]), stay: List[int] = Query([]), min_change: int = Query(0, ge = 0, le = 60)):
        """The journeys from one place to another.

        - from, to: "lat,lon" or "station:<id>" (an id of /api/stations).
        - date: YYYY-MM-DD or YYYYMMDD, a date of the timetable (default: the date of the server); time: HH:MM.
        - via (repeated): places to go through, in order, in the same forms; stay (repeated, one per via): the minutes
          to stay there (the journey goes on after it, with another ticket).
        - optimize: cost (the least generalized cost: fewer changes, less walking and waiting) or time (the earliest
          arrival). min_change: the least minutes between two vehicles, on top of the change time of the stops.
        - alternatives: how many journeys. The best journey is found, then the best one leaving after it, and so on, and
          in each gap the best one whose first vehicle leaves before the next found. They are listed by departure,
          without those another journey beats: it leaves no earlier and arrives no later, with no more changes, and
          saves more than MARGIN_S (5 minutes).
        - The traveller, for the price: age, student, halbtax, ga, verbund_abo, strecken_abo, gleis7_abo, junior_abo,
          first_class; home and study ("lat,lon": where they live and study, for the regional subscriptions and the
          discounts for residents); subscriptions (zone subscriptions: "ZVV:110,121;Mobilis:all").
        - fares: changes to the fares for this search, as JSON: a part of /api/fares (the price editor of the page).

        Returns the places (from, to, via) as found, the date and time, walk_min (the minutes to walk all the way, when a
        place is given by coordinates) and the journeys."""
        try:
            day = day_of(date) if date else default_day
        except ValueError:
            raise HTTPException(400, f'"{date}" is not a date (YYYY-MM-DD)')
        if not first_date <= day <= last_date:
            raise HTTPException(400, f"{iso(day)} is outside the timetable ({iso(first_date)} to {iso(last_date)})")
        router = router_on(day)
        origin, origin_info = place(from_)
        destination, destination_info = place(to)
        stay = list(stay) + [0] * (len(via) - len(stay))
        vias = [(place(v), minutes) for v, minutes in zip(via, stay) if v]
        via_points, via_info = [p for (p, _), _ in vias], [i for (_, i), _ in vias]
        stays = [max(minutes, 0) * 60 for _, minutes in vias]
        def point(text, name):
            try:
                lat, lon = (float(v) for v in text.split(","))
            except ValueError:
                raise HTTPException(400, f'{name}: "{text}" is not "lat,lon"')
            return lat, lon
        home_point, study_point = (point(v, name) if v else None for v, name in ((home, "home"), (study, "study")))
        traveller = Traveller(age = age, halbtax = halbtax, ga = ga, verbund_abo = verbund_abo, strecken_abo = strecken_abo,
                              gleis7_abo = gleis7_abo, junior_abo = junior_abo, first_class = first_class, student = student,
                              home = home_point, study = study_point, subscriptions = parse_subscriptions(subscriptions))
        prices = pricing_with(fares) if fares and pricing is not None else pricing

        wanted = min(alternatives, alternatives_max)
        cost = SWISS_COST if optimize == "cost" else EARLIEST_ARRIVAL
        changes = lambda j: max(len(j.rides) - 1, 0)

        def search(departure, latest = None):
            """The best journey leaving at departure (and whose first vehicle leaves before latest), or None."""
            journey = router.route(origin, destination, departure, cost = cost, via = via_points, stays = stays, latest = latest,
                                   min_change_s = 60 * min_change)
            return leave_late(journey, cost) if journey is not None else None

        via_stops = {int(v) for p in via_points if not isinstance(p, tuple) for v in p}

        def has_loop(journey):
            """Whether the journey comes back through a station (by name) it has left: Zürich HB - Oerlikon by S-Bahn, then
            a train back through Zürich HB. The places on the way start it again (a round trip through them is no loop)."""
            names = net.stop_names
            seen = set(names[origin]) if not isinstance(origin, tuple) else set()
            here = None
            for leg in journey.legs:
                if leg.kind == "stay":
                    continue
                if leg.from_stop >= 0:
                    here = names[leg.from_stop]
                    seen.add(here)
                if leg.kind == "walk":
                    if leg.to_stop < 0:   # at a place by coordinates (on the way, or the destination)
                        seen, here = set(), None
                        continue
                    path = [leg.to_stop]
                else:
                    path = leg.stops[1:]
                for stop_ in path:
                    name = names[stop_]
                    if name != here and name in seen:
                        return True
                    if stop_ in via_stops:
                        seen = set()
                    seen.add(name)
                    here = name
            return False

        # the best journey, then again leaving a minute after each journey leaves; and in each gap, the best journey whose
        # first vehicle leaves before the next one found (it lost to it, leaving later)
        found, gaps, departure = [], [], _seconds(time)
        for _ in range(3 * wanted):
            journey = search(departure)
            if journey is None:
                break
            if not has_loop(journey):   # a loop is left out, the search goes on after it
                found.append(journey)
            if not journey.rides or len(found) > wanted:
                break
            gaps.append((departure, journey.rides[0].dep - 60))
            departure = journey.rides[0].dep + 60
        for _ in range(3 * wanted):
            if not gaps:
                break
            start, latest = gaps.pop(0)
            journey = search(start, latest) if latest >= start else None
            if journey is not None and journey.rides and journey.rides[0].dep <= latest:
                if not has_loop(journey):
                    found.append(journey)
                gaps.append((start, journey.rides[0].dep - 60))

        # without the journeys another one beats: it leaves no earlier and arrives no later, with no more changes, and
        # saves more than MARGIN_S (leaving later and arriving earlier); the same journey found twice is kept once
        unique = {(j.dep, j.arr, tuple(r.trip for r in j.rides)): j for j in found}
        journeys = [j for j in unique.values() if not any(
            k.dep >= j.dep and k.arr <= j.arr and changes(k) <= changes(j) and (k.dep - j.dep) + (j.arr - k.arr) > MARGIN_S
            for k in unique.values())]
        journeys = sorted(journeys, key = lambda j: (j.dep, j.arr))[:wanted]

        walk_min = None
        if not vias and (isinstance(origin, tuple) or isinstance(destination, tuple)):
            a = origin if isinstance(origin, tuple) else (origin_info["lat"], origin_info["lon"])
            b = destination if isinstance(destination, tuple) else (destination_info["lat"], destination_info["lon"])
            k = 111_320
            walk_min = round(float(router._walk_s(np.hypot((a[0] - b[0]) * k, (a[1] - b[1]) * k * np.cos(np.radians(a[0]))))) / 60)
        return {"from": origin_info, "to": destination_info, "via": via_info, "date": iso(day), "time": time, "walk_min": walk_min,
                "journeys": [as_json(j, origin, destination, origin_info, destination_info, traveller,
                                     [i for p, i in zip(via_points, via_info) if isinstance(p, tuple)], prices) for j in journeys]}

    app.mount("/", _FreshFiles(directory = os.path.join(HERE, "web"), html = True), name = "web")
    return app


class _FreshFiles(StaticFiles):
    """The page and its files, checked again by the browser each time (else it keeps an old version after an update)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def hhmm(seconds):
    """The time of the day, 24 h: 24:05 of the timetable (after midnight) is 00:05."""
    minutes = int(seconds) // 60
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def _seconds(value):
    """Seconds after midnight of "HH:MM[:SS]"."""
    parts = [int(p) for p in value.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)


def main():
    """Starts the server with the settings of the config files and the command line."""
    parser = argparse.ArgumentParser(description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter)
    config = add_arguments(parser)
    parser.add_argument("--host", default = config.get("host") or "127.0.0.1")
    parser.add_argument("--port", type = int, default = config.get("port") or 8000)
    args = parser.parse_args()
    logging.basicConfig(level = logging.INFO, format = "%(message)s")

    import uvicorn
    crowding, profiles = load_simulation(args)
    app = create_app(*load(args), optimize_default = "cost" if args.routing == "cost" else "time", crowding = crowding, date = args.date,
                     profiles = profiles)
    uvicorn.run(app, host = args.host, port = args.port)


if __name__ == "__main__":
    main()
