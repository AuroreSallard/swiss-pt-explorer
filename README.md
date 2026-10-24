# GTFS router

A small router on a GTFS feed (Connection Scan Algorithm, earliest arrival) and the price of its journeys with the
pricing inputs of eqasim-switzerland (`data/pricing`).

```bash
python route_trips.py --gtfs ~/Documents/Data/pipeline/gtfs/gtfs_fp2026_20251214.zip --date 20260318 --trips examples/trips.csv --out results
```

`results/journeys.csv`: one row per trip (arrival, duration, rides, lines, price); `results/legs.csv`: the walks and
rides of each journey. The trips are on `--date` (default: today if the feed has it, else its first date), or on the
date of their `date` column (YYYYMMDD or YYYY-MM-DD) when there is one.

The whole feed is read once (all its trips and the days they run: 30 s and 1.7 GB of memory for the Swiss timetable
2026, 1.1 million trips) and kept in `cache/`; the network of a date (`network.on(date)`: the connections of the trips
running that day) takes 0.2 s, and a query then milliseconds (with numba, otherwise seconds). A trip of the day before
running after midnight (times after 24:00) is not in the network of a date.

From Python:

```python
from gtfs_router import build_network, Router, Pricing
feed = build_network("gtfs_fp2026_20251214.zip", cache_dir = "cache")   # the whole feed; feed.dates: its first and last dates
net = feed.on("20260318")                                               # or build_network(path, "20260318", cache_dir = "cache")
router, pricing = Router(net), Pricing("data/pricing")
journey = router.route((46.5168, 6.6291), (46.5191, 6.5668), "08:00")   # (lat, lon) or a stop id / station code
router.describe(journey), pricing.price(journey, net)
```

## Web API and map

```bash
pip install fastapi uvicorn
python api.py --gtfs ~/Documents/Data/pipeline/gtfs/gtfs_fp2026_20251214.zip --port 8000
```

