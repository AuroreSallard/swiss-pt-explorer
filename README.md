# Swiss PT explorer

Swiss PT explorer is a journey planner for public transport in Switzerland. You search a journey from A to B, as in
the SBB app, and for each journey it shows:

- **the route**: the trains, trams, buses and walks, with their times and intermediate stops;
- **the price** of the ticket, depending on the traveller (age, half-fare card, GA, zone subscriptions...);
- **how crowded each vehicle is**, as the person icons of the SBB app (one to three);
- **who the other passengers are**: their age, sex, trip purpose, employment and so on.

The timetable comes from the official Swiss GTFS feed. The crowding and the passengers come from a **MATSim
simulation** of [eqasim-switzerland](https://github.com/eqasim-org/eqasim-switzerland/tree/cmdp): an agent-based model where a synthetic population of Switzerland travels
through a whole day.

The prices try to reflect how fragmented [public transport fares](https://www.allianceswisspass.ch/de/tarife-vorschriften/uebersicht) are in Switzerland. A single journey can combine SBB
trains, priced by distance, with the zones of one or several regional fare networks (Verbünde such as ZVV in Zürich or
Unireso in Geneva), and with the many local operators, whose prices are hard to capture. The tool rebuilds the ticket of each journey from these pieces. To make this possible, the fares
are written in a small description format of their own (see [Prices](#prices)). On the map, a **price editor** changes
any of them (an SBB price per kilometre, the price list of a network...) and adds discounts, by age or for students,
and every journey is then priced with the new fares. Anyone can use it to play with prices.

You can use it in three ways: a map in the browser, a web API behind that map, and a script that computes a whole list
of trips from a CSV file.

## Getting started

Install the dependencies:

```bash
pip install fastapi uvicorn pyyaml
```

Then tell the tool where your files are. The settings are in two files next to `api.py`:

- `config.yml`, in git, has the shared defaults and explains each setting. Leave your own paths out of it.
- `config_local.yml`, which you create and which git ignores, has the settings of your computer. Its keys replace
  those of `config.yml`. For example:

  ```yaml
  gtfs: ~/data/gtfs_fp2026_20251214.zip                # the timetable: a GTFS zip, or a folder of its .txt files
  crowding: ~/simulations/ch_5pct/simulation_output    # the output folder of a MATSim simulation
  crowding_sample: 0.05                                # the share of the population it simulated (5 %)
  ```

Relative paths are relative to the folder of the config files, and `~` is your home folder. Both the crowding and the
passenger profiles come from the `crowding` folder; without it, the app runs without them.

Start the server:

```bash
python api.py
```

and open http://localhost:8000.

Every setting can also be given on the command line, where it wins over the config files: `--gtfs`, `--crowding`,
`--crowding-sample`, `--date` (the date the page shows first; by default today if the timetable covers it, else its
first day), `--port`... `--crowding ""` turns off the crowding and profiles. `python api.py --help` lists them all.

The first start takes a while: reading the whole Swiss timetable takes about 30 s and 1.7 GB of memory (1.1 million
trips in 2026), and building the passenger profiles about 30 s and 2 GB for Switzerland at 5 %. Everything is then
saved in `cache/`, so the next starts are much faster. Expect the server to need 4–6 GB of RAM in total.

## The map

On the page you choose an origin and a destination: a stop, an address, or a click on the map. You can pick any date
the timetable covers and a departure time. The results list several journeys, each with its legs, intermediate stops
and price. The rides are drawn along the real rail tracks and bus routes (see [Tracks and zones](#tracks-and-zones)).

Opening a ride shows its crowding segment by segment and, under "Profile of travellers on this train" (or bus, tram...),
who travels on it.

The **profile** button (top right) describes the traveller: age, student status, travelcards, zone subscriptions (whose
zones you pick on the map) and the minimum transfer time. It is kept in the browser.

The **fares** button (the tag, under the profile) opens the price editor:

- **Discounts**: a share off the tickets, for an age range, for students only (or non-students only), on all networks
  or on some of them. When several apply to a ticket, the largest one is taken; they don't add up. They come on top of
  the half-fare card, and free travel (GA, under 6, Junior card, Gleis 7) stays free. The age discounts of the fare
  description (in Unireso, free under 19) are listed here too, and can be changed or removed.
- **Fare networks**: the price list of a network by number of zones, full price and half-fare (or, for Unireso, the
  price of the centre zone and of each other zone, full price and half-fare).
- **SBB, by distance**: the price per kilometre of each distance band, and the minimum prices.
- **Outside the fare networks**: the distance-based price of the rides in no network.

The results follow each change. Prices computed with your fares are marked ✎, and the breakdown of a ticket names the
discount used. Your changes are kept in your browser and only change your own searches: other visitors, and shared
URLs, keep the original fares. **Export** saves them as a JSON file, which **Import** loads again, and which the server
and `route_trips.py` can use as their fares (see [Prices](#prices)).

The line at the bottom of the page opens **Methods and sources**, a short version of [How it works](#how-it-works) for
visitors, and links to the source code on GitHub.

A search can be shared by its URL, for example `?from=46.5168,6.6291&to=station:28&date=2026-03-18&time=08:00`.

The page loads Leaflet (from unpkg), the OpenStreetMap tiles and the Photon address search (photon.komoot.io).

## The API

The page only calls the API, and you can call it too. Its full description is at http://localhost:8000/docs.

- `GET /api/route`: the journeys between two places. Example:
  `/api/route?from=46.5168,6.6291&to=station:28&date=2026-03-18&time=08:00&alternatives=3&halbtax=true`
  - `from` and `to` are coordinates (`lat,lon`) or a station (`station:<id>`).
  - `date` is `YYYY-MM-DD` or `YYYYMMDD`; a date outside the timetable falls back to the default date.
  - `via` (repeatable) adds places to pass through, in order, and `stay` (one per `via`) the minutes to stay at each.
  - `min_change` is the least number of minutes between two vehicles. It applies on top of the change time of each
    stop: the longer of the two is used.
  - `optimize=cost` (the default) or `optimize=time`: see [How a journey is found](#how-a-journey-is-found).
  - The traveller is described by the same parameters as in the trips CSV: `age`, `student`, `halbtax`, `ga`,
    `verbund_abo`... and `subscriptions`, for example `subscriptions=ZVV:110,121;Mobilis:all`.
  - `fares` changes the fares for this search: a JSON object with some of the parts of `/api/fares` (`sbb`, `default`,
    single networks of `networks`, `discounts`), each given whole. The page sends its changes this way.
- `GET /api/stations?q=lausanne`: stations by name. Stops with the same name within 1 km of each other count as one
  station.
- `GET /api/info`: the default date, the first and last dates of the timetable, and the area it covers.
- `GET /api/fares`: the fares of the server, as the price editor shows them: the SBB distance bands and minimum prices,
  the distance-based default price, the price list of each network, and the discounts.
- `GET /api/networks` and `GET /api/networks/ZVV/zones`: the fare networks and their zones (names, municipalities,
  polygons), used to choose subscriptions.

**Which journeys are listed.** The API first finds the best journey, then the best one leaving after it, and so on. It
also looks in each gap for a journey whose first vehicle leaves before the next one found. The journeys are listed by
departure time, and a journey is dropped when another one beats it: it leaves no earlier, arrives no later, has no
more changes and saves more than 5 minutes (`MARGIN_S` in `api.py`). For example, Rueras – Zürich HB at 09:35 via
Andermatt, arriving 12:55, is kept next to the bus at 09:38 arriving 12:53. The walk to the first vehicle is moved to
just before it leaves, so you don't wait at the stop.

A server holds one timetable and keeps in memory the networks of the last 8 dates asked for.

## Computing a list of trips

`route_trips.py` computes and prices every trip of a CSV file:

```bash
python route_trips.py --date 20260318 --trips examples/trips.csv --out results
```

Each row of the CSV is a trip: an `id`, a `departure` time (HH:MM), and either the coordinates of its origin and
destination (`origin_lat`, `origin_lon`, `destination_lat`, `destination_lon`) or their stops (`origin_stop`,
`destination_stop`). Optional columns describe the traveller (`age`, `student`, `halbtax`, `ga`, `subscriptions`...), stops on
the way (`via`) and a `date`. See `examples/trips.csv` and the top of `route_trips.py` for the full list.

The script writes two files:

- `results/journeys.csv`: one row per trip, with its arrival, duration, rides, lines and price;
- `results/legs.csv`: the walks and rides of each journey.

The trips are on `--date` (by default today if the timetable covers it, else its first day), unless the CSV has a
`date` column (YYYYMMDD or YYYY-MM-DD). It uses the same config files as the server. With a simulation (`crowding`),
the files also get the columns `crowding_level`,
`crowding_load` and `crowding_occupancy`.

## From Python

```python
from gtfs_router import build_network, Router, Pricing
feed = build_network("gtfs_fp2026_20251214.zip", cache_dir = "cache")   # the whole timetable; feed.dates: its first and last dates
net = feed.on("20260318")                                               # the network of one day
router, pricing = Router(net), Pricing("data/pricing")
journey = router.route((46.5168, 6.6291), (46.5191, 6.5668), "08:00")   # (lat, lon), a stop id or a station code
router.describe(journey), pricing.price(journey, net)
```

`build_network` reads the whole timetable once: every trip and the days it runs. `feed.on(date)` then keeps the trips
running that day, which takes 0.2 s. A search takes milliseconds with numba installed, and seconds without it.

## How it works

### How a journey is found

A GTFS timetable is, in the end, a long list of **connections**: a vehicle leaving one stop at a given time and
arriving at the next one. To find a journey, the router (`gtfs_router/csa.py`) uses the **Connection Scan Algorithm**:
it goes through the connections of the day in order of departure, and keeps track of the best way found so far to
reach each stop.

"Best" can mean two things:

- **Least generalized cost** (the default: `--routing cost`, or `optimize=cost` in the API). Every minute is weighted
  by how unpleasant it is, with the weights the Swiss MATSim model ([eqasim-switzerland](https://github.com/eqasim-org/eqasim-switzerland/tree/cmdp)) uses to choose routes (`csa.SWISS_COST`). These
  weights are expressed in hours of train travel: a minute on a train counts 1, on a tram or metro 1.12, on a bus or
  other mode 1.58; a minute waiting counts 1.72 and a minute walking 1.97. Each change adds 0.10 h between two trains,
  0.18 h between two other vehicles and 0.22 h between a train and another vehicle. So a slightly longer journey with
  fewer changes and less walking can win. As in SwissRailRaptor (the MATSim router), each stop keeps only its cheapest
  way of being reached: a cheaper way that arrives later replaces a dearer one that arrives earlier. About 10 ms per
  search.
- **Earliest arrival** (`--routing earliest`, `optimize=time`). With every weight set to 1 and no penalty for changes,
  the cost is the arrival time, and both give the same journeys (checked on 400 searches).

Walking and changing:

- You can board a vehicle at a stop you walked to, or at a stop where you arrived by another vehicle, if you have the
  change time of that stop: its `transfers.txt` rule, else 60 s.
- You can walk between stops up to 400 m apart. The walk takes the `transfers.txt` time when there is a rule (rules
  between stops further apart are used as well), else the straight-line distance × 1.3 at 1.2 m/s.
- At the start and the end of the journey, the router considers the stops within 800 m of the coordinates, at the same
  walking speed. Walking all the way is chosen when it is cheaper (or arrives first, for the earliest arrival).

Stops on the way (`via`): the router finds the journey to the first stop, then from there at the arrival time, and so
on. If the same vehicle goes on through a stop on the way, it stays one ride. A place given by coordinates is walked to
and from. A stay (in minutes) at a stop on the way is a "stay" leg: the journey continues in another vehicle, the latest
one that arrives just as early (waiting longer means staying longer), and each part is priced as a separate ticket.

A trip from the day before that runs after midnight (times after 24:00) is not included in the network of a date.

### Prices

The fares are not written in the code but described in data files (`data/pricing`), so they can be changed without
programming. They are a simplified version of the official tariffs of the Swiss public transport
([Alliance SwissPass: tariffs and rules](https://www.allianceswisspass.ch/de/tarife-vorschriften/uebersicht)):

- `pricingDescription.xml` describes the fares. An `<sbb>` block gives the price per km by distance band, the minimum
  prices and the rounding; each regional network is an `<authority>`, with its price list by number of zones (with and
  without the half-fare card), the zones that count twice, and its age discounts (`<targetedPricing>`); a
  `<distanceBasedDefault>` block prices what belongs to no network (fixed part, price per km, rounding). 
- `gtfs_zones.csv` tells which fare networks and zones each stop belongs to.
- `SBB_all_distances.csv` gives the SBB tariff distances between stations.

There are two ways to change them:

- **For yourself, on the map**: the price editor (see [The map](#the-map)). Nothing changes on the server.
- **For everyone, or for a batch of trips**: export your changes from the editor and point the `fares` setting of
  `config_local.yml` (or `--fares`) to the file. The server and `route_trips.py` then start with these fares. You can
  also edit the files of `data/pricing` directly and restart.

`gtfs_router/pricing.py` reads these files (`Pricing.fares()` and `Pricing.with_changes()` are what the editor uses).
The price of a journey is found in four steps:

1. **Which fare networks each ride belongs to.** These are the networks (Tarifverbünde) shared by all the stops of the
   ride (`gtfs_zones.csv`), plus "SBB" for the stations of `SBB_all_distances.csv`. Only the highest priority is kept:
   regional networks 2, Z-Pass 1, SBB 0. A ride in no network gets the default price. When a stop has the same zone in
   several networks, the zone counts once, for the network with the highest priority.
2. **Which network prices which ride.** If one network covers all the rides, it prices the whole journey (the one with
   the highest priority if there are several). Otherwise, each ride goes to a network it shares with another ride (the
   one with the lowest priority), or else to its own (the one with the highest priority).
3. **The price in that network.**
   - Zone networks: the price list by number of zones (some zones count twice). Unireso (Geneva)
     works differently: the price of the centre zone, plus a price for each other zone, each with a half-fare
     price (the default price if the centre zone is not visited). The fare description has a half-fare price for the
     centre zone (2.00 instead of 3.00) but not for the other zones, which then cost the same with the half-fare card.
   - SBB: the tariff distances of the rides are added up and rounded by steps, and each kilometre is charged at the
     price of its distance band. The price is rounded up (to 0.20 CHF below 69 km, else to 1 CHF), halved with the
     half-fare card, and never below the minimum price.
   - Default price: a fixed part + a price per km of network distance + a price per km², halved with the half-fare
     card and rounded the same way.
4. **Discounts and free travel.**
   - Free: with a GA, under 6, under 16 with the Junior card, with Gleis 7 (under 25, between 19:00 and 05:00), and
     with a local subscription when the origin and destination are within 10 km of home.
   - Half fare: with the half-fare card, or under 16.
   - Discounts (`Discount`): a share off the ticket of a network, for an age range and/or students (`Traveller.student`).
     The largest discount that applies to a network's ticket is taken, after the half fare. The `<targetedPricing>`
     of the fare description are discounts too (Unireso: 100 % off from 0 to 18).
   - 1st class (`first_class`): each price is multiplied by 1.7 before rounding, as in the [SBB tariff](https://www.allianceswisspass.ch/de/tarife-vorschriften/uebersicht). In the zone
     networks too, for now: the zone price × 1.7, rounded up to 20 cents.
   - Zone subscriptions (`Traveller.subscriptions`, `{network: zones or None}`): a stop is covered when one of its
     zones is in a subscription. A ride whose stops are all covered is free. A ride that leaves the subscription is
     priced from its last covered stop, without the fully covered zones, like an extension ticket. A ride that leaves
     the zones and comes back into them is paid in full.
   - With `Pricing(folder, cap = True)` only, the price is capped at 60 CHF (35 CHF with the half-fare card).

`old_price_chf` is the price of the older, distance-based model.

### Crowding

The simulation tells how many simulated people board and leave each vehicle at each stop
(`pt_passenger_counts.csv.gz`). Following the stops of the vehicle's route (`output_transitSchedule.xml.gz`), this
gives its **load** between each pair of stops. Dividing by the sample rate (`--crowding-sample`) scales it to the real
population. Comparing it with the capacity of the vehicle type, seats plus standing room
(`output_transitVehicles.xml.gz`; for example bus 70, tram 180, train 400 in the Geneva simulation), gives its
**occupancy** (`gtfs_router/crowding.py`).

The simulation may use another timetable than the one you search in, so its vehicles are not matched one by one.
Instead, the loads are averaged by line name, pair of stations and hour. When there is no value for that hour, the next
or previous hour is used, or the station alone without the next one.

A ride takes the occupancy of its most crowded segment, as a level:

| Level | Occupancy | Icons |
|---|---|---|
| 1 | below 50 % of the capacity | one person |
| 2 | 50–80 % | two people |
| 3 | above 80 % | three people |

These thresholds are `crowding.LEVELS`. The page shows the level of the ride and of each segment, not numbers of
passengers.

Only the area covered by the simulation has meaningful loads: on lines leaving it, the passengers from outside are
missing.

To use another simulation, change `crowding` and `crowding_sample` in `config_local.yml`, or give them for one run,
for example a simulation of Lausanne at 10 %:

```bash
python api.py --crowding path/to/lausanne_10pct/simulation_output --crowding-sample 0.1
```

### Who the other passengers are

When the simulation folder also has `output_legs.csv.gz`, `output_trips.csv.gz` and `output_persons.csv.gz`, the server
describes the simulated people on board of each ride (`gtfs_router/profiles.py`):

- age, sex and employment;
- trip purpose (the activity at the end of their trip);
- travelcard and car availability;
- income per person and the type of their home municipality.

These are computed separately for the morning peak (06:00–09:00), the evening peak (16:00–19:00) and off-peak, by
boarding time.

Each simulated public transport leg is split into the sections of its route between its boarding and alighting stops,
and the person counts on each of them, by line, pair of stations and period (matched as for the crowding). A ride then
adds up its sections: these are the people on board with you, weighted by how many sections they share with you.

When fewer than 30 simulated people are found (`MIN_PEOPLE`), the server widens the search step by step: the same
sections over the whole day, then the whole line in the period (each leg counted once), then the whole line over the
whole day. If there are still too few, the ride has no profile.

The page shows a bar per category, with a tick for the average of all public transport legs of the period (or of the
day), so you can see how this ride differs from the rest.

Coverage, measured on random rides for 20.10.2026: 68 % of the trains, 97 % of the trams and 46 % of the buses have a
profile from their own sections (in the period or over the day); 19 % of the trains and 7 % of the buses have none;
few cable cars and funiculars have one.

### Tracks and zones

With the files of `data/network` (`--network-data`; pass an empty value to do without), the rides are drawn along
their real path, and their fare zones are computed from it. This is built once per timetable, for all its dates, and
saved in `cache/` with the network.

**Tracks** (`gtfs_router/tracks.py`). For each pair of consecutive stops:

- Trains follow the rail lines of `linie-mit-polygon.geojson` ([SBB open data: Linie mit Polygon](https://data.sbb.ch/explore/dataset/linie-mit-polygon/information/)): the shortest path between the operating points near the
  two stations. Several candidate points are tried, because a station can be on several lines (in Dietikon, the BDWM;
  in Bern, the RBS).
- Buses follow the links of the MATSim route with the same line and stops (`corrected_schedule.xml.gz`,
  `corrected_network.xml.gz`, from the network of [eqasim-switzerland](https://github.com/eqasim-org/eqasim-switzerland/tree/cmdp)).
- Everything else (tram, boat...) and the pairs not found are straight lines.

For 2026-03-18, 97 % of the rail pairs were found on the lines (missing: the Littorail of Neuchâtel, the end of the
line to Les Diablerets and a few stations), and 51,682 bus pairs came from MATSim.

**Zone polygons** (`gtfs_router/zones.py`). `tarifverbundkarte.geojson` ([SBB open data: Tarifverbundkarte](https://data.sbb.ch/explore/dataset/tarifverbundkarte/information/)) gives the municipalities and their fare
networks, and `gtfs_zones.csv` the zones of the stops. From these, each zone gets a polygon:

- A municipality belongs to the zone of most of its stops, not counting the stops on the border of two zones.
- A municipality with several zones (Bern, Lausanne...) is split by nearest stop.
- A municipality whose stops are all on a border (Wichtrach: Libero 626 and 710) is split between these zones, each
  part going to the nearest one.
- A municipality without stops takes the zones of the neighbour it shares the longest border with.

**The zones of a ride.** A ride is in every zone its track crosses for at least 200 m, and in the zone of each of its
stops. A stop on the border of two zones (Stettbach: ZVV 110 and 121) only adds the one the track goes through most.
At the first and last stops, when they are on a border, the nearby track in their zones belongs to them: of those
zones, the ride keeps the ones it needs anyway (because another stop is in them), else the one it goes through most.
For example, Stettbach – Zürich HB passes under Dübendorf (zone 121) in the Zürichberg tunnel but is only in zone
110; Stettbach – Winterthur is in zones 121, 122 and 120.

The distance of a ride is the length of its track. Without `data/network`, rides are straight lines, their zones are
those of their stops, and their distance is the sum of the straight-line distances between stops.

## Project layout

| Path | What it is |
|---|---|
| `api.py` | The web server: the map and the API |
| `route_trips.py` | Computes a CSV of trips |
| `web/index.html` | The map page |
| `gtfs_router/feed.py` | Reads the GTFS timetable |
| `gtfs_router/csa.py` | The router and its generalized cost |
| `gtfs_router/pricing.py` | The fare model |
| `gtfs_router/crowding.py` | The crowding from the simulation |
| `gtfs_router/profiles.py` | The passenger profiles from the simulation |
| `gtfs_router/tracks.py`, `zones.py` | The paths of the rides and the zone polygons |
| `config.yml` | The shared settings; yours go in `config_local.yml` (not in git) |
| `gtfs_router/load.py` | The settings and command-line options of both scripts, and the loading of the network, prices and tracks |
| `data/pricing`, `data/network` | Fare and network inputs |
| `cache/` | What is built on the first start (not in git) |
