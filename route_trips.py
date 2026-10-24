"""Routes a list of trips on a GTFS feed and prices them.

    python route_trips.py --gtfs gtfs_fp2026_20251214.zip --date 20260318 --trips examples/trips.csv --out results

The trips CSV has the columns id, departure (HH:MM), optionally date (YYYYMMDD or YYYY-MM-DD, else --date) and either origin_lat, origin_lon, destination_lat,
destination_lon, or origin_stop, destination_stop (GTFS stop ids or station codes, e.g. 8501120), and optionally the
traveller: age, halbtax, ga, verbund_abo, strecken_abo, gleis7_abo, junior_abo, first_class (true/false), home_lat, home_lon, subscriptions (zones,
e.g. "ZVV:110,121;Mobilis:all"), via (places to go through, separated by ";": stop ids or "lat,lon", with @minutes to stay there, e.g. 8507000@30). Writes
<out>/journeys.csv (one row per trip: times, number of changes, price) and <out>/legs.csv (one row per leg). With
--crowding <MATSim output folder>: the crowding of the rides (level 1 to 3, load, occupancy of the busiest segment).
"""
import argparse
import logging
import os
import time

import pandas as pd

from gtfs_router import Router, hhmm
from gtfs_router.load import add_arguments, default_date, load, load_crowding, routing_cost
from gtfs_router.pricing import Traveller, parse_subscriptions



def place(row, prefix):
    if f"{prefix}_stop" in row and isinstance(row[f"{prefix}_stop"], str) and row[f"{prefix}_stop"]:
        return row[f"{prefix}_stop"]
    return float(row[f"{prefix}_lat"]), float(row[f"{prefix}_lon"])


def vias(row):
    """(places, stays in seconds) of the via column: places separated by ';', each a stop id (or station code) or
    "lat,lon", optionally followed by @minutes to stay there (8507000@30)."""
    places, stays = [], []
    for item in filter(None, (part.strip() for part in str(row.get("via", "") or "").split(";"))):
        item, _, minutes = item.partition("@")
        if "," in item:
            lat, lon = (float(v) for v in item.split(","))
            places.append((lat, lon))
        else:
            places.append(item.strip())
        stays.append(int(float(minutes) * 60) if minutes.strip() else 0)
    return places, stays


def traveller(row, halbtax, first_class):
    flag = lambda name: str(row.get(name, "")).strip().lower() in ("true", "1", "yes")
    home = (float(row["home_lat"]), float(row["home_lon"])) if row.get("home_lat") and row.get("home_lon") else None
    return Traveller(age = int(row["age"]) if row.get("age") else 30, halbtax = flag("halbtax") or halbtax, ga = flag("ga"),
                     verbund_abo = flag("verbund_abo"), strecken_abo = flag("strecken_abo"), gleis7_abo = flag("gleis7_abo"),
                     junior_abo = flag("junior_abo"), first_class = flag("first_class") or first_class, home = home,
                     subscriptions = parse_subscriptions(row.get("subscriptions", "")))


def main():
    parser = argparse.ArgumentParser(description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    parser.add_argument("--trips", required = True)
    parser.add_argument("--out", default = "results")
    parser.add_argument("--halbtax", action = "store_true", help = "Every traveller has the half-fare card")
    parser.add_argument("--first-class", action = "store_true", help = "Every traveller travels in 1st class")
    args = parser.parse_args()
    logging.basicConfig(level = logging.INFO, format = "%(message)s")

    feed, pricing, _ = load(args)
    crowding = load_crowding(args)
    date = default_date(feed, args.date)
    routers = {}   # date -> router on the network of the date

    def router_on(day):
        day = default_date(feed, day or date)
        if day not in routers:
            routers[day] = Router(feed.on(day), cost = routing_cost(args))
        return routers[day]

    trips = pd.read_csv(args.trips, dtype = str, keep_default_na = False)
    journeys, legs = [], []
    started = time.time()
    for _, row in trips.iterrows():
        origin, destination = place(row, "origin"), place(row, "destination")
        via, stays = vias(row)
        router = router_on(row.get("date") or None)
        network = router.net
        journey = router.route(origin, destination, row["departure"], via = via, stays = stays)
        record = {"id": row["id"], "date": network.date, "departure": row["departure"]}
        if journey is None:
            journeys.append(record | {"found": False})
            continue
        rides = journey.rides
        record |= {"found": True, "arrival": hhmm(journey.arr), "duration_min": round((journey.arr - journey.dep) / 60, 1),
                   "rides": len(rides), "changes": max(len(rides) - 1, 0),
                   "lines": " > ".join(network.routes.iloc[network.trip_route[r.trip]]["name"] for r in rides)}
        if pricing is not None:
            result = pricing.price(journey, network, traveller(row, args.halbtax, args.first_class),
                                   origin if isinstance(origin, tuple) else None, destination if isinstance(destination, tuple) else None)
            record |= {"price_chf": result.price, "price_rule": result.rule, "old_price_chf": result.old_price, "network_km": round(result.network_km, 1)}
        rows = [{"id": row["id"], "leg": k} | described for k, described in enumerate(router.describe(journey))]
        if crowding is not None:
            for leg, leg_row in zip(journey.legs, rows):
                found = crowding.ride(network, leg) if leg.kind == "ride" else None
                if found:
                    leg_row |= {"crowding_level": found["level"], "crowding_load": found["load"], "crowding_occupancy": found["occupancy"]}
            levels = [r["crowding_level"] for r in rows if "crowding_level" in r]
            record["crowding_level"] = max(levels) if levels else None
        journeys.append(record)
        legs += rows

    os.makedirs(args.out, exist_ok = True)
    pd.DataFrame(journeys).to_csv(os.path.join(args.out, "journeys.csv"), index = False)
    pd.DataFrame(legs).to_csv(os.path.join(args.out, "legs.csv"), index = False)
    print(pd.DataFrame(journeys).to_string(index = False))
    print(f"{len(trips)} trips in {time.time() - started:.1f} s, written to {args.out}")


if __name__ == "__main__":
    main()
