"""What api.py and route_trips.py share: their settings, their command-line options, and the loading of the data.

The settings come from config.yml (in git, the shared defaults) and config_local.yml (not in git, the settings of one
computer, whose keys replace those of config.yml). An option given on the command line wins over both. load() then
builds what the options point to: the network of the timetable, the prices and the tracks, each from the cache folder
when it was built before; load_simulation does the same for the crowding and the profiles.
"""
import datetime
import json
import logging
import os
import pickle

import pandas as pd
import yaml

from . import crowding as crowding_module, profiles as profiles_module
from .crowding import build_crowding
from .profiles import build_profiles
from .csa import EARLIEST_ARRIVAL, SWISS_COST
from .feed import build_network, day_of
from .pricing import Pricing
from .tracks import build_tracks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILES = ("config.yml", "config_local.yml")   # in this order: the keys of config_local.yml (not in git) replace the others
PATHS = ("gtfs", "pricing", "network_data", "fares", "crowding", "crowding_cache", "cache")   # the settings that are paths
# the files of the network data folder: the municipalities and their fare networks, the SBB rail lines, and the MATSim
# schedule and network (for the bus routes)
NETWORK_FILES = {"municipalities": "tarifverbundkarte.geojson", "lines": "linie-mit-polygon.geojson",
                 "schedule": "corrected_schedule.xml.gz", "matsim_network": "corrected_network.xml.gz"}
logger = logging.getLogger(__name__)


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
                             "(default: crowding in the config; empty: no crowding, unless --crowding-cache has it)")
    parser.add_argument("--crowding-sample", type = float, default = config.get("crowding_sample") or 0.05,
                        help = "Sample rate of the simulated population (0.05: 5 %%)")
    parser.add_argument("--crowding-cache", default = config.get("crowding_cache"),
                        help = "A .pkl file with the crowding and the profiles of the simulation: written when the simulation of "
                               "--crowding is there, read when it is not (a server can run with this file only); empty: none")
    parser.add_argument("--cache", default = config.get("cache") or os.path.join(ROOT, "cache"), help = "Where the network and the tracks are kept")
    return config


def load_simulation(args, with_profiles = True):
    """(the Crowding, the Profiles): how crowded the vehicles are and who travels on them, from the simulation of
    --crowding; each can be None.

    When the output files of the simulation are there, both are built from them (or from the cache folder), and saved
    together in the --crowding-cache file, if any. When they are not, both are read from the --crowding-cache file, so
    a server can run without the simulation. with_profiles = False: the crowding only, and no --crowding-cache file
    written (route_trips.py)."""
    folder, saved_path = args.crowding, args.crowding_cache
    if folder and all(os.path.exists(os.path.join(folder, f)) for f in crowding_module.FILES):
        crowding = build_crowding(folder, args.crowding_sample, cache_dir = args.cache)
        profiles = build_profiles(folder, cache_dir = args.cache) if with_profiles else None
        if saved_path and with_profiles:
            _save_simulation(saved_path, folder, args.crowding_sample, crowding, profiles)
        return crowding, profiles
    if saved_path and os.path.exists(saved_path):
        with open(saved_path, "rb") as f:
            about = pickle.load(f)   # what the file holds, written before the data (see _save_simulation)
            crowding, profiles = pickle.load(f)
        logger.info(f"Crowding and profiles from {saved_path}: the simulation {about['source']} (sample {about['sample']:g}), "
                    f"saved on {about['saved']}{'' if profiles is not None else ', without profiles'}")
        return crowding, profiles if with_profiles else None
    if folder:
        raise FileNotFoundError(f"No simulation in {folder} (it needs {', '.join(crowding_module.FILES)})"
                                + (f", and no file {saved_path}" if saved_path else ""))
    if saved_path:
        logger.warning(f"No crowding and no profiles: the crowding_cache file {saved_path} does not exist, and there is no simulation")
    else:
        logger.info("No crowding and no profiles: neither crowding nor crowding_cache is set")
    return None, None


def _save_simulation(path, folder, sample, crowding, profiles):
    """Saves the crowding and the profiles of a simulation in one file, unless it already holds them.

    The file holds two pickles: first a small dict about the data (the simulation folder, its sample rate, the date it
    was saved, and a stamp of the files of the simulation and of the versions of the code), then (crowding, profiles).
    The stamp is compared with the one of the file, read without loading the data, to know whether to write it again."""
    files = [os.path.join(folder, f) for f in dict.fromkeys(crowding_module.FILES + profiles_module.FILES)]
    stamp = (f"{crowding_module.VERSION}|{profiles_module.VERSION}|{sample}|"
             + "|".join(f"{os.path.abspath(p)}:{os.path.getmtime(p)}" for p in files if os.path.exists(p)))
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                if pickle.load(f).get("stamp") == stamp:
                    return
        except Exception:   # an unreadable file: written again
            pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok = True)
    about = {"source": folder, "sample": sample, "saved": datetime.date.today().isoformat(), "stamp": stamp}
    with open(path + ".tmp", "wb") as f:   # written aside, then put in place: a server reading it never sees half a file
        pickle.dump(about, f, protocol = pickle.HIGHEST_PROTOCOL)
        pickle.dump((crowding, profiles), f, protocol = pickle.HIGHEST_PROTOCOL)
    os.replace(path + ".tmp", path)
    logger.info(f"Crowding and profiles saved in {path} ({os.path.getsize(path) / 1e6:.0f} MB)")


def load_crowding(args):
    """The Crowding of the simulation of --crowding (or of the --crowding-cache file), or None without a simulation."""
    return load_simulation(args, with_profiles = False)[0]


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
