"""Price of a journey, ported from eqasim-java (switzerland, ch_cmdp):
  - utils/pricing/RunComputeTransitPrices.processRequest   -> Pricing.price (free travel, half fare, cap)
  - mode_choice/utilities/predictors/SwissPtRoutePredictor -> Pricing.ride_zones (the networks and zones of a ride)
  - mode_choice/utilities/variables/SwissPtVariables       -> Pricing.group_rides (which network prices which rides)
  - mode_choice/costs/pt/ZoneBasedPricing, SBBPtStageCostCalculator, DefaultDistanceBasedCalculator -> the calculators
  - utils/pricing/inputs/*Reader                            -> Pricing.__init__
with the inputs of data/pricing: pricingDescription.xml, gtfs_zones.csv and SBB_all_distances.csv.

The router gives the rides with their stops; the network distance of a ride (MATSim route distance in Java) is here the
sum of the beeline distances between its consecutive stops.
"""
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

PT_REGIONAL_RADIUS_KM = 10.0
FIRST_CLASS_FACTOR = 1.7    # 1st class: the 2nd class price times this, rounded as usual
MIN_ZONE_M = 200            # with the tracks: a zone crossed on less than this does not count (the stops at the border of two zones)


def _java_round(value):
    return math.floor(value * 100.0 + 0.5) / 100.0


def _round_up(price, step):
    """Up to the next multiple of step (0.2, 1), without going a step too far on a float like 34.00000000000001."""
    return math.ceil(round(price / step, 9)) * step


def _fare_rounding(price, km):
    """Up to 20 Rp below 69 km, else to the next franc."""
    return _round_up(price, 0.2) if km < 69 else _round_up(price, 1.0)


def _simplified(stop_id):
    """As getZones: the part of the id before the first ':' or '.' (8501120:0:1 -> 8501120)."""
    return re.split(r"[:.]", stop_id)[0] if ":" in stop_id else stop_id


@dataclass(frozen = True)
class Authority:
    id: str
    priority: int     # SBB and None: 0, ZPass*: 1, the other fare networks: 2

    def __eq__(self, other):
        return isinstance(other, Authority) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


NONE = Authority("None", 0)
SBB = Authority("SBB", 0)
LABELS = {"None": "Distance-based"}   # the names of the authorities in the price breakdown


@dataclass
class Traveller:
    age: int = 30
    halbtax: bool = False
    ga: bool = False
    verbund_abo: bool = False
    strecken_abo: bool = False
    gleis7_abo: bool = False
    junior_abo: bool = False
    first_class: bool = False
    home: tuple = None          # (lat, lon), for the regional subscriptions
    subscriptions: dict = field(default_factory = dict)   # {network: set of zones, or None for all of them}


def parse_subscriptions(text):
    """ "ZVV:110,121;Mobilis:all" -> {"ZVV": {"110", "121"}, "Mobilis": None}."""
    result = {}
    for item in filter(None, (part.strip() for part in (text or "").split(";"))):
        network, _, zones = item.partition(":")
        zones = {z.strip() for z in zones.split(",") if z.strip()}
        result[network.strip()] = None if not zones or zones == {"all"} else zones
    return result


@dataclass
class RideZones:
    """A ride for the pricing (SwissPtLegVariables): its zones by authority, network and SBB distances."""
    zones: dict                 # {authority id: [zone ids]}
    network_km: float
    sbb_km: float
    from_name: str = ""
    to_name: str = ""


@dataclass
class PriceResult:
    price: float
    old_price: float
    network_km: float
    parts: list = field(default_factory = list)   # (authority, price) of each group of rides

    @property
    def rule(self):
        return " + ".join(f"{LABELS.get(a, a)} {p:.2f}" for a, p in self.parts) or "free"


