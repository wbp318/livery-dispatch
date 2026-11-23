# Self-hosted OSRM for the SAV <-> JAX corridor (v1.1.15).
#
#   docker build -t livery-osrm osrm
#   docker run --rm -p 5000:5000 livery-osrm
#   # then in .env:  OSRM_BASE_URL=http://localhost:5000
#
# Data: Geofabrik Georgia + Florida extracts, each clipped to the corridor
# bbox, merged, then MLD-preprocessed at IMAGE BUILD time — the runtime
# container just memory-maps finished files, starts in seconds, and needs
# a few hundred MB of RAM. Rebuild to refresh the road data (OSM edits to
# resort drives, new construction): the image is the data snapshot.
#
# BBOX must match CORRIDOR_BBOX in osrm_proxy.py (min_lon,min_lat,
# max_lon,max_lat): SAV + the Statesboro / GSU Tier-2 catchment in the north
# through JAX and St. Augustine in the south.
#
# Licensing: OSRM is BSD-2-Clause; OSM data is ODbL — keep the
# "© OpenStreetMap contributors" attribution the maps already show.
# See osrm/README.md for the Fly.io deploy.

ARG OSRM_IMAGE=ghcr.io/project-osrm/osrm-backend:v5.27.1

# --- 1. Download + clip + merge the OSM extract ----------------------------
FROM debian:bookworm-slim AS clip
ARG BBOX=-82.8,29.8,-80.6,32.9
ARG GEOFABRIK=https://download.geofabrik.de/north-america/us
RUN apt-get update \
 && apt-get install -y --no-install-recommends osmium-tool curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /data
RUN curl -fsSL --retry 5 --retry-all-errors --retry-delay 15 -o georgia.osm.pbf "$GEOFABRIK/georgia-latest.osm.pbf" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-delay 15 -o florida.osm.pbf "$GEOFABRIK/florida-latest.osm.pbf" \
 && osmium extract --bbox "$BBOX" --strategy smart -o ga.osm.pbf georgia.osm.pbf \
 && osmium extract --bbox "$BBOX" --strategy smart -o fl.osm.pbf florida.osm.pbf \
 && osmium merge -o corridor.osm.pbf ga.osm.pbf fl.osm.pbf \
 && rm georgia.osm.pbf florida.osm.pbf ga.osm.pbf fl.osm.pbf

# --- 2. OSRM preprocessing (car profile, multi-level Dijkstra) -------------
FROM ${OSRM_IMAGE} AS build
COPY --from=clip /data/corridor.osm.pbf /data/corridor.osm.pbf
RUN osrm-extract -p /opt/car.lua /data/corridor.osm.pbf \
 && osrm-partition /data/corridor.osrm \
 && osrm-customize /data/corridor.osrm \
 && rm /data/corridor.osm.pbf

# --- 3. Runtime ------------------------------------------------------------
FROM ${OSRM_IMAGE}
COPY --from=build /data /data
EXPOSE 5000
# Only the route service is proxied (osrm_proxy.py); keep the
# table service's matrix size small so a stray caller can't burn CPU.
CMD ["osrm-routed", "--algorithm", "mld", "--port", "5000", "--max-table-size", "16", "/data/corridor.osrm"]
