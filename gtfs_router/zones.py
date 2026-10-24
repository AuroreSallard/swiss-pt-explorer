"""The polygons of the fare zones, drawn from the municipalities.

No official map of the zones of the Swiss fare networks is used. Instead, the zones are rebuilt from two sources: the
map of the municipalities and the fare networks they belong to (tarifverbundkarte.geojson), and the zone of each stop
(gtfs_zones.csv of the pricing inputs). For each fare network:

- a municipality belongs to the zone of most of its stops (at least MAJORITY of them), not counting the stops on the
  border of two zones (those listed in both);
- a municipality whose stops are in several zones (Bern, Lausanne...) is split between them: each place goes to the
  zone of its nearest stop;
- a municipality whose stops are all on the border of zones (Wichtrach: Libero 626 and 710) is split between these
  zones, each part going to the nearest one;
- a municipality without any stop of the network takes the zone of its neighbour with the longest common border, for
  the networks the map says it is in (and their Z-Pass).

The polygons are in the Swiss projection (LV95, EPSG:2056), in metres. They are used to find the zones a ride goes
through (tracks.py), to draw the zones on the map, and to know whether a traveller lives in a network's area.
"""
import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

logger = logging.getLogger(__name__)

# the names of the networks of the map, as the pricing inputs call them (the others are the same)
NETWORK_NAMES = {"A-Welle": "Awelle", "ONDE VERTE": "OndeVerte", "engadin mobil": "EngadinMobil", "unireso": "Unireso",
                 "Tarifverbund Schwyz": "TVSZ", "Tarifverbund Zug": "ZVB", "OSTWIND": "Ostwind", "Tarifverbund Davos": "Davos",
                 "Tarifverbund Klosters": "Klosters"}
MAJORITY = 0.9   # the share of its stops a zone needs for a whole municipality to be in it (else it is split)


