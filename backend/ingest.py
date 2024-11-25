"""MQTT -> SQLite ingest. Single writer for `sensor_readings`.

Subscribes to the ESP32 topic, validates each payload, inserts into
agrisense.db (next to this file). Run: python ingest.py (Ctrl+C to stop).

FIXME: public HiveMQ broker, plain TCP 1883, no auth. Fine for the sim;
needs TLS + per-device credentials before a real node goes in the field.
"""

import json
import math
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

MQTT_HOST = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "agrisense/demo/field_001/sensors"
MQTT_KEEPALIVE = 60  # seconds
# Random per run: the broker kicks the older session when two clients share an ID.
MQTT_CLIENT_ID = f"agrisense_ingest_{uuid.uuid4().hex[:12]}"

DB_PATH = Path(__file__).resolve().parent / "agrisense.db"

# Plausibility gates (inclusive). Range check only, no rate-of-change or
# stuck-value detection.
HUMIDITY_RANGE = (0.0, 100.0)
# TODO: soil value is a raw potentiometer percentage in the sim. With a real
# capacitive probe, gate on per-probe dry/wet ADC counts instead of 0-100.
SOIL_MOISTURE_RANGE = (0.0, 100.0)
# DHT22 spec: -40..80 C.
TEMPERATURE_RANGE = (-40.0, 80.0)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    temperature_c REAL,
    humidity_pct REAL,
    soil_moisture_pct REAL,
    is_simulation BOOLEAN NOT NULL DEFAULT 1,
    sensor_status TEXT,
    is_valid BOOLEAN DEFAULT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(device_id, field_id, timestamp)
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO sensor_readings (
    device_id, field_id, timestamp,
    temperature_c, humidity_pct, soil_moisture_pct,
    is_simulation, sensor_status, is_valid
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


class PayloadError(ValueError):
    pass


def init_db(db_path=DB_PATH):
    # FIXME: default journal mode; the dashboard and assessment readers can hit
    # SQLITE_BUSY under write bursts. Try WAL if it shows up on hardware.
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:  # commit/rollback
            conn.execute(CREATE_TABLE_SQL)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def save_reading(conn, record):
    with conn:  # one txn per record: a crash loses at most one reading
        cursor = conn.execute(
            INSERT_SQL,
            (
                record["device_id"],
                record["field_id"],
                record["timestamp"],
                record["temperature_c"],
                record["humidity_pct"],
                record["soil_moisture_pct"],
                int(record["is_simulation"]),
                record["sensor_status"],
                int(record["is_valid"]),
            ),
        )
        # rowcount is 0 when OR IGNORE swallowed a duplicate.
        return cursor.rowcount == 1


def _require_dict(parent, key, label):
    value = parent.get(key)
    if not isinstance(value, dict):
        raise PayloadError(f"'{label}' is missing or not a JSON object")
    return value


def _require_str(parent, key, label):
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"'{label}' is missing or not a non-empty string")
    return value.strip()


def _require_number(parent, key, label):
    value = parent.get(key)
    # bool is an int subclass; json `true` must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadError(f"'{label}' is missing or not a number")
    return float(value)


def _in_range(value, bounds):
    return math.isfinite(value) and bounds[0] <= value <= bounds[1]


def _check_timestamp(value):
    # TODO: timestamps come from the node clock. A real ESP32 needs NTP/RTC
    # sync; consider stamping on receipt as well to catch drift.
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise PayloadError(f"'timestamp' is not valid ISO 8601: {value!r}")


