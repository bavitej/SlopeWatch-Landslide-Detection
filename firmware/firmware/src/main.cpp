#include <Arduino.h>
#include <Wire.h>
#include <math.h>
#include <string.h>

// STM32F411CE Black Pill: dual MPU6050 + analog soil moisture.
// Demonstration/research-inspired indicators only; NOT a certified warning system.
// All measurement values in the JSON stream identify their actual source.

#define SDA_PIN PB7
#define SCL_PIN PB6
#define TOP_AD0 PA9
#define BOTTOM_AD0 PA10
#define SOIL_PIN PA0              // AO wire of a 3-wire capacitive sensor
#define LED_PIN PC13
#define TOP_ADDR 0x68
#define BOTTOM_ADDR 0x69

const float PIPE_LENGTH_MM = 250.0f;
const float DEMO_WARN_DEG = 1.0f;
const float DEMO_HIGH_DEG = 3.0f;
const float RESEARCH_WARN_DEG_PER_H = 0.03f;
const float RESEARCH_HIGH_DEG_PER_H = 0.10f;
const float VIB_THRESHOLD_G = 0.09f;
const float GRAVITY_ALPHA = 0.08f;
const int WETNESS_THRESHOLD_PERCENT = 70; // locally calibrated relative index, NOT VWC

const unsigned long IMU_SAMPLE_MS = 25UL;
const unsigned long SOIL_SAMPLE_MS = 1000UL;
const unsigned long PRINT_MS = 500UL;
const unsigned long RETRY_MS = 2000UL;
const unsigned long TILT_CONFIRM_MS = 3000UL;
const unsigned long VIB_HOLD_MS = 3000UL;
const unsigned long HISTORY_INTERVAL_MS = 60000UL;
const uint8_t HISTORY_SAMPLES = 61;       // 60 minute change, sampled every minute
const float RAD_TO_DEG_F = 57.2957795f;
const float DEG_TO_RAD_F = 0.0174532925f;

enum RunMode { MODE_DEMO, MODE_RESEARCH };
RunMode runMode = MODE_DEMO;

struct IMU {
  uint8_t address;
  bool connected = false;
  bool calibrated = false;
  uint8_t failures = 0;
  unsigned long lastRetry = 0;
  uint32_t successfulReads = 0;
  uint32_t failedReads = 0;
  float ax = 0, ay = 0, az = 0;
  float gx = 0, gy = 0, gz = 0;
  float biasX = 0, biasY = 0, biasZ = 0;
  float roll = 0, pitch = 0, tiltChange = 0;
  float filteredTilt = 0;
  float refX = 0, refY = 0, refZ = 1;
  float gravX = 0, gravY = 0, gravZ = 1;
  float vibrationInstant = 0, vibrationPeak = 0, windowPeak = 0;
};

IMU top = {TOP_ADDR};
IMU bottom = {BOTTOM_ADDR};
unsigned long lastIMU = 0, lastSoil = 0, lastPrint = 0;
unsigned long lastVibrationTime = 0;
bool vibrationSeen = false;
bool ledState = false;

int soilRaw = 0;
int dryRaw = -1, wetRaw = -1;
int relativeWetness = -1;
bool soilCalibrated = false;
bool soilWet = false;

// Research mode: a full hour of readings is needed before comparing hourly rates.
float histTop[HISTORY_SAMPLES] = {};
float histBottom[HISTORY_SAMPLES] = {};
uint8_t histIndex = 0;
uint8_t histCount = 0;
unsigned long lastHistory = 0;
float tiltRateTop = 0, tiltRateBottom = 0;
bool rateReady = false;

bool warningTiming = false, highTiming = false;
unsigned long warningStart = 0, highStart = 0;
bool tiltWarn = false, tiltHigh = false;

char commandBuffer[64];
uint8_t commandIndex = 0;

void initializeI2C() {
  // Preserve the exact address-select/init sequence from the working program.
  digitalWrite(TOP_AD0, LOW);
  digitalWrite(BOTTOM_AD0, HIGH);
  pinMode(TOP_AD0, OUTPUT);
  pinMode(BOTTOM_AD0, OUTPUT);
  delay(100);
  Wire.setSDA(SDA_PIN);
  Wire.setSCL(SCL_PIN);
  Wire.begin();
  Wire.setClock(50000);
}

