#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include "AD524X.h"

/*
  DAQ firmware, cleaned version

  Commands:
    0,0       Stop device
    0,1       Start device
    1,0-255   Set visible PD gain, AD524X channel 0
    2,0-255   Set IR PD gain, AD524X channel 1
    3,0-4095  Set VIS LED DAC code, MCP4725
    4,us      Pulse IR LED for us microseconds
    7,0       Print status
    8,0       Take one ADC sample and print channels 1-4
    9,0       Disable ADC streaming
    9,1       Enable ADC streaming while operating
    10,N      Set ADC stream decimation, prints every N operating loops
*/

#define SDA_PIN 3
#define SCL_PIN 4

#define INDICATOR_LED_PIN 1
#define AUX_PIN_2 2
#define AUX_PIN_8 8
#define IRLED_PIN 16

#define MCP4725_VISLEDADDR 0x61
#define AD524X_ADDR 0x2E

AD524X AD01(AD524X_ADDR);

bool isoperating = false;
bool streamAdc = false;
uint16_t visledlevel = 0;
uint8_t visibleGain = 0;
uint8_t irGain = 0;
uint16_t streamDecimation = 100;
uint32_t sampleCounter = 0;

uint16_t lowdata[4];
uint16_t highdata[4];

static const int PIN_ADC_OS0      = 5;
static const int PIN_ADC_OS1      = 6;
static const int PIN_ADC_OS2      = 7;
static const int PIN_ADC_CONVSTB  = 9;
static const int PIN_ADC_CONVST   = 10;
static const int PIN_ADC_FRSTD    = 11;
static const int PIN_ADC_RESET    = 12;
static const int PIN_ADC_BUSY     = 13;
static const int PIN_ADC_CS       = 33;
static const int PIN_ADC_SCK      = 36;
static const int PIN_ADC_MISO     = 37;

SPIClass adcSPI(FSPI);
SPISettings adcSpiSettings(1000000, MSBFIRST, SPI_MODE1);
static const float FULL_SCALE_VOLTS = 5.0f;

void parseserial();
void setDeviceOperating(bool enable);
void setIndicatorLed(bool enable);
uint16_t clampDacCode(long value);
uint8_t clampPotCode(long value);
bool setVisLed(uint16_t value);
void pulseIrLed(uint32_t duration_us);
void printStatus();
void printSingleAdcRead();
void printAdcStreamLine();

void adcSetOversampling(uint8_t os);
void adcReset();
void adcStartConversion();
bool adcWaitForBusyLow(uint32_t timeout_us = 5000);
uint16_t adcReadWordDOUTA();
bool adcReadChannels1to4(uint16_t ch[4]);
int16_t adcCodeToSigned(uint16_t raw);
float adcCodeToVolts(uint16_t raw);

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);
  delay(1000);

  Serial.println("Booting DAQ firmware");

  pinMode(INDICATOR_LED_PIN, OUTPUT);
  pinMode(AUX_PIN_2, OUTPUT);
  pinMode(AUX_PIN_8, OUTPUT);
  pinMode(IRLED_PIN, OUTPUT);

  setIndicatorLed(false);
  digitalWrite(AUX_PIN_2, LOW);
  digitalWrite(AUX_PIN_8, LOW);
  digitalWrite(IRLED_PIN, LOW);

  Wire.begin(SDA_PIN, SCL_PIN);
  AD01.begin();

  pinMode(PIN_ADC_OS0, OUTPUT);
  pinMode(PIN_ADC_OS1, OUTPUT);
  pinMode(PIN_ADC_OS2, OUTPUT);
  pinMode(PIN_ADC_CONVST, OUTPUT);
  pinMode(PIN_ADC_CONVSTB, OUTPUT);
  pinMode(PIN_ADC_RESET, OUTPUT);
  pinMode(PIN_ADC_CS, OUTPUT);

  pinMode(PIN_ADC_BUSY, INPUT);
  pinMode(PIN_ADC_FRSTD, INPUT);

  digitalWrite(PIN_ADC_CS, HIGH);
  digitalWrite(PIN_ADC_CONVST, LOW);
  digitalWrite(PIN_ADC_CONVSTB, LOW);
  digitalWrite(PIN_ADC_RESET, LOW);

  adcSetOversampling(0);
  adcSPI.begin(PIN_ADC_SCK, PIN_ADC_MISO, -1, PIN_ADC_CS);
  adcReset();

  setVisLed(0);
  AD01.write(0, visibleGain);
  AD01.write(1, irGain);

  Serial.println("Initialization Complete");
}