def zone_polygons(pricing, net, municipalities_path):
    """The zone polygons, as a GeoDataFrame with one row per zone of each network: network, zone, geometry,
    municipalities (the names of the municipalities in it, the one with the most stops first) and name (that first one)."""
    towns = gpd.read_file(municipalities_path).to_crs(2056)[["id", "name", "partners", "geometry"]]
    towns["networks"] = towns["partners"].map(lambda p: {NETWORK_NAMES.get(x.strip(), x.strip()) for x in (p or "").split(",") if x.strip()})

    # the stops with one zone in a network, and the stops at the border of zones (several zones in the same network)
    rows, border_rows = [], []
    for s, stop_id in enumerate(net.stop_ids):
        by_network = {}
        for auth, zone in pricing._stop_zones(str(stop_id)):
            if auth.id != "SBB":
                by_network.setdefault(auth.id, set()).add(zone)
        rows += [(s, n, next(iter(z))) for n, z in by_network.items() if len(z) == 1]
        border_rows += [(s, n, z) for n, z in by_network.items() if len(z) > 1]

    def located(rows, columns):
        """The rows (one per stop) with the position of their stop and the municipality it is in (column id)."""
        table = pd.DataFrame(rows, columns = columns)
        points = gpd.GeoSeries(gpd.points_from_xy(net.stop_lon[table["stop"]], net.stop_lat[table["stop"]]), crs = 4326).to_crs(2056)
        table = gpd.GeoDataFrame(table, geometry = points.values, crs = 2056)
        return gpd.sjoin(table, towns[["id", "geometry"]], predicate = "within").drop(columns = "index_right")
    stops = located(rows, ["stop", "network", "zone"])
    border = located(border_rows, ["stop", "network", "zones"])

    # each municipality in the zone most of its stops are in, or split by nearest stop (Voronoi cells of its stops)
    pieces = []   # (town id, network, zone, geometry)
    votes = stops.groupby(["id", "network", "zone"]).size().rename("n").reset_index()
    votes["share"] = votes["n"] / votes.groupby(["id", "network"])["n"].transform("sum")
    best = votes.sort_values("n", ascending = False).drop_duplicates(["id", "network"])
    shapes = towns.set_index("id").geometry
    split = 0
    for town, network, zone, share in zip(best["id"], best["network"], best["zone"], best["share"]):
        if share >= MAJORITY:
            pieces.append((town, network, zone, shapes[town]))
            continue
        split += 1   # the town by the nearest stop of the network
        inside = stops[(stops["id"] == town) & (stops["network"] == network)].drop_duplicates("geometry")
        cells = shapely.voronoi_polygons(shapely.multipoints(inside.geometry.values), extend_to = shapes[town])
        cells = gpd.GeoDataFrame(geometry = list(shapely.get_parts(cells)), crs = 2056)
        cells = gpd.sjoin(cells, inside[["zone", "geometry"]], predicate = "contains")
        for z, cell in cells.dissolve("zone").geometry.items():
            pieces.append((town, network, z, cell.intersection(shapes[town])))

    # the towns with only stops at the border of zones (Wichtrach, Libero 626 and 710): the border goes through them.
    # A grid of points covers the town, each point goes to the nearest of these zones (as drawn so far), and the town is
    # cut along the cells of these points
    have = {(t, n) for t, n, _, _ in pieces}
    between = 0
    for (town, network), group in border.groupby(["id", "network"]):
        if (town, network) in have:
            continue
        candidates = set().union(*group["zones"])
        known = {z: shapely.union_all([g for t, n, zz, g in pieces if n == network and zz == z]) for z in candidates}
        known = {z: g for z, g in known.items() if not g.is_empty}
        if not known:
            continue
        shape = shapes[town]
        step = max(100.0, np.sqrt(shape.area) / 40)
        x0, y0, x1, y1 = shape.bounds
        grid = shapely.points(*np.meshgrid(np.arange(x0, x1 + step, step), np.arange(y0, y1 + step, step)))
        grid = grid.ravel()[shapely.contains(shape, grid.ravel())]
        if len(grid) == 0:
            grid = np.array([shape.representative_point()])
        names = sorted(known)
        nearest = np.array(names)[np.argmin([shapely.distance(known[z], grid) for z in names], axis = 0)]
        cells = shapely.get_parts(shapely.voronoi_polygons(shapely.multipoints(grid), extend_to = shape))
        cells = gpd.GeoDataFrame(geometry = list(cells), crs = 2056)
        cells = gpd.sjoin(cells, gpd.GeoDataFrame({"zone": nearest}, geometry = list(grid), crs = 2056), predicate = "contains")
        for z, cell in cells.dissolve("zone").geometry.items():
            pieces.append((town, network, z, cell.intersection(shape)))
        have.add((town, network))
        between += 1

    # the towns without stops of a network: the zone of their neighbour with the longest common border (the neighbours
    # are tried from the longest border down), for the networks the map gives them
    zone_of = {}   # town -> {network: its first zone}
    for t, n, z, _ in pieces:
        zone_of.setdefault(t, {}).setdefault(n, z)
    neighbours = gpd.sjoin(towns[["id", "geometry"]], towns[["id", "geometry"]], predicate = "touches")
    border = shapely.length(shapely.intersection(shapes[neighbours["id_left"]].values, shapes[neighbours["id_right"]].values))
    neighbours = neighbours.assign(border = border).sort_values("border", ascending = False)
    networks_of = towns.set_index("id")["networks"]
    filled = 0
    for town, group in neighbours.groupby("id_left", sort = False):
        wanted = networks_of[town]
        if not wanted:
            continue
        for other in group["id_right"]:
            for network, zone in zone_of.get(other, {}).items():
                if (town, network) in have or not (network in wanted or network.startswith("ZPass")):
                    continue
                pieces.append((town, network, zone, shapes[town]))
                have.add((town, network))
                filled += 1

    # the pieces merged into one polygon per zone, named after their municipalities (the one with the most stops first)
    result = gpd.GeoDataFrame(pd.DataFrame(pieces, columns = ["id", "network", "zone", "geometry"]), crs = 2056)
    result["town"] = result["id"].map(towns.set_index("id")["name"])
    counts = stops.groupby(["network", "zone", "id"]).size().rename("stops")
    result = result.join(counts, on = ["network", "zone", "id"]).fillna({"stops": 0})
    names = result.sort_values("stops", ascending = False).groupby(["network", "zone"])["town"].agg(lambda t: list(dict.fromkeys(t)))
    result = result.dissolve(["network", "zone"]).reset_index()[["network", "zone", "geometry"]]
    result["geometry"] = result.geometry.buffer(0)   # repairs the small overlaps and gaps of the merged pieces
    result["municipalities"] = [names[(n, z)] for n, z in zip(result["network"], result["zone"])]
    result["name"] = result["municipalities"].map(lambda m: m[0])
    logger.info(f"  {len(result)} zone polygons: {len(best)} municipality-network pairs from their stops ({split} split by stop), "
                f"{between} split between the zones of their border stops, {filled} from a neighbour")
    return result
