"""What api.py and route_trips.py share: their settings, their command-line options, and the loading of the data.

The settings come from config.yml (in git, the shared defaults) and config_local.yml (not in git, the settings of one
computer, whose keys replace those of config.yml). An option given on the command line wins over both. load() then
builds what the options point to: the network of the timetable, the prices, the tracks, the crowding and the profiles,
each from the cache folder when it was built before.
"""
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
PATHS = ("gtfs", "pricing", "network_data", "fares", "crowding", "cache")   # the settings that are paths
# the files of the network data folder: the municipalities and their fare networks, the SBB rail lines, and the MATSim
# schedule and network (for the bus routes)
NETWORK_FILES = {"municipalities": "tarifverbundkarte.geojson", "lines": "linie-mit-polygon.geojson",
                 "schedule": "corrected_schedule.xml.gz", "matsim_network": "corrected_network.xml.gz"}


def read_config(root = ROOT):
    """The settings of config.yml and config_local.yml in the folder root, as a dict. Relative paths are made relative
    to root, ~ is the home folder, and an empty value is None."""
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
    """Adds the options shared by api.py and route_trips.py to an argparse parser, with the values of the config files
    as defaults, and returns the config (for the options of each script, such as the port of api.py)."""
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
    """The Profiles of the simulation of --crowding (who travels on the vehicles), or None without a simulation or
    without its legs, trips and persons."""
    return build_profiles(args.crowding, cache_dir = args.cache) if args.crowding else None


def load_crowding(args):
    """The Crowding of the simulation of --crowding, or None without a simulation."""
    return build_crowding(args.crowding, args.crowding_sample, cache_dir = args.cache) if args.crowding else None


def routing_cost(args):
    """The Cost of --routing: the least generalized cost (SWISS_COST) or the earliest arrival."""
    return SWISS_COST if args.routing == "cost" else EARLIEST_ARRIVAL


def default_date(net, date = None):
    """The date to use (YYYYMMDD): the one given, else today if the timetable covers it, else its first day."""
    first, last = net.dates
    if date:
        return day_of(date)
    today = pd.Timestamp.today().strftime("%Y%m%d")
    return today if first <= today <= last else first


def load(args):
    """(the Network of the whole feed, the Pricing or None, the Tracks or None), from the options of add_arguments.

    There are no prices without --pricing, and no tracks without the rail lines and the municipalities in
    --network-data. With --fares, the changes of that file are applied to the prices. The network of a date is
    network.on(date)."""
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