void loop() {
  if (Serial.available() > 0) {
    parseserial();
  }

  if (isoperating) {
    digitalWrite(IRLED_PIN, HIGH);
    delayMicroseconds(500);

    bool highOk = adcReadChannels1to4(highdata);

    digitalWrite(IRLED_PIN, LOW);
    delayMicroseconds(500);

    bool lowOk = false;
    if (highOk) {
      lowOk = adcReadChannels1to4(lowdata);
    }

    if (!highOk) {
      Serial.println("ERR ADC read failed high phase");
    } else if (!lowOk) {
      Serial.println("ERR ADC read failed low phase");
    } else {
      sampleCounter++;

      if (streamAdc && streamDecimation > 0 && (sampleCounter % streamDecimation == 0)) {
        printAdcStreamLine();
      }
    }

    delay(1);
  }
}

void parseserial() {
  String serialData = Serial.readStringUntil('\n');
  serialData.trim();

  if (serialData.length() == 0) {
    return;
  }

  long command = 0;
  long value = 0;

  if (sscanf(serialData.c_str(), "%ld,%ld", &command, &value) != 2) {
    Serial.print("ERR invalid command: ");
    Serial.println(serialData);
    return;
  }

  switch (command) {
    case 0:
      if (value == 0) {
        setDeviceOperating(false);
      } else if (value == 1) {
        setDeviceOperating(true);
      } else {
        Serial.println("ERR command 0 expects 0 or 1");
      }
      break;

    case 1:
      visibleGain = clampPotCode(value);
      AD01.write(0, visibleGain);
      Serial.print("OK Visible PD Gain Updated ");
      Serial.println(visibleGain);
      break;

    case 2:
      irGain = clampPotCode(value);
      AD01.write(1, irGain);
      Serial.print("OK IR PD Gain Updated ");
      Serial.println(irGain);
      break;

    case 3:
      visledlevel = clampDacCode(value);
      if (setVisLed(visledlevel)) {
        Serial.print("OK Visible LED DAC Updated ");
        Serial.println(visledlevel);
      }
      break;

    case 4:
      pulseIrLed((uint32_t)max(value, 0L));
      Serial.println("OK IR pulse complete");
      break;

    case 7:
      printStatus();
      break;

    case 8:
      printSingleAdcRead();
      break;

    case 9:
      if (value == 0) {
        streamAdc = false;
        Serial.println("OK ADC streaming OFF");
      } else if (value == 1) {
        streamAdc = true;
        Serial.println("OK ADC streaming ON");
      } else {
        Serial.println("ERR command 9 expects 0 or 1");
      }
      break;

    case 10:
      if (value < 1) {
        streamDecimation = 1;
      } else if (value > 65535) {
        streamDecimation = 65535;
      } else {
        streamDecimation = (uint16_t)value;
      }
      Serial.print("OK ADC stream decimation ");
      Serial.println(streamDecimation);
      break;

    default:
      Serial.print("ERR unknown command: ");
      Serial.println(command);
      break;
  }
}

void setDeviceOperating(bool enable) {
  if (enable) {
    setVisLed(visledlevel);
    isoperating = true;
    setIndicatorLed(true);
    Serial.println("OK Device ON");
  } else {
    isoperating = false;
    streamAdc = false;
    setIndicatorLed(false);
    setVisLed(0);
    digitalWrite(IRLED_PIN, LOW);
    Serial.println("OK Device OFF");
  }
}

void setIndicatorLed(bool enable) {
  digitalWrite(INDICATOR_LED_PIN, enable ? HIGH : LOW);
}

uint16_t clampDacCode(long value) {
  if (value < 0) return 0;
  if (value > 4095) return 4095;
  return (uint16_t)value;
}

uint8_t clampPotCode(long value) {
  if (value < 0) return 0;
  if (value > 255) return 255;
  return (uint8_t)value;
}

bool setVisLed(uint16_t value) {
  value = clampDacCode(value);

  Wire.beginTransmission(MCP4725_VISLEDADDR);
  Wire.write(0x40);
  Wire.write(value >> 4);
  Wire.write((value & 0x0F) << 4);
  uint8_t err = Wire.endTransmission();

  if (err != 0) {
    Serial.print("ERR MCP4725 I2C error ");
    Serial.println(err);
    return false;
  }

  return true;
}

void pulseIrLed(uint32_t duration_us) {
  digitalWrite(IRLED_PIN, HIGH);
  delayMicroseconds(duration_us);
  digitalWrite(IRLED_PIN, LOW);
}

