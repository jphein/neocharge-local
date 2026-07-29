# Dedicated anonymous MQTT broker for the NeoCharge Smart Splitter.
#
# In production this runs as a plain `docker run` of the stock eclipse-mosquitto:2
# image with the config bind-mounted (see deploy.sh / reproduction.md) — no custom
# image is strictly required. This Dockerfile is provided for completeness / for a
# self-contained image that bakes the config in (e.g. if you'd rather not bind-mount).
#
# Build:  docker build -t neocharge-mqtt -f tooling/Dockerfile tooling/
# Run:    docker run -d --name neocharge-mqtt --restart unless-stopped \
#           --network host neocharge-mqtt
#
# NOTE: --network host is required so the broker can both serve the device on :1893
# (reached via the gatekeeper DNAT) AND bridge to core-mosquitto on 127.0.0.1:1883.
#
# The bridge in neocharge-mqtt.conf authenticates to core-mosquitto with jp/<pw>.
# Baking the config in therefore bakes that credential into the image — prefer the
# bind-mount approach for anything shared. See ../docs/security.md.

FROM eclipse-mosquitto:2

COPY neocharge-mqtt.conf /mosquitto/config/mosquitto.conf

EXPOSE 1893
# (host networking is used at runtime; EXPOSE is documentary only)
