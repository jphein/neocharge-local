# Stock mosquitto, no HA base needed — there is no run.sh logic, just the
# broker reading its config from /share (mapped by Supervisor).
FROM eclipse-mosquitto:2

CMD ["/usr/sbin/mosquitto", "-c", "/share/neocharge-mqtt/mosquitto.conf"]