void printStatus() {
  Serial.print("STATUS operating=");
  Serial.print(isoperating ? 1 : 0);
  Serial.print(",streamAdc=");
  Serial.print(streamAdc ? 1 : 0);
  Serial.print(",streamDecimation=");
  Serial.print(streamDecimation);
  Serial.print(",visDac=");
  Serial.print(visledlevel);
  Serial.print(",visDacVoltsApprox=");
  Serial.print((3.3f * (float)visledlevel) / 4095.0f, 4);
  Serial.print(",visibleGain=");
  Serial.print(visibleGain);
  Serial.print(",irGain=");
  Serial.print(irGain);
  Serial.print(",busy=");
  Serial.print(digitalRead(PIN_ADC_BUSY));
  Serial.print(",frstdata=");
  Serial.println(digitalRead(PIN_ADC_FRSTD));
}

void printSingleAdcRead() {
  uint16_t ch[4];

  if (!adcReadChannels1to4(ch)) {
    Serial.println("ERR single ADC read failed");
    return;
  }

  Serial.print("SINGLE_RAW");
  for (int i = 0; i < 4; i++) {
    Serial.print(',');
    Serial.print(ch[i]);
  }
  Serial.println();

  Serial.print("SINGLE_VOLTS");
  for (int i = 0; i < 4; i++) {
    Serial.print(',');
    Serial.print(adcCodeToVolts(ch[i]), 6);
  }
  Serial.println();
}

void printAdcStreamLine() {
  Serial.print("DATA");
  Serial.print(',');
  Serial.print(sampleCounter);

  Serial.print(",H");
  for (int i = 0; i < 4; i++) {
    Serial.print(',');
    Serial.print(highdata[i]);
  }

  Serial.print(",L");
  for (int i = 0; i < 4; i++) {
    Serial.print(',');
    Serial.print(lowdata[i]);
  }

  Serial.println();
}

void adcSetOversampling(uint8_t os) {
  digitalWrite(PIN_ADC_OS0, (os & 0x01) ? HIGH : LOW);
  digitalWrite(PIN_ADC_OS1, (os & 0x02) ? HIGH : LOW);
  digitalWrite(PIN_ADC_OS2, (os & 0x04) ? HIGH : LOW);
}

void adcReset() {
  digitalWrite(PIN_ADC_RESET, LOW);
  delayMicroseconds(5);
  digitalWrite(PIN_ADC_RESET, HIGH);
  delayMicroseconds(5);
  digitalWrite(PIN_ADC_RESET, LOW);
  delayMicroseconds(20);
}

void adcStartConversion() {
  digitalWrite(PIN_ADC_CONVST, LOW);
  digitalWrite(PIN_ADC_CONVSTB, LOW);
  delayMicroseconds(1);

  digitalWrite(PIN_ADC_CONVST, HIGH);
  digitalWrite(PIN_ADC_CONVSTB, HIGH);
  delayMicroseconds(1);

  digitalWrite(PIN_ADC_CONVST, LOW);
  digitalWrite(PIN_ADC_CONVSTB, LOW);
}

bool adcWaitForBusyLow(uint32_t timeout_us) {
  uint32_t start = micros();

  while (digitalRead(PIN_ADC_BUSY) == HIGH) {
    if ((micros() - start) > timeout_us) {
      return false;
    }
  }

  return true;
}

uint16_t adcReadWordDOUTA() {
  uint16_t value = 0;
  value |= ((uint16_t)adcSPI.transfer(0x00)) << 8;
  value |= ((uint16_t)adcSPI.transfer(0x00));
  return value;
}

bool adcReadChannels1to4(uint16_t ch[4]) {
  adcStartConversion();
  delayMicroseconds(2);

  if (!adcWaitForBusyLow(5000)) {
    Serial.println("ERR timed out waiting for BUSY low");
    return false;
  }

  adcSPI.beginTransaction(adcSpiSettings);
  digitalWrite(PIN_ADC_CS, LOW);
  delayMicroseconds(1);

  ch[0] = adcReadWordDOUTA();
  ch[1] = adcReadWordDOUTA();
  ch[2] = adcReadWordDOUTA();
  ch[3] = adcReadWordDOUTA();

  digitalWrite(PIN_ADC_CS, HIGH);
  adcSPI.endTransaction();

  return true;
}

int16_t adcCodeToSigned(uint16_t raw) {
  return (int16_t)raw;
}

float adcCodeToVolts(uint16_t raw) {
  return ((float)(int16_t)raw / 32768.0f) * FULL_SCALE_VOLTS;
}