void scanI2C() {
  Serial.println("# I2C SCAN START");
  for (uint8_t address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      Serial.print("# I2C DEVICE 0x");
      Serial.println(address, HEX);
    }
  }
  Serial.println("# I2C SCAN END");
}

bool writeMPU(uint8_t address, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool initializeMPU(uint8_t address) {
  Wire.beginTransmission(address);
  if (Wire.endTransmission() != 0) return false;
  if (!writeMPU(address, 0x6B, 0x00)) return false; // wake
  delay(20);
  if (!writeMPU(address, 0x1C, 0x00)) return false; // +/-2g
  if (!writeMPU(address, 0x1B, 0x00)) return false; // +/-250 deg/s
  if (!writeMPU(address, 0x1A, 0x03)) return false; // DLPF
  return true;
}

bool readMPU(IMU &m) {
  // Working 14-byte burst read; retain repeated-start transaction.
  Wire.beginTransmission(m.address);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) {
    m.failedReads++;
    return false;
  }
  if (Wire.requestFrom((uint8_t)m.address, (uint8_t)14) != 14) {
    m.failedReads++;
    return false;
  }
  int16_t raw[7];
  for (int i = 0; i < 7; i++) {
    raw[i] = (int16_t)((Wire.read() << 8) | Wire.read());
  }
  m.ax = raw[0] / 16384.0f;
  m.ay = raw[1] / 16384.0f;
  m.az = raw[2] / 16384.0f;
  m.gx = raw[4] / 131.0f;
  m.gy = raw[5] / 131.0f;
  m.gz = raw[6] / 131.0f;
  m.successfulReads++;
  return true;
}

bool calibrateIMU(IMU &m) {
  const int samples = 80;
  float sumAX = 0, sumAY = 0, sumAZ = 0;
  float sumGX = 0, sumGY = 0, sumGZ = 0;
  int successful = 0;
  for (int i = 0; i < samples; i++) {
    if (readMPU(m)) {
      sumAX += m.ax; sumAY += m.ay; sumAZ += m.az;
      sumGX += m.gx; sumGY += m.gy; sumGZ += m.gz;
      successful++;
    }
    delay(10);
  }
  if (successful < 70) { m.calibrated = false; return false; }
  float ax = sumAX / successful, ay = sumAY / successful, az = sumAZ / successful;
  float norm = sqrtf(ax * ax + ay * ay + az * az);
  if (norm < 0.1f) { m.calibrated = false; return false; }
  m.refX = ax / norm; m.refY = ay / norm; m.refZ = az / norm;
  m.gravX = ax; m.gravY = ay; m.gravZ = az;
  m.biasX = sumGX / successful;
  m.biasY = sumGY / successful;
  m.biasZ = sumGZ / successful;
  m.tiltChange = 0; m.filteredTilt = 0;
  m.vibrationInstant = 0; m.windowPeak = 0; m.vibrationPeak = 0;
  m.failures = 0; m.calibrated = true;
  return true;
}

void resetRateHistory() {
  histIndex = 0; histCount = 0; rateReady = false;
  tiltRateTop = 0; tiltRateBottom = 0;
  lastHistory = millis();
  warningTiming = false; highTiming = false;
  tiltWarn = false; tiltHigh = false;
}

void calculateTilt(IMU &m) {
  m.roll = atan2f(m.ay, m.az) * RAD_TO_DEG_F;
  m.pitch = atan2f(-m.ax, sqrtf(m.ay*m.ay + m.az*m.az)) * RAD_TO_DEG_F;
  float norm = sqrtf(m.ax*m.ax + m.ay*m.ay + m.az*m.az);
  if (norm < 0.1f) return;
  float dot = (m.ax * m.refX + m.ay * m.refY + m.az * m.refZ) / norm;
  dot = constrain(dot, -1.0f, 1.0f);
  m.tiltChange = acosf(dot) * RAD_TO_DEG_F;
  m.filteredTilt += 0.04f * (m.tiltChange - m.filteredTilt);
}