class Pricing:
    def __init__(self, folder, cap = False, tracks = None):
        """cap: prices capped at 60 CHF (35 with half fare), as in eqasim-java. tracks (gtfs_router.tracks): the zones
        of a ride are the zones its track crosses, and its distance the length of the track; else the zones of its stops
        and the beeline distances between them."""
        self.folder, self.cap, self.tracks = folder, cap, tracks
        self._read_description(os.path.join(folder, "pricingDescription.xml"))
        self._read_zones(os.path.join(folder, "gtfs_zones.csv"))
        distances = pd.read_csv(os.path.join(folder, "SBB_all_distances.csv"), dtype = {"origin_id": str, "destination_id": str})
        self.sbb_distances = {}
        for a, b, d in zip(distances["origin_id"].str.strip(), distances["destination_id"].str.strip(), distances["distance"].astype(float)):
            self.sbb_distances[(a, b)] = d
            self.sbb_distances[(b, a)] = d
        # the SBB "zone" holds every stop of the distance table
        for stop in set(distances["origin_id"].str.strip()) | set(distances["destination_id"].str.strip()):
            self.zones_by_stop.setdefault(stop, set()).add((SBB, "SBB"))

    # ------------------------------------------------------------------ inputs

    def _read_description(self, path):
        root = ET.parse(path).getroot()
        d = root.find("distanceBasedDefault")
        self.default = (float(d.find("flatRatePrice").get("value")), float(d.find("pricePerKM").get("value")), float(d.find("power2Factor").get("value")))
        s = root.find("sbb")
        self.sbb_min = float(s.find("minimumPrice").get("value"))
        self.sbb_min_halbtax = float(s.find("minimumPriceHalbtax").get("value"))
        self.sbb_bins = [(float(b.get("distance_min")), float(b.get("distance_max")), float(b.get("priceByKM"))) for b in s.findall("priceByKMByDistanceBin")]

        self.calculators = {}
        for a in root.findall("authority"):
            name = a.find("name").get("value")
            c = {"full": None, "halbtax": None, "double": set(), "center": None, "center_price": float("nan"),
                 "additional_price": float("nan"), "age_discount": {}}
            center_based = a.find("zonalBased").get("value") == "centerbased"
            if not center_based:
                for p in a.findall("priceListByZone"):
                    prices = [float(v) for v in p.get("prices").split(", ")]
                    c["halbtax" if p.get("halbtax") == "true" else "full"] = prices
                two = a.find("zonesCountingAsTwo")
                if two is not None:
                    c["double"] = {z.strip() for z in two.get("zones").split(", ")}
            else:
                c["center"] = a.find("centerZoneID").get("zone")
                c["center_price"] = float(a.find("priceCenterZone").get("price"))
                c["additional_price"] = float(a.find("priceAdditionalZone").get("price"))
            for t in a.findall("targetedPricing"):
                low, high = (int(x) for x in t.get("age_range").split("_"))
                for age in range(low, high + 1):
                    c["age_discount"][age] = float(t.get("discount"))
            self.calculators[name] = c

    def _read_zones(self, path):
        """ZonalReader: the authorities of the 'tarif network' column (ZPass*: priority 1, else 2), the zones of each stop."""
        df = pd.read_csv(path, dtype = str, keep_default_na = False)
        self.authorities = {}
        for text in df["tarif network"]:
            for auth in re.sub(r"[\[\]' ]", "", text).split(","):
                if auth and auth != "nan":
                    self.authorities.setdefault(auth, Authority(auth, 1 if "ZPass" in auth else 2))
        self.zones_by_stop = {}
        for stop_id, text in zip(df["stop_id"].str.strip(), df["zones"]):
            for entry in text.split(","):
                parts = entry.strip().split(":")
                if len(parts) == 2 and parts[0].strip() in self.authorities:
                    self.zones_by_stop.setdefault(stop_id, set()).add((self.authorities[parts[0].strip()], parts[1].strip()))
        self.network_zones = {}
        for pairs in self.zones_by_stop.values():
            for auth, zone in pairs:
                self.network_zones.setdefault(auth.id, set()).add(zone)

    # ------------------------------------------------------------------ rides

    def _stop_zones(self, stop_id):
        """getZones: the zones of the stop id and of its simplified id."""
        return self.zones_by_stop.get(stop_id, set()) | self.zones_by_stop.get(_simplified(stop_id), set())

    def ride_zones(self, stop_ids):
        """cleanZoneInfo: {authority id: [zones]} of the authorities common to every stop of the ride, the highest priority."""
        by_stop = {}
        for stop in dict.fromkeys(stop_ids):
            unique = {}
            for auth, zone in self._stop_zones(stop):   # deduplicated by zone id, the highest priority kept
                if zone not in unique or auth.priority > unique[zone][0].priority:
                    unique[zone] = (auth, zone)
            groups = {}
            for auth, zone in unique.values():
                groups.setdefault(auth, []).append(zone)
            by_stop[stop] = groups

        common = None
        for groups in by_stop.values():
            common = set(groups) if common is None else common & set(groups)
        if not common:
            return {}
        top = max(a.priority for a in common)
        selected = {a for a in common if a.priority == top}

        result = {}
        for groups in by_stop.values():
            for auth in selected:
                for zone in groups.get(auth, []):
                    if zone not in result.setdefault(auth.id, []):
                        result[auth.id].append(zone)
        return result

    def ride(self, network, ride):
        stop_ids = [str(network.stop_ids[s]) for s in ride.stops]
        zones = self.ride_zones(stop_ids)
        km = _ride_km(network, ride.stops)
        if self.tracks is not None and ride.pos >= 0:
            zones = self.track_zones(zones, stop_ids, self.tracks.crossed(network, ride), lambda a: self.tracks.pieces(network, ride, a))
            km = self.tracks.length_m(network, ride) / 1000
        sbb_km = self.sbb_distances.get((_simplified(stop_ids[0]), _simplified(stop_ids[-1])), -1.0) if "SBB" in zones else -1.0
        return RideZones(zones, km, sbb_km, network.stop_names[ride.stops[0]], network.stop_names[ride.stops[-1]])

    def track_zones(self, zones, stop_ids, crossed, pieces_of):
        """The zones of the authorities of a ride from its track: the zones crossed on at least MIN_ZONE_M and the zone of
        each stop; a stop at the border of zones (in several of them) adds the one its track goes through most.
        The first and last stops at a border: the track next to them in their zones belongs to them (Stettbach, 110 and
        121, then the Zürichberg tunnel under Dübendorf, 121, to Zürich, 110: only 110); of these zones, the ride keeps
        those it needs anyway (the zone of another stop, a zone crossed further), else the one it goes through most."""
        result = {}
        for auth, stop_zones in zones.items():
            along = crossed.get(auth)
            if not along:
                result[auth] = stop_zones
                continue
            keep, needed = {z for z, m in along.items() if m >= MIN_ZONE_M}, set()
            for stop_id in dict.fromkeys(stop_ids):
                own = {z for a, z in self._stop_zones(stop_id) if a.id == auth}
                if len(own) == 1:
                    keep |= own
                    needed |= own
                elif own and not own & keep:
                    keep.add(max(sorted(own), key = lambda z: along.get(z, 0.0)))
            pieces = pieces_of(auth)
            for stop_id, ordered in ((stop_ids[0], pieces), (stop_ids[-1], pieces[::-1])):
                own = {z for a, z in self._stop_zones(stop_id) if a.id == auth}
                if len(own) < 2:
                    continue
                near = {}   # the zones of the stop along the track next to it, and their lengths
                for k, (zone, start, end) in enumerate(ordered):
                    if zone not in own:
                        break
                    near[zone] = near.get(zone, 0.0) + end - start
                further = {zone for zone, _, _ in ordered[k + 1 if zone in own else k:]} if near else set()
                absorbed = {z for z in near if z not in further}
                if not absorbed:
                    continue
                needed_here = absorbed & needed
                keep -= absorbed
                keep |= needed_here or {max(sorted(absorbed), key = lambda z: near[z])}
            result[auth] = sorted(keep, key = lambda z: (len(z), z))
        return result

    # ------------------------------------------------------------------ subscriptions

    def covering(self, stop_id, subscriptions):
        """The subscription (network) valid at the stop, or None."""
        for auth, zone in self._stop_zones(stop_id):
            if auth.id in subscriptions and (subscriptions[auth.id] is None or zone in subscriptions[auth.id]):
                return auth.id
        return None

    def zone_covered(self, network, zone, subscriptions):
        """Whether a zone of a network is in the subscriptions; the Z-Pass and the networks they join share their zone
        numbers."""
        for other, zones in subscriptions.items():
            if other == network or "ZPass" in other or "ZPass" in network:
                zones = self.network_zones.get(other, set()) if zones is None else zones
                if zone in zones and (other == network or zone in self.network_zones.get(network, set())):
                    return True
        return False

    def uncovered_ride(self, network, ride, subscriptions):
        """The part of a ride to pay with subscriptions: None if they cover all its stops, else the ride from the last
        covered stop before the uncovered ones to the first covered one after them, without the zones of the subscriptions
        (the extension ticket). A ride leaving and entering the subscription again is paid in full, but for its zones."""
        stop_ids = [str(network.stop_ids[s]) for s in ride.stops]
        covered = [self.covering(s, subscriptions) is not None for s in stop_ids]
        if all(covered):
            return None
        first = covered.index(False)
        last = len(covered) - 1 - covered[::-1].index(False)
        if not any(covered[first:last + 1]):
            begin, end = max(first - 1, 0), min(last + 1, len(covered) - 1)
        else:
            begin, end = 0, len(covered) - 1
        part = self.ride(network, replace(ride, stops = ride.stops[begin:end + 1], pos = ride.pos + begin if ride.pos >= 0 else -1))
        zones = {a: [z for z in zs if not self.zone_covered(a, z, subscriptions)] for a, zs in part.zones.items()}
        if part.zones and not any(zones.values()):
            return None
        part.zones = {a: zs for a, zs in zones.items() if zs}
        return part

    def _authorities_of(self, ride):
        if not ride.zones:
            return {NONE}
        return {self.authorities.get(a, SBB if a == "SBB" else Authority(a, 2)) for a in ride.zones}

    def group_rides(self, rides):
        """getPricingStrategy: {authority id: [rides]}."""
        sets = [self._authorities_of(r) for r in rides]
        frequency = {}
        for s in sets:
            for a in s:
                frequency[a] = frequency.get(a, 0) + 1
        everywhere = [a for a, n in frequency.items() if n == len(rides)]
        if everywhere:
            return {max(everywhere, key = lambda a: a.priority).id: list(rides)}
        groups = {}
        for ride, s in zip(rides, sets):
            shared = [a for a in s if frequency.get(a, 0) > 1]
            chosen = min(shared, key = lambda a: a.priority) if shared else max(s, key = lambda a: a.priority)
            groups.setdefault(chosen.id, []).append(ride)
        return groups

    # ------------------------------------------------------------------ calculators

    def default_price(self, rides, halbtax, factor = 1.0):
        km = sum(r.network_km for r in rides)
        flat, per_km, power2 = self.default
        price = (flat + per_km * km + power2 * km * km) * factor
        if halbtax:
            price /= 2
        return _java_round(_fare_rounding(price, km))

    @staticmethod
    def _sbb_rounding(d):
        for limit, step in ((8, 4), (30, 2), (60, 3), (100, 4), (150, 5), (300, 10)):
            if d <= limit:
                return math.ceil(d / step) * step
        return math.ceil(d / 20.0) * 20

    def sbb_price(self, rides, halbtax, factor = 1.0):
        distance = self._sbb_rounding(sum(r.sbb_km for r in rides))
        price = 0.0
        for low, high, rate in self.sbb_bins:
            if distance >= int(low):
                price += (min(distance, int(high)) - int(low) + 1) * rate
            else:
                break
        price = _fare_rounding(price * factor, distance)
        minimum = _round_up((self.sbb_min_halbtax if halbtax else self.sbb_min) * factor, 0.2)
        return _java_round(max(price / 2.0 if halbtax else price, minimum))

    def zone_price(self, authority, rides, halbtax, age, factor = 1.0):
        """factor: 1st class, the 2nd class price times it rounded up to 20 Rp (the same rule as the SBB until the
        zone networks are checked)."""
        c = self.calculators[authority]
        zones = {z for r in rides for z in r.zones.get(authority, [])}
        if not zones:
            raise ValueError("At least one zone must be visited")
        if c["full"] is not None and c["halbtax"] is not None:
            prices = c["halbtax"] if halbtax else c["full"]
            n = len(zones) + sum(1 for z in zones if z in c["double"])
            price = _java_round(prices[min(n, len(prices)) - 1])
            if factor != 1.0:
                price = _round_up(price * factor, 0.2)
            if n > len(prices):
                return _java_round(price)
        elif c["center"] in zones:
            price = c["center_price"] + c["additional_price"] * (len(zones) - 1)
            if factor != 1.0:
                price = _round_up(price * factor, 0.2)
        else:
            price = self.default_price(rides, halbtax, factor)
        discount = c["age_discount"].get(age)
        return _java_round(price * (1.0 - discount / 100.0 if discount is not None else 1.0))

    def _calculate(self, authority, rides, halbtax, age, factor = 1.0):
        if authority == "SBB":
            return self.sbb_price(rides, halbtax, factor)
        if authority in self.calculators:
            return self.zone_price(authority, rides, halbtax, age, factor)
        return self.default_price(rides, halbtax, factor)   # "None" and the authorities without a price description

    # ------------------------------------------------------------------ journey

    def price(self, journey, network, traveller = None, origin = None, destination = None):
        """PriceResult of a Journey of the router (processRequest). origin and destination (lat, lon) are needed for the
        regional subscriptions only."""
        t = traveller or Traveller()
        if any(leg.kind == "stay" for leg in journey.legs):   # a stay at a stop on the way ends a ticket: each part priced
            parts, current = [], []
            for leg in journey.legs + [None]:
                if leg is None or leg.kind == "stay":
                    if any(l.kind == "ride" for l in current):
                        parts.append(self.price(type(journey)(current[0].dep, current[-1].arr, current), network, t, origin, destination))
                    current = []
                else:
                    current.append(leg)
            return PriceResult(_java_round(sum(p.price for p in parts)), _java_round(sum(p.old_price for p in parts)),
                               sum(p.network_km for p in parts), [x for p in parts for x in p.parts])
        if not journey.rides:
            return PriceResult(0.0, 0.0, 0.0)
        wholes = [self.ride(network, r) for r in journey.rides]
        km = sum(w.network_km for w in wholes)

        dep = journey.dep
        gleis7 = t.age < 25 and t.gleis7_abo and (dep < 5 * 3600 or dep >= 19 * 3600)
        if t.ga or t.age < 6 or (t.age < 16 and t.junior_abo) or gleis7:
            return PriceResult(0.0, 0.0, km)
        if (t.verbund_abo or t.strecken_abo) and t.home is not None and origin is not None and destination is not None:
            home_km = max(_km(origin, t.home), _km(destination, t.home))
            if home_km <= PT_REGIONAL_RADIUS_KM:
                return PriceResult(0.0, 0.0, km)

        rides, used = [], set()
        for r, whole in zip(journey.rides, wholes):
            part = self.uncovered_ride(network, r, t.subscriptions) if t.subscriptions else whole
            if part is not None:
                rides.append(part)
            if part is None or part.zones != whole.zones or part.network_km != whole.network_km:   # a subscription helped
                used |= {n for s in r.stops if (n := self.covering(str(network.stop_ids[s]), t.subscriptions))}
        parts = [(f"{n} subscription", 0.0) for n in sorted(used)]
        if not rides:
            return PriceResult(0.0, 0.0, km, parts)
        paid_km = sum(r.network_km for r in rides)

        half = t.halbtax or t.age < 16
        factor = FIRST_CLASS_FACTOR if t.first_class else 1.0
        parts += [(authority, self._calculate(authority, group, half, t.age, factor)) for authority, group in self.group_rides(rides).items()]
        price = _java_round(sum(p for _, p in parts))

        old = max(2.8, 2 * (0.21 * paid_km - 0.00015 * paid_km ** 2)) * factor
        if half:
            old /= 2.0
        old = _java_round(old)
        if self.cap:
            cap = 35.0 if half else 60.0
            price, old = min(price, cap), min(old, cap)
        return PriceResult(price, old, km, parts)


def _ride_km(network, stops):
    """The sum of the beeline distances between the stops."""
    lat, lon = network.stop_lat[stops], network.stop_lon[stops]
    return float(np.sum(np.hypot(np.diff(lat) * 111.32, np.diff(lon) * 111.32 * np.cos(np.radians(lat[:-1])))))


def _km(a, b):
    return float(np.hypot((a[0] - b[0]) * 111.32, (a[1] - b[1]) * 111.32 * np.cos(np.radians(a[0]))))
