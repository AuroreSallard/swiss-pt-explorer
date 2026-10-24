"""The options of the command line tools, with their defaults from config.yml and config_local.yml, and the network, the
pricing and the tracks they load."""
import json
import os

import pandas as pd
import yaml

from .crowding import build_crowding
from .profiles import build_profiles
from .csa import EARLIEST_ARRIVAL, SWISS_COST
from .feed import build_network, day_of
from .pricing import Pricing
from .tracks import build_tracks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILES = ("config.yml", "config_local.yml")   # in this order: the keys of config_local.yml (not in git) replace the others
PATHS = ("gtfs", "pricing", "network_data", "fares", "crowding", "cache")
NETWORK_FILES = {"municipalities": "tarifverbundkarte.geojson", "lines": "linie-mit-polygon.geojson",
                 "schedule": "corrected_schedule.xml.gz", "matsim_network": "corrected_network.xml.gz"}


def read_config(root = ROOT):
    """The settings of config.yml and config_local.yml in root; their relative paths are relative to root, ~ is the home
    folder, an empty value is None."""
    config = {}
    for name in CONFIG_FILES:
        path = os.path.join(root, name)
        if os.path.exists(path):
            with open(path) as f:
                config.update(yaml.safe_load(f) or {})
    for key in PATHS:
        if config.get(key):
            config[key] = os.path.join(root, os.path.expanduser(str(config[key])))
    if config.get("date") is not None:
        config["date"] = str(config["date"])
    return config


def add_arguments(parser):
    """The options shared by api.py and route_trips.py, with the defaults of the config files; returns the config."""
    config = read_config()
    parser.add_argument("--gtfs", required = not config.get("gtfs"), default = config.get("gtfs"),
                        help = "GTFS feed: zip or folder of .txt files (default: gtfs in the config)")
    parser.add_argument("--date", default = config.get("date"), help = "YYYYMMDD, a date of the feed: the date of the journeys (default: today, "
                                                                     "else the first date of the feed)")
    parser.add_argument("--pricing", default = config.get("pricing"), help = "Folder with the pricing inputs; empty: no prices")
    parser.add_argument("--fares", default = config.get("fares"),
                        help = "JSON file of changes to the fares of --pricing, as the price editor of the map exports them")
    parser.add_argument("--network-data", default = config.get("network_data"),
                        help = "Folder with " + ", ".join(NETWORK_FILES.values()) + ": the tracks and the zones they "
                               "cross; empty: straight lines and the zones of the stops")
    parser.add_argument("--routing", choices = ["cost", "earliest"], default = config.get("routing") or "cost",
                        help = "cost: the journey of least generalized cost (the eqasim utilities: in-vehicle time by mode, waiting, "
                               "walking, transfers; csa.SWISS_COST); earliest: the earliest arrival")
    parser.add_argument("--crowding", default = config.get("crowding"),
                        help = "Output folder of a MATSim simulation (pt_passenger_counts.csv.gz, output_transitSchedule.xml.gz, "
                               "output_transitVehicles.xml.gz): the crowding of the vehicles, by line, pair of stations and hour "
                             "(default: crowding in the config; empty: no crowding)")
    parser.add_argument("--crowding-sample", type = float, default = config.get("crowding_sample") or 0.05,
                        help = "Sample rate of the simulated population (0.05: 5 %%)")
    parser.add_argument("--cache", default = config.get("cache") or os.path.join(ROOT, "cache"), help = "Where the network and the tracks are kept")
    return config


def load_profiles(args):
    """Who travels on the vehicles, from the legs, trips and persons of the simulation of --crowding, or None."""
    return build_profiles(args.crowding, cache_dir = args.cache) if args.crowding else None


def load_crowding(args):
    """The crowding of the simulation of --crowding, or None."""
    return build_crowding(args.crowding, args.crowding_sample, cache_dir = args.cache) if args.crowding else None


def routing_cost(args):
    return SWISS_COST if args.routing == "cost" else EARLIEST_ARRIVAL


def default_date(net, date = None):
    """The date (YYYYMMDD) given, else today if it is in the feed, else the first date of the feed."""
    first, last = net.dates
    if date:
        return day_of(date)
    today = pd.Timestamp.today().strftime("%Y%m%d")
    return today if first <= today <= last else first


def load(args):
    """(network of the whole feed, pricing or None, tracks or None) of the arguments of add_arguments; the network of a
    date is network.on(date)."""
    files = {k: os.path.join(args.network_data, f) for k, f in NETWORK_FILES.items()} if args.network_data else {}
    files = {k: f for k, f in files.items() if os.path.exists(f)}
    net = build_network(args.gtfs, cache_dir = args.cache)
    if not args.pricing:
        return net, None, None
    tracks = None
    if "lines" in files and "municipalities" in files:
        tracks = build_tracks(net, Pricing(args.pricing), files["lines"], files["municipalities"], files.get("schedule"), files.get("matsim_network"),
                              cache_dir = args.cache)
    pricing = Pricing(args.pricing, tracks = tracks)
    if args.fares:
        with open(args.fares) as f:
            pricing = pricing.with_changes(json.load(f))
    return net, pricing, tracks