void calculateVibration(IMU &m) {
  // A gravity high-pass residual: sensitive to knocks, NOT Omron D7S SI data.
  m.gravX += GRAVITY_ALPHA * (m.ax - m.gravX);
  m.gravY += GRAVITY_ALPHA * (m.ay - m.gravY);
  m.gravZ += GRAVITY_ALPHA * (m.az - m.gravZ);
  float vx = m.ax - m.gravX;
  float vy = m.ay - m.gravY;
  float vz = m.az - m.gravZ;
  m.vibrationInstant = sqrtf(vx*vx + vy*vy + vz*vz);
  if (m.vibrationInstant > m.windowPeak) m.windowPeak = m.vibrationInstant;
  if (m.vibrationInstant >= VIB_THRESHOLD_G) {
    lastVibrationTime = millis();
    vibrationSeen = true;
  }
}

void updateIMU(IMU &m) {
  unsigned long now = millis();
  if (!m.connected || !m.calibrated) {
    if (now - m.lastRetry < RETRY_MS) return;
    m.lastRetry = now;
    if (initializeMPU(m.address)) {
      Serial.print("# CALIBRATING 0x"); Serial.println(m.address, HEX);
      if (calibrateIMU(m)) {
        m.connected = true;
        resetRateHistory();
        Serial.print("# MPU RECONNECTED 0x"); Serial.println(m.address, HEX);
      } else m.connected = false;
    }
    return;
  }
  if (!readMPU(m)) {
    m.failures++;
    if (m.failures >= 3) {
      m.connected = false; m.calibrated = false; m.lastRetry = now;
      resetRateHistory();
      Serial.print("# MPU LOST 0x"); Serial.println(m.address, HEX);
    }
    return;
  }
  m.failures = 0;
  calculateTilt(m);
  calculateVibration(m);
}

void updateSoil() {
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) {
    sum += analogRead(SOIL_PIN);
    delayMicroseconds(200);
  }
  soilRaw = (int)(sum / 16);
  soilCalibrated = dryRaw >= 0 && wetRaw >= 0 && abs(dryRaw - wetRaw) >= 100;
  if (soilCalibrated) {
    float fraction = (float)(soilRaw - dryRaw) / (float)(wetRaw - dryRaw);
    relativeWetness = constrain((int)roundf(100.0f * fraction), 0, 100);
    soilWet = relativeWetness >= WETNESS_THRESHOLD_PERCENT;
  } else {
    relativeWetness = -1;
    soilWet = false;
  }
}

void pushHistory() {
  if (!(top.connected && bottom.connected && top.calibrated && bottom.calibrated)) return;
  histTop[histIndex] = top.filteredTilt;
  histBottom[histIndex] = bottom.filteredTilt;
  histIndex = (uint8_t)((histIndex + 1) % HISTORY_SAMPLES);
  if (histCount < HISTORY_SAMPLES) histCount++;
  if (histCount == HISTORY_SAMPLES) {
    // Oldest and newest points span 60 minutes once the buffer has filled.
    float newestTop = histTop[(histIndex + HISTORY_SAMPLES - 1) % HISTORY_SAMPLES];
    float newestBottom = histBottom[(histIndex + HISTORY_SAMPLES - 1) % HISTORY_SAMPLES];
    tiltRateTop = fabsf(newestTop - histTop[histIndex]);
    tiltRateBottom = fabsf(newestBottom - histBottom[histIndex]);
    rateReady = true;
  }
}

void updateIndicators(unsigned long now) {
  bool bothReady = top.connected && bottom.connected && top.calibrated && bottom.calibrated;
  if (!bothReady) {
    warningTiming = highTiming = false;
    tiltWarn = tiltHigh = false;
    return;
  }
  if (runMode == MODE_RESEARCH) {
    // Research examples: hourly rate thresholds, not validated for this soil/rod.
    float rate = max(tiltRateTop, tiltRateBottom);
    tiltWarn = rateReady && rate >= RESEARCH_WARN_DEG_PER_H;
    tiltHigh = rateReady && rate >= RESEARCH_HIGH_DEG_PER_H;
  } else {
    float tilt = max(top.filteredTilt, bottom.filteredTilt);
    if (tilt >= DEMO_WARN_DEG) {
      if (!warningTiming) { warningTiming = true; warningStart = now; }
    } else warningTiming = false;
    if (tilt >= DEMO_HIGH_DEG) {
      if (!highTiming) { highTiming = true; highStart = now; }
    } else highTiming = false;
    tiltWarn = warningTiming && (now - warningStart >= TILT_CONFIRM_MS);
    tiltHigh = highTiming && (now - highStart >= TILT_CONFIRM_MS);
  }
}

