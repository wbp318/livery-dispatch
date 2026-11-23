"""Seed script: load example drivers, sample subcontractors, sample rate
cards, and a couple of trips for development.

Run from the repository root:
    python seed.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from database import SessionLocal, init_db
from models import (
    Driver, DriverStatus,
    RateCard,
    Subcontractor, SubcontractorTier,
    Trip, TripStatus,
    VehicleClass,
)
from rate_defaults import planning_rate_cards


# Anchor coordinates for the corridor.
BRUNSWICK = (31.1499, -81.4915)
ST_SIMONS = (31.1522, -81.3914)
DARIEN = (31.3690, -81.4346)
JAX_TERMINAL = (30.4941, -81.6878)
SAV_TERMINAL = (32.1276, -81.2025)
CLOISTER = (31.1969, -81.3438)
BLUFFTON_SC = (32.2371, -80.8607)
SAVANNAH = (32.0809, -81.0912)


def seed():
    init_db()
    with SessionLocal() as s:
        if s.query(Driver).count() == 0:
            d1 = Driver(
                name="Marcus Aurelius (lead)",
                phone="+19125550101",
                vehicle_classes="suv,sprinter",
                status=DriverStatus.AVAILABLE,
                lat=BRUNSWICK[0],
                lon=BRUNSWICK[1],
            )
            d2 = Driver(
                name="Eleanor Vance (W-2)",
                phone="+19125550102",
                vehicle_classes="suv",
                status=DriverStatus.AVAILABLE,
                lat=ST_SIMONS[0],
                lon=ST_SIMONS[1],
            )
            d3 = Driver(
                name="Henry Cole (1099 backup)",
                phone="+19125550103",
                vehicle_classes="suv,sprinter",
                status=DriverStatus.OFF_DUTY,
                lat=DARIEN[0],
                lon=DARIEN[1],
            )
            s.add_all([d1, d2, d3])
            s.commit()
            print(f"Seeded 3 drivers (ids {d1.id}, {d2.id}, {d3.id}).")
        else:
            print("Drivers already seeded; skipping.")

        if s.query(Subcontractor).count() == 0:
            # Fictional overflow partners for development. Names, phone numbers
            # and coordinates are placeholders; replace them with real
            # partners before any live dispatch.
            subs = [
                Subcontractor(
                    name="Sample Northshore Cars",
                    dispatch_phone="+18435550100",
                    dispatch_email="dispatch@northshore.invalid",
                    vehicle_classes="suv,sedan",
                    tier=SubcontractorTier.TIER_1,
                    markup_pct=15.0,
                    base_lat=BLUFFTON_SC[0],
                    base_lon=BLUFFTON_SC[1],
                    service_radius_mi=80.0,
                    notes="Sample partner: north end of the corridor.",
                ),
                Subcontractor(
                    name="Sample Island Transfer",
                    dispatch_phone="+19125550200",
                    dispatch_email="dispatch@island-transfer.invalid",
                    vehicle_classes="suv,sprinter",
                    tier=SubcontractorTier.TIER_1,
                    markup_pct=15.0,
                    base_lat=ST_SIMONS[0],
                    base_lon=ST_SIMONS[1],
                    service_radius_mi=60.0,
                    notes="Sample partner: island base, covers the JAX side.",
                ),
                Subcontractor(
                    name="Sample Gateway Airport Cars",
                    dispatch_phone="+19125550300",
                    dispatch_email="dispatch@gateway-airport.invalid",
                    vehicle_classes="suv,sprinter,sedan",
                    tier=SubcontractorTier.TIER_1,
                    markup_pct=15.0,
                    base_lat=BRUNSWICK[0],
                    base_lon=BRUNSWICK[1],
                    service_radius_mi=100.0,
                    notes="Sample partner: mainland base, widest radius.",
                ),
                Subcontractor(
                    name="Sample Riverfront Executive",
                    dispatch_phone="+19125550400",
                    dispatch_email="dispatch@riverfront-exec.invalid",
                    vehicle_classes="suv,sedan",
                    tier=SubcontractorTier.TIER_1,
                    markup_pct=15.0,
                    base_lat=SAVANNAH[0],
                    base_lon=SAVANNAH[1],
                    service_radius_mi=70.0,
                    notes="Sample partner: covers the SAV side.",
                ),
            ]
            s.add_all(subs)
            s.commit()
            print(f"Seeded {len(subs)} subcontractors.")
        else:
            print("Subcontractors already seeded; skipping.")

        if s.query(RateCard).count() == 0:
            # Sample rates for development; replace with your own before
            # going live. Money in cents (so DB math stays integer-only).
            # Shared with `cli.py rates seed-defaults` (v1.1.18, E6).
            rate_cards = planning_rate_cards()
            s.add_all(rate_cards)
            s.commit()
            print(f"Seeded {len(rate_cards)} rate cards.")
        else:
            print("Rate cards already seeded; skipping.")

        if s.query(Trip).count() == 0:
            # t1 starts BROADCASTING so the /map view shows an in-progress
            # offer (yellow status badge, route drawn between pickup and
            # dropoff) the moment the dispatcher opens the dispatcher map for the
            # first time.
            t1 = Trip(
                passenger_name="Mr. Whitfield (Cloister guest)",
                passenger_phone="+19045556789",
                pickup_label="JAX Terminal",
                pickup_lat=JAX_TERMINAL[0],
                pickup_lon=JAX_TERMINAL[1],
                dropoff_label="The Cloister, Sea Island",
                dropoff_lat=CLOISTER[0],
                dropoff_lon=CLOISTER[1],
                pickup_time=datetime.now(timezone.utc) + timedelta(hours=4),
                vehicle_class=VehicleClass.SUV,
                notes="Inside-terminal meet-and-greet; flight tracking on AA1234.",
                status=TripStatus.BROADCASTING,
            )
            # t2 is ACCEPTED + assigned to the lead driver so the map shows
            # an "on trip" driver pin (with the pulsing brass halo) and a
            # connected route. Eleanor (d2) holds the lead-driver role and
            # gets flipped to ON_TRIP to match.
            t2 = Trip(
                passenger_name="Davenport-Hayes wedding party (8 pax)",
                passenger_phone="+19125558888",
                pickup_label="SAV Terminal",
                pickup_lat=SAV_TERMINAL[0],
                pickup_lon=SAV_TERMINAL[1],
                dropoff_label="The Lodge, Sea Island",
                dropoff_lat=CLOISTER[0],
                dropoff_lon=CLOISTER[1],
                pickup_time=datetime.now(timezone.utc) + timedelta(hours=8),
                vehicle_class=VehicleClass.SPRINTER,
                notes="Group of 8 plus luggage; bridal party. Champagne service requested.",
                status=TripStatus.ACCEPTED,
            )
            s.add_all([t1, t2])
            s.flush()  # assign IDs before we cross-reference drivers
            # Assign t2 to Eleanor (d2) so the map can render the "driver on
            # trip" state vividly. If the drivers were re-seeded (or the
            # seed re-ran with existing data), skip the cross-link.
            eleanor = s.query(Driver).filter(
                Driver.phone == "+19125550102"
            ).first()
            if eleanor is not None:
                t2.assigned_driver_id = eleanor.id
                eleanor.status = DriverStatus.ON_TRIP
            s.commit()
            print(f"Seeded 2 trips (ids {t1.id}, {t2.id}).")
        else:
            print("Trips already seeded; skipping.")


if __name__ == "__main__":
    seed()
