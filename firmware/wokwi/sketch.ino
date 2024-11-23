#include <WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <ArduinoJson.h>
#include <time.h>

// Sensor Config
#define DHTPIN 4
#define DHTTYPE DHT22
#define POT_PIN 34
DHT dht(DHTPIN, DHTTYPE);

// Network Config (Wokwi Virtual WiFi)
const char* ssid = "Wokwi-GUEST";
const char* password = "";

// MQTT Config
const char* mqtt_server = "broker.hivemq.com";
const char* mqtt_topic = "agrisense/demo/field_001/sensors";
const char* client_id = "agrisense_node_14112026001x1107"; // Must be unique

// NTP Time Config
const char* ntpServer = "pool.ntp.org";
const long  gmtOffset_sec = 0;
const int   daylightOffset_sec = 0;

WiFiClient espClient;
PubSubClient client(espClient);

void setup_wifi() {
  delay(10);
  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected");
}

void reconnect() {
  while (!client.connected()) {
    Serial.print("Attempting MQTT connection...");
    if (client.connect(client_id)) {
      Serial.println("connected");
    } else {
      Serial.print("failed, rc=");
      Serial.print(client.state());
      Serial.println(" try again in 5 seconds");
      delay(5000);
    }
  }
}

// Function to get ISO 8601 UTC Timestamp
String getTimestamp() {
  struct tm timeinfo;
  if(!getLocalTime(&timeinfo)){
    return "1970-01-01T00:00:00Z";
  }
  char timeStringBuff[30];
  strftime(timeStringBuff, sizeof(timeStringBuff), "%Y-%m-%dT%H:%M:%SZ", &timeinfo);
  return String(timeStringBuff);
}

void setup() {
  Serial.begin(115200);
  dht.begin();
  analogReadResolution(12);

  setup_wifi();
  client.setBufferSize(512);
  client.setServer(mqtt_server, 1883);

  // Init NTP
  configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  float h = dht.readHumidity();
  float t = dht.readTemperature();
  int potValue = analogRead(POT_PIN);
  float soilMoisture = map(potValue, 0, 4095, 0, 100);

  if (isnan(h) || isnan(t)) {
    Serial.println("Failed to read from DHT sensor!");
    delay(5000);
    return;
  }

  StaticJsonDocument<256> doc;
  doc["device_id"] = "agrisense_001";
  doc["field_id"] = "field_001";
  doc["timestamp"] = getTimestamp();

  JsonObject readings = doc.createNestedObject("readings");
  readings["temperature_c"] = t;
  readings["humidity_pct"] = h;
  readings["soil_moisture_pct"] = soilMoisture;

  JsonObject metadata = doc.createNestedObject("metadata");
  metadata["simulation"] = true;
  metadata["sensor_status"] = "normal";

  char jsonBuffer[256];
  serializeJson(doc, jsonBuffer);

  Serial.print("Publishing message: ");
  Serial.println(jsonBuffer);

  client.publish(mqtt_topic, jsonBuffer);

  delay(5000);
}
