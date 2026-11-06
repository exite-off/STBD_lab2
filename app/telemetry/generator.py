import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

STEP_SECONDS = 30
DEPOT = (50.4501, 30.5234)  # Kyiv

# cruise speed and speed limit by vehicle type, km/h
CRUISE = {"Car": 85, "Van": 75, "Truck": 70, "Tractor unit": 78, "Bus": 65}
LIMIT = {"Car": 110, "Van": 90, "Truck": 90, "Tractor unit": 90, "Bus": 90}


@dataclass(slots=True)
class Point:
    seq: int
    t: datetime
    lat: float
    lon: float
    speed: float
    heading: int
    sensors: dict[str, float]


@dataclass(slots=True)
class Event:
    type: str
    t: datetime
    lat: float
    lon: float
    severity: int
    value: float | None


@dataclass(slots=True)
class Track:
    id: int
    vehicle: dict
    driver: dict
    started_at: datetime
    ended_at: datetime | None = None
    distance_km: float = 0.0
    max_speed: float = 0.0
    avg_speed: float = 0.0
    points: list[Point] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Assignment:
    vehicle_id: int
    plate: str
    vehicle_type: str
    fuel_type: str
    driver_id: int
    driver_name: str


class SensorModel:
    """Sensor set depends on vehicle type and fuel, like real trackers."""

    def __init__(self, a: Assignment, rnd: random.Random) -> None:
        self.rnd = rnd
        self.type = a.vehicle_type
        self.electric = a.fuel_type == "Electric"
        self.level = rnd.uniform(70, 98)
        self.cargo = rnd.uniform(2.0, 4.5)
        # some reefer trips have a failing compressor
        self.cargo_drift = rnd.uniform(0.004, 0.012) if rnd.random() < 0.15 else rnd.uniform(-0.002, 0.002)
        self.axle = rnd.uniform(6, 9) if self.type == "Truck" else rnd.uniform(10, 11.5)
        self.passengers = rnd.randint(5, 30)

    def read(self, speed: float) -> dict[str, float]:
        r = self.rnd
        self.level = max(5.0, self.level - speed * 0.0009 * r.uniform(0.8, 1.2))
        s: dict[str, float] = {}
        if self.electric:
            s["battery_soc"] = round(self.level, 1)
            s["battery_temp"] = round(r.gauss(31, 2), 1)
        else:
            s["fuel_level"] = round(self.level, 1)
            s["engine_temp"] = round(r.gauss(89, 2.5), 1)
        if self.type == "Truck":
            self.cargo += self.cargo_drift * STEP_SECONDS / 10 + r.gauss(0, 0.05)
            s["cargo_temp"] = round(self.cargo, 2)
        if self.type in ("Truck", "Tractor unit"):
            s["axle_load"] = round(self.axle + r.gauss(0, 0.05), 2)
        if self.type == "Bus":
            if speed == 0:
                self.passengers = max(0, min(49, self.passengers + r.randint(-6, 6)))
            s["passengers"] = self.passengers
        if self.type in ("Bus", "Van"):
            s["door_open"] = 1.0 if speed == 0 and r.random() < 0.6 else 0.0
        return s


def move(lat: float, lon: float, heading: float, km: float) -> tuple[float, float]:
    rad = math.radians(heading)
    dlat = km * math.cos(rad) / 111.32
    dlon = km * math.sin(rad) / (111.32 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def build_track(track_id: int, a: Assignment, started_at: datetime, points_count: int, seed: int) -> Track:
    rnd = random.Random(seed * 1_000_003 + track_id)
    sensors = SensorModel(a, rnd)
    track = Track(id=track_id, vehicle={"id": a.vehicle_id, "plate": a.plate, "type": a.vehicle_type,
                                        "fuel": a.fuel_type},
                  driver={"id": a.driver_id, "name": a.driver_name}, started_at=started_at)
    lat = DEPOT[0] + rnd.uniform(-0.15, 0.15)
    lon = DEPOT[1] + rnd.uniform(-0.25, 0.25)
    heading = rnd.uniform(0, 360)
    speed = 0.0
    cruise, limit = CRUISE[a.vehicle_type], LIMIT[a.vehicle_type]
    speeding = cargo_alarm = False
    t = started_at
    total_speed = 0.0

    for seq in range(points_count):
        prev = speed
        roll = rnd.random()
        if roll < 0.04:
            speed = 0.0  # traffic light, stop
        elif roll < 0.05:
            speed = max(0.0, speed - rnd.uniform(30, 45))  # sudden braking
        else:
            target = cruise + rnd.gauss(0, 12)
            speed = max(0.0, min(140.0, speed + (target - speed) * rnd.uniform(0.3, 0.8)))
        speed = round(speed, 1)
        heading = (heading + rnd.gauss(0, 8)) % 360
        km = speed * STEP_SECONDS / 3600
        lat, lon = move(lat, lon, heading, km)
        readings = sensors.read(speed)
        point = Point(seq, t, round(lat, 6), round(lon, 6), speed, int(heading), readings)
        track.points.append(point)
        plat, plon = point.lat, point.lon
        track.distance_km += km
        track.max_speed = max(track.max_speed, speed)
        total_speed += speed

        if prev - speed >= 30:
            track.events.append(Event("harsh_braking", t, plat, plon, 2, round(prev - speed, 1)))
        elif speed - prev >= 35:
            track.events.append(Event("harsh_acceleration", t, plat, plon, 1, round(speed - prev, 1)))
        if speed > limit and not speeding:
            track.events.append(Event("speeding", t, plat, plon, 2 if speed > limit + 20 else 1, speed))
        speeding = speed > limit
        temp = readings.get("cargo_temp")
        if temp is not None and temp > 8 and not cargo_alarm:
            track.events.append(Event("cargo_temp_alarm", t, plat, plon, 3, temp))
            cargo_alarm = True
        if readings.get("door_open") and speed > 0:
            track.events.append(Event("door_open_moving", t, plat, plon, 3, None))
        t += timedelta(seconds=STEP_SECONDS)

    track.ended_at = track.points[-1].t if track.points else None
    track.distance_km = round(track.distance_km, 2)
    track.avg_speed = round(total_speed / max(1, len(track.points)), 1)
    return track


def generate(assignments: list[Assignment], tracks: int, seed: int = 42, days: int = 180,
             first_id: int = 1) -> Iterator[Track]:
    """Deterministic: the same seed gives the same data for every database."""
    rnd = random.Random(seed)
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    by_vehicle: dict[int, list[Assignment]] = {}
    for a in assignments:
        by_vehicle.setdefault(a.vehicle_id, []).append(a)
    vehicles = sorted(by_vehicle)
    for i in range(tracks):
        a = rnd.choice(by_vehicle[vehicles[i % len(vehicles)]])
        day = end - timedelta(days=rnd.randint(1, days))
        started = day + timedelta(hours=rnd.randint(5, 17), minutes=rnd.randint(0, 59))
        points = rnd.randint(240, 720)  # 2..6 hours
        yield build_track(first_id + i, a, started, points, seed)
