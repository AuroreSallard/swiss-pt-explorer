"""Router (Connection Scan Algorithm): scans the connections of the day by departure time once per query, keeping at
each stop the label of least generalized cost (in-vehicle time by mode, waiting and walking time, a penalty per
transfer, as the cost of SwissRailRaptor) and the time it is reached. With the default Cost (every second counts 1, no
penalty) the cost is the arrival time: the journey with the earliest arrival. A trip can be boarded at a stop reached on
foot (from the origin or walking from another stop) or reached by another vehicle at least the change time of the stop
before. Like SwissRailRaptor, one label per stop: a cheaper label reached later replaces a dearer one reached earlier."""
from dataclasses import dataclass, field

import numpy as np

try:
    from numba import njit
except ImportError:  # works without numba, but a query takes seconds instead of milliseconds
    def njit(*args, **kwargs):
        return args[0] if args and callable(args[0]) else (lambda f: f)

INF = np.int32(2 ** 30)
NO_COST = 1e18
ACCESS, RIDE, WALK = 0, 1, 2      # the kinds of records


@dataclass(frozen = True)
class Cost:
    """Weights of the seconds (in-vehicle by mode, waiting, walking) and penalties per transfer (hours), in the unit of the
    rail in-vehicle time. The default: the earliest arrival."""
    in_vehicle: dict = field(default_factory = dict)   # mode of network.routes -> weight; missing: 1
    wait: float = 1.0
    walk: float = 1.0
    transfer_rail_h: float = 0.0        # rail to rail
    transfer_other_h: float = 0.0       # between the other modes
    transfer_intermodal_h: float = 0.0  # rail to another mode or back
    per_transfer_h: float = 0.0

    @classmethod
    def from_eqasim(cls, params):
        """The cost of the utility parameters of eqasim (per hour, relative to the rail in-vehicle time)."""
        rail = -params["travelTimeRail_u_h"]
        other = -params["travelTimeOther_u_h"] / rail
        tram = -params["travelTimeTram_u_h"] / rail
        return cls(in_vehicle = {"rail": 1.0, "bus": -params["travelTimeBus_u_h"] / rail, "tram": tram,
                                 "metro": -params["travelTimeSubway_u_h"] / rail, "boat": other, "cable car": other,
                                 "funicular": other, "other": other},
                   wait = -params["waitTime_u_h"] / rail, walk = -params["walkTime_u_h"] / rail,
                   transfer_rail_h = params["raptorPenalties:transfer_rail"], transfer_other_h = params["raptorPenalties:transfer_other"],
                   transfer_intermodal_h = params["raptorPenalties:transfer_intermodal"], per_transfer_h = -params["perTransfer_u"] / rail)


EARLIEST_ARRIVAL = Cost()
# the utilities of eqasim (switzerland), relative to the rail in-vehicle time
SWISS_COST = Cost.from_eqasim({
    "travelTimeRail_u_h": -1.0, "travelTimeBus_u_h": -1.5836037239192158, "travelTimeTram_u_h": -1.1222785629604182,
    "travelTimeSubway_u_h": -1.1222785629604182, "travelTimeOther_u_h": -1.5836037239192158, "waitTime_u_h": -1.72,
    "walkTime_u_h": -1.97, "perTransfer_u": 0.0, "raptorPenalties:transfer_intermodal": 0.21802854138692665,
    "raptorPenalties:transfer_rail": 0.10055599178047361, "raptorPenalties:transfer_other": 0.1772974263245458})


@njit(cache = True)
def _penalty(previous, rail, p_rail, p_other, p_inter):
    """The penalty of boarding a trip (rail or not) after a vehicle of the mode previous (-1: none, 0 other, 1 rail)."""
    if previous < 0:
        return 0.0
    if previous == 1 and rail:
        return p_rail
    if previous == 0 and not rail:
        return p_other
    return p_inter