def parse_payload(raw_payload):
    try:
        text = raw_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadError(f"payload is not valid UTF-8: {exc}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"payload is not valid JSON: {exc}")

    if not isinstance(data, dict):
        raise PayloadError("payload JSON is not an object")

    device_id = _require_str(data, "device_id", "device_id")
    field_id = _require_str(data, "field_id", "field_id")
    timestamp = _require_str(data, "timestamp", "timestamp")
    _check_timestamp(timestamp)

    readings = _require_dict(data, "readings", "readings")
    temperature = _require_number(readings, "temperature_c", "readings.temperature_c")
    humidity = _require_number(readings, "humidity_pct", "readings.humidity_pct")
    soil = _require_number(readings, "soil_moisture_pct", "readings.soil_moisture_pct")

    metadata = _require_dict(data, "metadata", "metadata")
    simulation = metadata.get("simulation")
    if not isinstance(simulation, bool):
        raise PayloadError("'metadata.simulation' is missing or not a boolean")

    # Optional (nullable column), text only.
    sensor_status = metadata.get("sensor_status")
    if sensor_status is not None and not isinstance(sensor_status, str):
        raise PayloadError("'metadata.sensor_status' must be a string")

    is_valid = (
        _in_range(temperature, TEMPERATURE_RANGE)
        and _in_range(humidity, HUMIDITY_RANGE)
        and _in_range(soil, SOIL_MOISTURE_RANGE)
    )

    def storable(value):
        # SQLite turns NaN into NULL silently and can't hold inf; be explicit.
        # The row is is_valid=0 regardless.
        return value if math.isfinite(value) else None

    return {
        "device_id": device_id,
        "field_id": field_id,
        "timestamp": timestamp,
        "temperature_c": storable(temperature),
        "humidity_pct": storable(humidity),
        "soil_moisture_pct": storable(soil),
        "is_simulation": simulation,
        "sensor_status": sensor_status,
        "is_valid": is_valid,
    }


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[MQTT] Connection refused by broker: {reason_code}")
        return
    print(f"[MQTT] Connected to {MQTT_HOST}:{MQTT_PORT} as '{MQTT_CLIENT_ID}'")
    result, _mid = client.subscribe(MQTT_TOPIC)
    if result == mqtt.MQTT_ERR_SUCCESS:
        print(f"[MQTT] Subscribing to '{MQTT_TOPIC}' ...")
    else:
        print(f"[MQTT] Subscribe request failed: {mqtt.error_string(result)}")


def on_subscribe(client, userdata, mid, reason_code_list, properties):
    for rc in reason_code_list:
        if rc.is_failure:
            print(f"[MQTT] Subscription rejected by broker: {rc}")
        else:
            print(f"[MQTT] Subscribed. Waiting for messages ...")


def on_connect_fail(client, userdata):
    print("[MQTT] Connection attempt failed. Retrying automatically ...")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[MQTT] Unexpected disconnect ({reason_code}). Reconnecting ...")
    else:
        print("[MQTT] Disconnected.")


def on_message(client, userdata, msg):
    conn = userdata["db"]
    try:
        record = parse_payload(msg.payload)
    except PayloadError as exc:
        print(f"[REJECTED] {exc}")
        return
    except Exception as exc:  # catch-all, see docstring
        print(f"[ERROR] Unexpected error while parsing: {exc!r}")
        return

    try:
        inserted = save_reading(conn, record)
    except sqlite3.Error as exc:
        print(f"[DB ERROR] Could not save reading: {exc}")
        return

    summary = (
        f"{record['device_id']} / {record['field_id']} @ {record['timestamp']} | "
        f"T={record['temperature_c']} C, H={record['humidity_pct']} %, "
        f"Soil={record['soil_moisture_pct']} %"
    )
    if not inserted:
        print(f"[DUPLICATE] Already stored, ignored: {summary}")
    elif record["is_valid"]:
        print(f"[INSERTED] {summary} | is_valid=1")
    else:
        print(f"[INSERTED] {summary} | is_valid=0 (out-of-range value)")


def create_client(conn):
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
        userdata={"db": conn},
    )
    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    # Exponential backoff 1 -> 30 s.
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def run_mqtt(client):
    print(f"[MQTT] Connecting to {MQTT_HOST}:{MQTT_PORT} ...")
    # retry_first_connection: boot without network shouldn't be fatal.
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
    client.loop_forever(retry_first_connection=True)


def main():
    try:
        conn = init_db()
    except sqlite3.Error as exc:
        print(f"[DB ERROR] Could not initialise database at {DB_PATH}: {exc}")
        return 1

    print(f"[DB] Using database: {DB_PATH}")
    client = create_client(conn)
    try:
        run_mqtt(client)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping (Ctrl+C) ...")
    except Exception as exc:
        print(f"[FATAL] Unexpected error: {exc!r}")
        return 1
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        conn.close()
        print("[DB] Connection closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
