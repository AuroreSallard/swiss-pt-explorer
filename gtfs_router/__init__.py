"""Swiss PT explorer: journeys on a GTFS timetable, with their price, crowding and fellow passengers.

The modules, in the order a journey goes through them:

- feed: reads the GTFS timetable into a Network (build_network), and the network of one date (Network.on);
- csa: the Router, which finds the best Journey between two places (Connection Scan Algorithm);
- pricing: the price of a journey under the Swiss fares (Pricing), and the changes of the price editor;
- tracks and zones: the real path of the rides and the fare zones they cross, used by the map and the pricing;
- crowding and profiles: how crowded the rides are and who travels on them, from a MATSim simulation;
- load: the settings (config.yml, config_local.yml) and command-line options shared by api.py and route_trips.py.
"""

from .feed import build_network, Network
from .csa import Router, Journey, Leg, Cost, EARLIEST_ARRIVAL, SWISS_COST, hhmm
from .pricing import Pricing