@njit(cache = True)
def _scan(c_dep_stop, c_arr_stop, c_dep, c_arr, c_trip, change_s, f_start, f_to, f_dur, trip_w, trip_rail,
          access, egress, first, dep0, wait_w, walk_w, p_rail, p_other, p_inter, min_w, latest, min_change):
    """access, egress: walking seconds from the origin to each stop and from each stop to the destination (-1: too far);
    latest: the latest departure of the first vehicle; min_change: the least time between two vehicles (seconds, on top
    of the change time of the stop or the walk between stops: the longest of them).
    Returns the best cost, its record and egress stop, and the records (kind, a, b, parent, time) to rebuild the journey:
    ACCESS (a: stop), RIDE (a: boarding connection, b: alighting connection), WALK (a: from stop, b: to stop)."""
    n = len(access)
    n_trips = len(trip_w)
    cap = 1 << 16
    r_kind = np.empty(cap, np.int8)
    r_a = np.empty(cap, np.int64)
    r_b = np.empty(cap, np.int64)
    r_parent = np.empty(cap, np.int64)
    r_time = np.empty(cap, np.int64)    # the arrival of the record
    n_rec = 0

    foot_cost = np.full(n, NO_COST)     # the stop reached on foot (from the origin or from another stop)
    foot_time = np.full(n, INF, np.int64)
    foot_mode = np.full(n, -1, np.int8)   # the last vehicle before
    foot_rec = np.full(n, -1, np.int64)
    veh_cost = np.full(n, NO_COST)      # the stop reached by a vehicle
    veh_time = np.full(n, INF, np.int64)
    veh_mode = np.full(n, -1, np.int8)
    veh_rec = np.full(n, -1, np.int64)
    trip_cost = np.full(n_trips, NO_COST)   # the cost on board at the last arrival of the trip
    trip_time = np.zeros(n_trips, np.int64)
    trip_enter = np.full(n_trips, -1, np.int64)
    trip_parent = np.full(n_trips, -1, np.int64)
    best, best_rec, best_stop = NO_COST, -1, -1

    for s in range(n):
        if access[s] >= 0:
            if n_rec == cap:
                cap *= 2
                r_kind, r_a, r_b, r_parent, r_time = _grow(r_kind, r_a, r_b, r_parent, r_time, cap)
            r_kind[n_rec], r_a[n_rec], r_b[n_rec], r_parent[n_rec], r_time[n_rec] = ACCESS, s, -1, -1, dep0 + access[s]
            foot_cost[s], foot_time[s], foot_rec[s] = walk_w * access[s], dep0 + access[s], n_rec
            if egress[s] >= 0 and walk_w * (access[s] + egress[s]) < best:   # on foot through the stop
                best, best_rec, best_stop = walk_w * (access[s] + egress[s]), n_rec, s
            n_rec += 1

    for i in range(first, len(c_dep)):
        if min_w * (c_dep[i] - dep0) >= best:
            break
        t = c_trip[i]
        s = c_dep_stop[i]
        w = trip_w[t]
        on_board = NO_COST
        if trip_cost[t] < NO_COST:   # staying in the trip
            on_board = trip_cost[t] + w * (c_dep[i] - trip_time[t])
        if foot_time[s] <= c_dep[i] and (foot_mode[s] >= 0 or c_dep[i] <= latest):
            board = foot_cost[s] + wait_w * (c_dep[i] - foot_time[s]) + _penalty(foot_mode[s], trip_rail[t], p_rail, p_other, p_inter)
            if board < on_board:
                on_board, trip_enter[t], trip_parent[t] = board, i, foot_rec[s]
        if veh_time[s] < INF and veh_time[s] + max(change_s[s], min_change) <= c_dep[i]:
            board = veh_cost[s] + wait_w * (c_dep[i] - veh_time[s]) + _penalty(veh_mode[s], trip_rail[t], p_rail, p_other, p_inter)
            if board < on_board:
                on_board, trip_enter[t], trip_parent[t] = board, i, veh_rec[s]
        if on_board >= NO_COST:
            continue
        cost = on_board + w * (c_arr[i] - c_dep[i])
        trip_cost[t], trip_time[t] = cost, c_arr[i]
        a = c_arr_stop[i]
        if cost >= veh_cost[a]:
            continue
        if n_rec + 1 + f_start[a + 1] - f_start[a] >= cap:
            while n_rec + 1 + f_start[a + 1] - f_start[a] >= cap:
                cap *= 2
            r_kind, r_a, r_b, r_parent, r_time = _grow(r_kind, r_a, r_b, r_parent, r_time, cap)
        ride = n_rec
        r_kind[ride], r_a[ride], r_b[ride], r_parent[ride], r_time[ride] = RIDE, trip_enter[t], i, trip_parent[t], c_arr[i]
        n_rec += 1
        mode = 1 if trip_rail[t] else 0
        veh_cost[a], veh_time[a], veh_mode[a], veh_rec[a] = cost, c_arr[i], mode, ride
        if egress[a] >= 0 and cost + walk_w * egress[a] < best:
            best, best_rec, best_stop = cost + walk_w * egress[a], ride, a
        for k in range(f_start[a], f_start[a + 1]):
            b = f_to[k]
            walked = cost + walk_w * f_dur[k]
            extra = max(min_change - f_dur[k], 0)   # waiting at b until the least change time is over
            if walked + wait_w * extra < foot_cost[b]:
                r_kind[n_rec], r_a[n_rec], r_b[n_rec], r_parent[n_rec], r_time[n_rec] = WALK, a, b, ride, c_arr[i] + f_dur[k]
                foot_cost[b], foot_time[b], foot_mode[b], foot_rec[b] = walked + wait_w * extra, c_arr[i] + f_dur[k] + extra, mode, n_rec
                n_rec += 1
                if egress[b] >= 0 and walked + walk_w * egress[b] < best:
                    best, best_rec, best_stop = walked + walk_w * egress[b], n_rec - 1, b
    return best, best_rec, best_stop, r_kind[:n_rec], r_a[:n_rec], r_b[:n_rec], r_parent[:n_rec], r_time[:n_rec]


