"""The network, the pricing and the tracks of the command line tools, from the files of data/."""
import os

import pandas as pd

from .crowding import build_crowding
from .profiles import build_profiles
from .csa import EARLIEST_ARRIVAL, SWISS_COST
from .feed import build_network, day_of
from .pricing import Pricing
from .tracks import build_tracks

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CROWDING = os.path.expanduser("~/Documents/Results/20261018_60it_ch_5pct")   # the default simulation (Switzerland, 5 %)
NETWORK_FILES = {"municipalities": "tarifverbundkarte.geojson", "lines": "linie-mit-polygon.geojson",
                 "schedule": "corrected_schedule.xml.gz", "matsim_network": "corrected_network.xml.gz"}


def add_arguments(parser):
    parser.add_argument("--gtfs", required = True, help = "GTFS feed: zip or folder of .txt files")
    parser.add_argument("--date", default = None, help = "YYYYMMDD, a date of the feed: the date of the journeys (default: today, else the "
                                                         "first date of the feed)")
    parser.add_argument("--pricing", default = os.path.join(DATA, "pricing"), help = "Folder with the pricing inputs; empty: no prices")
    parser.add_argument("--network-data", default = os.path.join(DATA, "network"),
                        help = "Folder with " + ", ".join(NETWORK_FILES.values()) + ": the tracks and the zones they "
                               "cross; empty: straight lines and the zones of the stops")
    parser.add_argument("--routing", choices = ["cost", "earliest"], default = "cost",
                        help = "cost: the journey of least generalized cost (the eqasim utilities: in-vehicle time by mode, waiting, "
                               "walking, transfers; csa.SWISS_COST); earliest: the earliest arrival")
    parser.add_argument("--crowding", default = CROWDING if os.path.isdir(CROWDING) else None,
                        help = "Output folder of a MATSim simulation (pt_passenger_counts.csv.gz, output_transitSchedule.xml.gz, "
                               "output_transitVehicles.xml.gz): the crowding of the vehicles, by line, pair of stations and hour "
                             "(default: the Swiss simulation at 5 %%; empty: no crowding)")
    parser.add_argument("--crowding-sample", type = float, default = 0.05, help = "Sample rate of the simulated population (0.05: 5 %%)")
    parser.add_argument("--cache", default = os.path.join(os.path.dirname(DATA), "cache"), help = "Where the network and the tracks are kept")


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
    return net, Pricing(args.pricing, tracks = tracks), tracks