http://localhost:8000: a map to search journeys (stops of the feed and addresses, or clicks on the map) on any date of
the feed, with their legs, intermediate stops and prices; a search can be shared by its URL
(`?from=46.5168,6.6291&to=station:28&date=2026-03-18&time=08:00`). `--date`: the date shown first (default: today if the
feed has it, else its first date).
The profile (top right, kept in the browser): age, travelcards and zone subscriptions, whose zones are chosen on the map.
The API (described at http://localhost:8000/docs):

- `GET /api/stations?q=lausanne`: stations by name (the stops of the same name within 1 km are one station).
- `GET /api/route?from=46.5168,6.6291&to=station:28&date=2026-03-18&time=08:00&alternatives=3&halbtax=true`: `date`
  (YYYY-MM-DD or YYYYMMDD) a date of the feed, else the default one; from and to are `lat,lon`
  or `station:<id>`; `via` (repeated) the places to go through in order, in the same forms, and `stay` (repeated, one
  per via) the minutes to stay there; `min_change` the least minutes between two vehicles (on top of the change time of
  the stops and the walks between them: the longest; the profile of the page, "Minimum transfer time"); the traveller has the parameters of the trips CSV (age, halbtax, ga...). The best journey,
  then the best one leaving after it, and so on, and in each gap the best journey whose first vehicle leaves before the
  next one found; listed by departure, without the journeys another one beats (it leaves no earlier and arrives no
  later, with no more changes, and saves more than 5 minutes, `MARGIN_S` in `api.py`: Rueras - Zürich HB at 09:35 via
  Andermatt, arriving 12:55, is kept next to the bus at 09:38 arriving 12:53). The walk to the first vehicle is moved to
  just before it leaves.
- `GET /api/info`: the default date, the first and last dates of the feed, its extent.
- `GET /api/networks`, `GET /api/networks/ZVV/zones`: the fare networks and their zones (name, municipalities, polygons),
  for the subscriptions: `subscriptions=ZVV:110,121;Mobilis:all` in `/api/route`.

One feed per server; the networks of the last 8 dates asked are kept. The rides are drawn along their tracks (below).
The page loads Leaflet (unpkg), the OpenStreetMap tiles and the address search of Photon (photon.komoot.io).

## Switzerland, tracks and zones

With the files of `data/network` (`--network-data`, empty to do without):

- `tarifverbundkarte.geojson` (the municipalities and their fare networks): with the zones of the
  stops of `gtfs_zones.csv`, the polygons of the zones (`gtfs_router/zones.py`): a municipality is in the zone of most of
  its stops (not counting the stops at the border of two zones), a municipality in several zones (Bern, Lausanne...) is
  split by the nearest stop, one with only stops at the border of zones (Wichtrach: Libero 626 and 710) is split between
  these zones (each part to the nearest one), one without stops takes the zones of the neighbour with the longest
  common border.
- The track between each pair of consecutive stops (`gtfs_router/tracks.py`): rail on `linie-mit-polygon.geojson`
  (the shortest path between the operating points near the stations, of several candidates: Dietikon BDWM or Bern RBS
  are other lines), bus on the links of the MATSim route with the same line and stops (`corrected_schedule.xml.gz`,
  `corrected_network.xml.gz`), the others (tram, boat...) and what is not found as a straight line. For 2026-03-18:
  97 % of the rail pairs on the lines (missing: the Littorail of Neuchâtel, the end of the line to Les Diablerets, a few
  stations), 51682 bus pairs from MATSim.
- A ride is in the zones its track crosses on at least 200 m and in the zone of each of its stops; a stop at the border
  of two zones (Stettbach: ZVV 110 and 121) adds only the one its track goes through most. At the first and last stops
  of a ride, when they are at a border, the track next to them in their zones belongs to them: of these zones, the ride
  keeps those it needs anyway (another stop in it), else the one it goes through most (Stettbach - Zürich HB passes
  under Dübendorf, 121, in the Zürichberg tunnel: 110 only; Stettbach - Winterthur: 121, 122, 120). Its distance is
  the length of the track.

Built once per feed (for all its dates) and kept in `cache/` with the network.

## Crowding

```bash
python api.py --gtfs ~/Documents/Data/pipeline/gtfs/gtfs_fp2026_20251214.zip --crowding ~/Documents/Results/20260914_geneva_10pct/simulation_output --crowding-sample 0.1
```

The default: the simulation of Switzerland at 5 % (`~/Documents/Results/20261018_60it_ch_5pct`, `--crowding-sample 0.05`)
when its folder exists; another one with `--crowding` and its `--crowding-sample` (above: Geneva at 10 %);
`--crowding ""`: without crowding.

The crowding of the vehicles of a MATSim simulation (`gtfs_router/crowding.py`): the load of each simulated vehicle
between consecutive stops (the boardings minus alightings of `pt_passenger_counts.csv.gz` along the stops of its route
in `output_transitSchedule.xml.gz`, divided by the sample rate), against the capacity of its type in
`output_transitVehicles.xml.gz` (seats and standing room: bus 70, tram 180, train 400... in the Geneva simulation).
The simulation can have another timetable than the feed: the loads are averaged by line name, pair of stations and hour
(the next or previous hour, or the station without the next one, when missing), and a ride takes the most crowded of
its segments: level 1 below 50 % of the capacity, 2 below 80 %, 3 above (`crowding.LEVELS`). The page shows it as
the person icons of the SBB app and the level of each segment (not the numbers of passengers); `route_trips.py --crowding` adds the columns
crowding_level, crowding_load and crowding_occupancy. Only the area of the simulation has meaningful loads (the lines
leaving it lose the passengers from outside).

## Profile of the travellers

With `output_legs.csv.gz`, `output_trips.csv.gz` and `output_persons.csv.gz` in the folder of `--crowding`, the
distributions of the simulated passengers of each ride (`gtfs_router/profiles.py`): age, sex, trip purpose (the activity
at the end of the trip), employment, travelcard, car availability, income per person and home municipality type, in the
morning peak (06:00-09:00), the evening peak (16:00-19:00) or off-peak (by the boarding time). Each leg is split into the
sections of its route between its boarding and alighting stops, and its person counts on each of them, by line, pair of
stations and period (matched as for the crowding); a ride adds up its sections (the people on board with the traveller,
weighted by the sections they share). With fewer than 30 simulated people (`MIN_PEOPLE`): the same sections all day,
else the whole line in the period (each leg once), else the whole line all day, else none. The page shows them in the
details of a ride ("Profile of travellers on this train", bus...): a bar per category, and a tick for all the public
transport legs of the period (or of the day). For 20.10.2026, on random rides: 68 % of the trains, 97 % of the trams and
46 % of the buses with a profile of their own sections (in the period or all day), 7 % of the buses and 19 % of the
trains without any; few cable cars and funiculars with any. Built once (about 30 s and 2 GB for Switzerland at 5 %) and kept in `cache/`.

## Router

- Stops on the way (`via`): the journey to the first one, then from it at the arrival time, and so on; a vehicle going on
  through a station stays one ride (the parts of a trip are merged), a place given by coordinates is walked to and from.
  A stay at a stop on the way (minutes) is a "stay" leg: the journey goes on after it in another vehicle, the latest one
  that arrives as early (waiting more is staying longer), and each part is priced as a ticket of its own.
- Connections of the trips active on the date, scanned by departure time; the journey of least generalized cost
  (`--routing cost`, the default; `optimize=cost` in the API) or with the earliest arrival (`--routing earliest`,
  `optimize=time`).
- The generalized cost (`csa.Cost`, `csa.SWISS_COST` from the eqasim utilities, in hours of rail in-vehicle time): the
  in-vehicle time weighted by mode (rail 1, tram and metro 1.12, bus and the others 1.58), waiting 1.72, walking 1.97,
  and per transfer 0.10 h rail to rail, 0.18 h between other modes, 0.22 h between rail and another mode. As
  SwissRailRaptor, one label per stop (the cheapest, with the time it is reached): a cheaper label reached later replaces
  a dearer one reached earlier. With every weight 1 and no penalty, the cost is the arrival time (the same journeys as
  the earliest-arrival router, checked on 400 queries). About 10 ms per query.
- A trip can be boarded at a stop reached on foot, or reached by another vehicle at least the change time of the stop
  before (`transfers.txt` rule from the stop to itself, else 60 s).
- Walks between stops within 400 m: the `transfers.txt` time when there is a rule, else the beeline distance x 1.3 at
  1.2 m/s (rules between stops further apart are used too). Access and egress: stops within 800 m of the coordinates,
  same walking speed; a direct walk is used when it costs less (arrives first, with the earliest arrival).

## Pricing

`gtfs_router/pricing.py` is a port of the eqasim-java pricing (switzerland, `ch_cmdp`: `RunComputeTransitPrices`,
`SwissPtRoutePredictor`, `SwissPtVariables`, `ZoneBasedPricing`, `SBBPtStageCostCalculator`,
`DefaultDistanceBasedCalculator` and the readers of `utils/pricing/inputs`), with the inputs of `data/pricing`:

1. Each ride gets the fare networks common to all its stops (`gtfs_zones.csv`, plus "SBB" for the stations of
   `SBB_all_distances.csv`), the highest priority only (networks 2, Z-Pass 1, SBB 0); none: "None" (default price).
   The zones of a stop are deduplicated by zone id, keeping the highest priority network.
2. A network common to all the rides prices the whole journey (the highest priority); otherwise each ride goes to a
   network it shares with another ride (the lowest priority) or to its own one (the highest priority).
3. Zone networks: the price list by number of zones (zones counting as two count twice); Unireso: centre zone price plus
   the additional price for each other zone (default price if the centre zone is not visited); age discounts
   (`targetedPricing`). SBB: the tariff distances added up, rounded by steps, each km at the price of its bin, rounded
   up (0.20 below 69 km, else 1 CHF), half with the half-fare card, at least the minimum. Default: flat + per km x
   network distance + power2 x distance^2, half with the half-fare card, rounded the same way.
4. Free: GA, under 6, under 16 with the Junior card, Gleis 7 (under 25, 19:00-05:00), a local subscription when origin
   and destination are within 10 km of home. 1st class (`first_class`): each price before its rounding times 1.7, the SBB standard (in the zone networks too
   for now: the zone price times 1.7 rounded up to 20 Rp). Zone subscriptions (`Traveller.subscriptions`, `{network: zones or None}`): a stop is covered when
   one of its zones is in a subscription. A ride with all its stops covered is free; one leaving the subscription
   is priced from its last covered stop, without the zones where all its stops are covered (an extension ticket). A
   ride leaving and entering the zones again is paid in full. Half fare: half-fare card or under 16. Capped at 60 CHF (35 with half fare) with `Pricing(folder, cap = True)` only.
   `old_price_chf` is the old distance-based model.

Differences with Java: the zones of a ride are those of its track (above; only those of its stops in Java), the network
distance of a ride is the length of its track (the MATSim route distance in Java; the sum of the beeline distances
between the stops without `data/network`), and the 10 km radius of the local subscriptions is in km here (Java compares the distance in
metres to 10).