@njit(cache = True)
def _grow(r_kind, r_a, r_b, r_parent, r_time, cap):
    k, a, b, p, t = (np.empty(cap, np.int8), np.empty(cap, np.int64), np.empty(cap, np.int64), np.empty(cap, np.int64),
                     np.empty(cap, np.int64))
    n = len(r_kind)
    k[:n], a[:n], b[:n], p[:n], t[:n] = r_kind, r_a, r_b, r_parent, r_time
    return k, a, b, p, t


@dataclass
class Leg:
    kind: str                     # "walk", "ride" or "stay" (at a stop on the way)
    dep: int                      # seconds since midnight
    arr: int
    from_stop: int = -1           # stop indices (-1: the origin or the destination)
    to_stop: int = -1
    trip: int = -1
    stops: list = field(default_factory = list)   # the stops of a ride, from boarding to alighting
    pos: int = -1                 # the position of the boarding stop in the trip


@dataclass
class Journey:
    dep: int
    arr: int
    legs: list
    cost: float = 0.0             # the generalized cost (seconds of rail in-vehicle time)

    @property
    def rides(self):
        return [leg for leg in self.legs if leg.kind == "ride"]


class Router:
    def __init__(self, network, walk_speed_m_s = 1.2, detour = 1.3, access_radius_m = 800, cost = EARLIEST_ARRIVAL):
        self.net = network
        self.walk_speed, self.detour, self.access_radius = walk_speed_m_s, detour, access_radius_m
        self.cost = cost
        self._modes = network.routes["mode"].to_numpy()[network.trip_route]
        self._trip_rail = self._modes == "rail"
        self._weights = {}   # id of a Cost -> the in-vehicle weight of each trip

    def _walk_s(self, meters):
        return np.ceil(meters * self.detour / self.walk_speed).astype(np.int32)

    def route(self, origin, destination, departure, cost = None, via = (), stays = (), latest = None, min_change_s = 0):
        """origin, destination: (lat, lon), a stop id or a list of stop indices (e.g. the platforms of a station);
        departure: seconds since midnight or "HH:MM[:SS]"; cost: a Cost, else the one of the router; via: places to go
        through, in order (same forms as origin). Returns the Journey of least cost (by default the earliest arrival), or
        None. With via, the journey to each place, then from it at the arrival time: a vehicle going on through the place
        is the same ride (the parts of a trip are merged). stays: seconds to stay at each place of via (a "stay" leg; the
        next part leaves after it, in another vehicle). latest: the latest departure of the first vehicle (seconds since
        midnight or "HH:MM[:SS]"); min_change_s: the least time to change vehicles (on top of the change time of each stop,
        and of the walks between stops: the longest of them)."""
        if via:
            stays = list(stays) + [0] * (len(via) - len(stays))
            journeys, time = [], _seconds(departure)
            for k, (a, b) in enumerate(zip([origin, *via], [*via, destination])):
                part = self.route(a, b, time, cost, latest = latest if k == 0 else None, min_change_s = min_change_s)
                if part is None:
                    return None
                if k > 0 and stays[k - 1] > 0:
                    # after a stay, waiting more is staying longer: the latest departure that arrives as early
                    for _ in range(10):
                        first = next((leg.dep for leg in part.legs if leg.kind == "ride"), None)
                        later = self.route(a, b, first + 60, cost) if first is not None else None
                        if later is None or later.arr > part.arr:
                            break
                        part = Journey(time, later.arr, later.legs, later.cost)
                journeys.append(part)
                time = part.arr
                if k < len(via) and stays[k] > 0:
                    where = part.legs[-1].to_stop if part.legs else -1   # the stop reached (-1: a place by coordinates)
                    journeys.append(Journey(time, time + int(stays[k]), [Leg("stay", time, time + int(stays[k]), where, where)], 0.0))
                    time += int(stays[k])
            legs = []
            for leg in (leg for part in journeys for leg in part.legs):
                last = legs[-1] if legs else None
                if last is not None and last.kind == "ride" and leg.kind == "ride" and last.trip == leg.trip and last.to_stop == leg.from_stop:
                    legs[-1] = Leg("ride", last.dep, leg.arr, last.from_stop, leg.to_stop, last.trip, last.stops + leg.stops[1:], last.pos)
                elif not (leg.kind == "walk" and leg.arr == leg.dep and legs):
                    legs.append(leg)
            return Journey(journeys[0].dep, journeys[-1].arr, legs, sum(j.cost for j in journeys))
        net, cost = self.net, cost or self.cost
        if id(cost) not in self._weights:
            weights = net.routes["mode"].map(cost.in_vehicle).fillna(1.0).to_numpy(np.float64)
            self._weights[id(cost)] = (cost, weights[net.trip_route])
        trip_w = self._weights[id(cost)][1]
        dep = _seconds(departure)
        n = len(net.stop_ids)
        access = np.full(n, -1, np.int64)
        access_from = np.full(n, -1, np.int64)   # the stop of the origin a stop is walked to from (-1: the coordinates)
        egress = np.full(n, -1, np.int64)
        direct = None

        if _is_stops(origin):
            start = net.stop_index(origin) if isinstance(origin, str) else np.asarray(origin, np.int64)
            for s in start:   # and the stops within walking distance of it (the other platforms of the station...)
                links = slice(net.f_start[s], net.f_start[s + 1])
                to, dur = net.f_to[links], net.f_dur[links]
                better = (access[to] < 0) | (dur < access[to])
                access[to[better]], access_from[to[better]] = dur[better], s
            access[start], access_from[start] = 0, start
        else:
            near, dist = net.stops_near(*origin, self.access_radius)
            access[near] = self._walk_s(dist)
        if _is_stops(destination):
            egress[net.stop_index(destination) if isinstance(destination, str) else np.asarray(destination, np.int64)] = 0
        else:
            near, dist = net.stops_near(*destination, self.access_radius)
            egress[near] = self._walk_s(dist)
        if not _is_stops(origin) and not _is_stops(destination):
            k = 111_320
            meters = np.hypot((origin[0] - destination[0]) * k, (origin[1] - destination[1]) * k * np.cos(np.radians(origin[0])))
            direct = int(self._walk_s(meters))

        h = 3600.0
        min_w = min([cost.wait, cost.walk, 1.0] + list(cost.in_vehicle.values()))
        first = int(np.searchsorted(net.c_dep, dep))
        best, best_rec, best_stop, kind, ra, rb, parent, rtime = _scan(
            net.c_dep_stop, net.c_arr_stop, net.c_dep, net.c_arr, net.c_trip, net.change_s, net.f_start, net.f_to, net.f_dur,
            trip_w, self._trip_rail, access, egress, first, dep, cost.wait, cost.walk,
            (cost.transfer_rail_h + cost.per_transfer_h) * h, (cost.transfer_other_h + cost.per_transfer_h) * h,
            (cost.transfer_intermodal_h + cost.per_transfer_h) * h, min_w, INF if latest is None else _seconds(latest), int(min_change_s))

        if direct is not None and cost.walk * direct <= best:
            return Journey(dep, dep + direct, [Leg("walk", dep, dep + direct)], cost.walk * direct)
        if best_rec < 0:
            return None

        arrival = int(rtime[best_rec]) + int(egress[best_stop])
        legs = [Leg("walk", int(rtime[best_rec]), arrival, from_stop = best_stop)] if egress[best_stop] > 0 else []
        r = best_rec
        while r >= 0:
            if kind[r] == ACCESS:
                if rtime[r] > dep:
                    legs.append(Leg("walk", dep, int(rtime[r]), from_stop = int(access_from[ra[r]]), to_stop = int(ra[r])))
            elif kind[r] == WALK:
                legs.append(Leg("walk", int(rtime[parent[r]]), int(rtime[r]), from_stop = int(ra[r]), to_stop = int(rb[r])))
            else:
                enter, exit_ = ra[r], rb[r]
                trip = net.c_trip[exit_]
                begin = net.t_start[trip]
                stops = net.t_stop[begin + net.c_pos[enter]: begin + net.c_pos[exit_] + 2].tolist()
                legs.append(Leg("ride", int(net.c_dep[enter]), int(net.c_arr[exit_]), int(net.c_dep_stop[enter]), int(net.c_arr_stop[exit_]),
                                int(trip), stops, int(net.c_pos[enter])))
            r = parent[r]
        legs.reverse()
        return Journey(dep, arrival, legs, float(best))

    def describe(self, journey):
        """The journey as a list of dicts (one per leg), readable and easy to write to a CSV."""
        net, rows = self.net, []
        last = len(journey.legs) - 1
        for i, leg in enumerate(journey.legs):
            row = {"kind": leg.kind, "dep": hhmm(leg.dep), "arr": hhmm(leg.arr),
                   "from": net.stop_names[leg.from_stop] if leg.from_stop >= 0 else ("origin" if i == 0 else "via"),
                   "to": net.stop_names[leg.to_stop] if leg.to_stop >= 0 else ("destination" if i == last else "via")}
            if leg.kind == "ride":
                route = net.routes.iloc[net.trip_route[leg.trip]]
                row.update(line = route["name"], mode = route["mode"], agency = route["agency"], trip_id = net.trip_ids[leg.trip],
                           from_stop_id = net.stop_ids[leg.from_stop], to_stop_id = net.stop_ids[leg.to_stop], n_stops = len(leg.stops) - 1)
            rows.append(row)
        return rows


def _is_stops(place):
    """A stop id or a list of stop indices, rather than (lat, lon)."""
    return isinstance(place, (str, list, np.ndarray))


def _seconds(value):
    if isinstance(value, (int, np.integer)):
        return int(value)
    parts = [int(p) for p in str(value).split(":")]
    return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)


def hhmm(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"
