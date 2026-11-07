/*
  Recap badge firmware (ESP32, Arduino IDE).

  Reads:
    - Throat effort:  MyoWare 2.0 EMG "ENV" (envelope) output on EMG_PIN
                      and/or an MPU-6050 neck accelerometer on I2C (vibration energy)
    - Breath trainer: analog pressure sensor (e.g. MPX5010DP) on PRESSURE_PIN
  Prints one JSON line every ~100 ms over USB serial (115200 baud):
    {"effort": 412.5, "pressure": 3.2}
  client/serial_bridge.py forwards these to the backend.

  SAFETY: anything touching skin must run from USB-from-a-laptop-on-battery or
  a LiPo. Never wear the electrodes while the laptop is plugged into the wall.

  Wiring (change pins to match your board):
    MyoWare ENV  -> GPIO 34 (ADC1, input-only)     MyoWare +/- -> 3V3 / GND
    Pressure out -> GPIO 35 (ADC1) through a divider if the sensor outputs 5 V!
    MPU-6050 SDA -> GPIO 21, SCL -> GPIO 22, VCC -> 3V3
*/
#include <Wire.h>

const int EMG_PIN = 34;
const int PRESSURE_PIN = 35;
const bool USE_EMG = true;
const bool USE_ACCEL = true;     // set false if no MPU-6050 is wired
const bool USE_PRESSURE = true;
const uint8_t MPU_ADDR = 0x68;

// Pressure calibration: MPX5010DP gives ~0.2 V at 0 kPa and ~4.7 V at 10 kPa.
// With a 2:1 divider into the ESP32 ADC, measure your zero reading and set these.
float PRESSURE_ZERO_V = 0.10;      // volts at the ADC with no pressure
float PRESSURE_V_PER_KPA = 0.225;  // volts at the ADC per kPa (after divider)
const float CMH2O_PER_KPA = 10.197;

const int SAMPLE_HZ = 1000;
const int REPORT_MS = 100;

float emgEnergy = 0, accelEnergy = 0;
long n = 0;
unsigned long lastReport = 0;
float accelMean = 0;   // running mean to remove gravity

bool mpuOk = false;

void mpuInit() {
  Wire.begin(21, 22);
  Wire.setClock(400000);
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B); Wire.write(0);          // wake up
  mpuOk = (Wire.endTransmission() == 0);
  if (!mpuOk) return;
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1A); Wire.write(0x00);       // DLPF off: keep voice-band vibration (~100-300 Hz)
  Wire.endTransmission();
}

float mpuZ() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3F);                          // ACCEL_ZOUT_H
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, (uint8_t)2);
  if (Wire.available() < 2) return 0;
  int16_t z = (Wire.read() << 8) | Wire.read();
  return z / 16384.0;                        // g
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
  if (USE_ACCEL) mpuInit();
}

void loop() {
  static unsigned long lastSample = 0;
  unsigned long now = micros();
  if (now - lastSample >= 1000000UL / SAMPLE_HZ) {
    lastSample = now;
    if (USE_EMG) {
      float v = analogRead(EMG_PIN) / 4095.0 * 3.3;
      emgEnergy += v * v;
    }
    if (USE_ACCEL && mpuOk) {
      float z = mpuZ();
      accelMean = 0.999 * accelMean + 0.001 * z;   // slow mean = gravity + posture
      float d = z - accelMean;                     // what's left = voice vibration
      accelEnergy += d * d;
    }
    n++;
  }

  if (millis() - lastReport >= REPORT_MS && n > 0) {
    lastReport = millis();
    // effort: RMS of EMG envelope plus scaled vibration RMS (arbitrary units;
    // the backend only uses it relative to Alex's own fresh baseline).
    float effort = 0;
    if (USE_EMG) effort += 1000.0 * sqrt(emgEnergy / n);
    if (USE_ACCEL && mpuOk) effort += 20000.0 * sqrt(accelEnergy / n);

    Serial.print("{\"effort\":");
    Serial.print(effort, 2);
    if (USE_PRESSURE) {
      float v = analogRead(PRESSURE_PIN) / 4095.0 * 3.3;
      float kpa = max(0.0f, (v - PRESSURE_ZERO_V) / PRESSURE_V_PER_KPA);
      Serial.print(",\"pressure\":");
      Serial.print(kpa * CMH2O_PER_KPA, 1);
    }
    Serial.println("}");
    emgEnergy = 0; accelEnergy = 0; n = 0;
  }
}
