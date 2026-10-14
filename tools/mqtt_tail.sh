#!/bin/sh
# Follow kiosk and discovery topics. Usage: mqtt_tail.sh [broker-host]
host=${1:-homeassistant.local}
exec mosquitto_sub -h "$host" -t 'pikiosk/#' -t 'homeassistant/+/pikiosk_#' -v
