"""Sample default rate cards (shared by seed.py and `cli.py rates seed-defaults`).

The numbers are illustrative. Replace them with your own rates before going
live. Change the card values, not the schema. Money
is in cents so DB math stays integer-only.

Before v1.1.18 these lived inline in the dev-only `seed.py`, so a fresh
production database had no rate cards and no way to get any. `/admin/rates`
could edit cards but not create them, and the public `/quote` soft-failed
(E6). Production now loads them with `python cli.py rates seed-defaults`, or
the dispatcher creates cards at `/admin/rates/new`.
"""

from __future__ import annotations

from models import RateCard, VehicleClass

_PLANNING_NOTE = (
    "Sample rates. Replace with your own before going live."
)


def planning_rate_cards() -> list[RateCard]:
    """Fresh (unsaved) RateCard objects: one SUV card, one Sprinter card."""
    return [
        RateCard(
            name="Standard SUV (Escalade ESV)",
            vehicle_class=VehicleClass.SUV,
            base_fare_cents=15000,               # $150.00
            per_mile_cents=550,                  # $5.50/mi
            per_hour_cents=12500,                # $125/hr (hourly hires)
            airport_meet_surcharge_cents=2500,   # $25.00
            after_hours_surcharge_cents=2500,    # $25.00 (10pm–6am)
            peak_week_multiplier=1.25,
            minimum_fare_cents=12500,            # $125.00 minimum
            active=True,
            notes=_PLANNING_NOTE,
        ),
        RateCard(
            name="Sprinter Limited (executive)",
            vehicle_class=VehicleClass.SPRINTER,
            base_fare_cents=25000,               # $250.00
            per_mile_cents=800,                  # $8.00/mi
            per_hour_cents=20000,                # $200/hr
            airport_meet_surcharge_cents=3500,   # $35.00
            after_hours_surcharge_cents=3500,    # $35.00
            peak_week_multiplier=1.30,
            minimum_fare_cents=25000,            # $250.00 minimum
            active=True,
            notes=("Sample rates. Sprinter premium reflects the FMCSA-trigger "
                   "cost stack and the 8–14-pax wedding-party / corporate-group use case."),
        ),
    ]