bool recentVibration() {
  return vibrationSeen && millis() - lastVibrationTime <= VIB_HOLD_MS;
}

const char* statusText() {
  if (!top.connected || !bottom.connected || !top.calibrated || !bottom.calibrated) return "SENSOR FAULT";
  if (runMode == MODE_RESEARCH && !rateReady) return "COLLECTING BASELINE";
  bool vib = recentVibration();
  if (tiltHigh) return "HIGH ALERT";
  if (tiltWarn && (soilWet || vib)) return "WARNING";
  if (tiltWarn || (soilWet && vib)) return "CAUTION";
  if (vib) return "DISTURBANCE";
  if (soilWet) return "WET CONDITION";
  if (!soilCalibrated) return "PARTIAL DATA";
  return "NORMAL";
}

void finishVibrationWindow(IMU &m) {
  m.vibrationPeak = m.connected && m.calibrated ? m.windowPeak : 0;
  m.windowPeak = 0;
}

// Compact, machine-readable telemetry for the desktop dashboard.
// Debug messages start with '#', and are not mistaken for sensor data.
void printTelemetry() {
  finishVibrationWindow(top);
  finishVibrationWindow(bottom);
  bool readyA = top.connected && top.calibrated;
  bool readyB = bottom.connected && bottom.calibrated;
  bool both = readyA && readyB;
  float dA = PIPE_LENGTH_MM * sinf(top.tiltChange * DEG_TO_RAD_F);
  float dB = PIPE_LENGTH_MM * sinf(bottom.tiltChange * DEG_TO_RAD_F);
  float vPeak = max(top.vibrationPeak, bottom.vibrationPeak);
  Serial.print("{\"t\":"); Serial.print(millis() / 1000.0f, 1);
  Serial.print(",\"mode\":\""); Serial.print(runMode == MODE_DEMO ? "DEMO" : "RESEARCH"); Serial.print('"');
  Serial.print(",\"a_ok\":"); Serial.print(readyA ? "true" : "false");
  Serial.print(",\"b_ok\":"); Serial.print(readyB ? "true" : "false");
  Serial.print(",\"a_tilt\":"); Serial.print(readyA ? top.tiltChange : -1, 3);
  Serial.print(",\"b_tilt\":"); Serial.print(readyB ? bottom.tiltChange : -1, 3);
  Serial.print(",\"a_roll\":"); Serial.print(readyA ? top.roll : -999, 2);
  Serial.print(",\"a_pitch\":"); Serial.print(readyA ? top.pitch : -999, 2);
  Serial.print(",\"b_roll\":"); Serial.print(readyB ? bottom.roll : -999, 2);
  Serial.print(",\"b_pitch\":"); Serial.print(readyB ? bottom.pitch : -999, 2);
  Serial.print(",\"a_disp\":"); Serial.print(readyA ? dA : -1, 2);
  Serial.print(",\"b_disp\":"); Serial.print(readyB ? dB : -1, 2);
  Serial.print(",\"a_vib\":"); Serial.print(readyA ? top.vibrationPeak : -1, 4);
  Serial.print(",\"b_vib\":"); Serial.print(readyB ? bottom.vibrationPeak : -1, 4);
  Serial.print(",\"vib_peak\":"); Serial.print(both ? vPeak : -1, 4);
  Serial.print(",\"vib_recent\":"); Serial.print(recentVibration() ? "true" : "false");
  Serial.print(",\"soil_raw\":"); Serial.print(soilRaw);
  Serial.print(",\"soil_pct\":"); Serial.print(relativeWetness);
  Serial.print(",\"soil_cal\":"); Serial.print(soilCalibrated ? "true" : "false");
  Serial.print(",\"soil_wet\":"); Serial.print(soilWet ? "true" : "false");
  Serial.print(",\"dry_raw\":"); Serial.print(dryRaw);
  Serial.print(",\"wet_raw\":"); Serial.print(wetRaw);
  Serial.print(",\"rate_a\":"); Serial.print(rateReady ? tiltRateTop : -1, 4);
  Serial.print(",\"rate_b\":"); Serial.print(rateReady ? tiltRateBottom : -1, 4);
  Serial.print(",\"rate_ready\":"); Serial.print(rateReady ? "true" : "false");
  Serial.print(",\"baseline_min\":"); Serial.print(histCount > 0 ? histCount - 1 : 0);
  Serial.print(",\"tilt_flag\":"); Serial.print(tiltWarn ? "true" : "false");
  Serial.print(",\"high_flag\":"); Serial.print(tiltHigh ? "true" : "false");
  Serial.print(",\"moist_flag\":"); Serial.print(soilWet ? "true" : "false");
  Serial.print(",\"vib_flag\":"); Serial.print(recentVibration() ? "true" : "false");
  Serial.print(",\"a_reads\":"); Serial.print(top.successfulReads);
  Serial.print(",\"b_reads\":"); Serial.print(bottom.successfulReads);
  Serial.print(",\"a_fail\":"); Serial.print(top.failedReads);
  Serial.print(",\"b_fail\":"); Serial.print(bottom.failedReads);
  Serial.print(",\"status\":\""); Serial.print(statusText()); Serial.println("\"}");
}

void handleCommand(const char* command) {
  if (strcmp(command, "MODE DEMO") == 0) {
    runMode = MODE_DEMO; warningTiming = highTiming = false;
    tiltWarn = tiltHigh = false;
    Serial.println("# MODE DEMO");
  } else if (strcmp(command, "MODE RESEARCH") == 0) {
    runMode = MODE_RESEARCH; warningTiming = highTiming = false;
    tiltWarn = tiltHigh = false;
    Serial.println("# MODE RESEARCH");
  } else if (strcmp(command, "CAL_DRY") == 0) {
    updateSoil(); dryRaw = soilRaw; updateSoil();
    Serial.print("# DRY RAW "); Serial.println(dryRaw);
  } else if (strcmp(command, "CAL_WET") == 0) {
    updateSoil(); wetRaw = soilRaw; updateSoil();
    Serial.print("# WET RAW "); Serial.println(wetRaw);
  } else if (strcmp(command, "RESET_SOIL") == 0) {
    dryRaw = wetRaw = -1; updateSoil();
    Serial.println("# SOIL CALIBRATION CLEARED");
  } else if (strcmp(command, "PING") == 0) {    Serial.println("# PONG");
  } else {
    Serial.println("# UNKNOWN COMMAND");
  }
}

void checkSerialCommands() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      commandBuffer[commandIndex] = '\0';
      if (commandIndex > 0) handleCommand(commandBuffer);
      commandIndex = 0;
    } else if (commandIndex < sizeof(commandBuffer) - 1) {
      commandBuffer[commandIndex++] = c;
    } else {
      commandIndex = 0;
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(2000);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  pinMode(SOIL_PIN, INPUT);
  analogReadResolution(12);
  initializeI2C();
  Serial.println("# SLOPEWATCH firmware 1.0");
  scanI2C();
  top.connected = initializeMPU(TOP_ADDR);
  bottom.connected = initializeMPU(BOTTOM_ADDR);
  Serial.print("# MPU 0x68: "); Serial.println(top.connected ? "INITIALIZED" : "MISSING");
  Serial.print("# MPU 0x69: "); Serial.println(bottom.connected ? "INITIALIZED" : "MISSING");
  Serial.println("# Keep rod stationary during calibration");
  if (top.connected) top.connected = calibrateIMU(top);
  if (bottom.connected) bottom.connected = calibrateIMU(bottom);
  top.lastRetry = bottom.lastRetry = millis();
  if (top.connected && readMPU(top)) calculateTilt(top);
  if (bottom.connected && readMPU(bottom)) calculateTilt(bottom);
  updateSoil();
  lastIMU = lastSoil = lastPrint = millis();
  resetRateHistory();
  pushHistory();            // t=0 snapshot, then one each minute
  Serial.println("# MONITORING STARTED");
}

void loop() {
  unsigned long now = millis();
  checkSerialCommands();
  if (now - lastIMU >= IMU_SAMPLE_MS) {
    lastIMU = now;
    updateIMU(top);
    updateIMU(bottom);
    updateIndicators(millis());
  }
  if (now - lastSoil >= SOIL_SAMPLE_MS) {
    lastSoil = now;
    updateSoil();
  }
  if (now - lastHistory >= HISTORY_INTERVAL_MS) {
    lastHistory = now;
    pushHistory();
    updateIndicators(now);
  }
  if (now - lastPrint >= PRINT_MS) {
    lastPrint = now;
    ledState = !ledState;
    digitalWrite(LED_PIN, ledState ? LOW : HIGH);
    printTelemetry();
  }
}