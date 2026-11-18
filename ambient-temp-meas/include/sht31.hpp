#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>

#include "i2c_device.hpp"

namespace sensors {

// Protocol-level failure: CRC mismatch, or a command issued in the wrong mode.
// Bus-level failures propagate as std::system_error from hw::I2cDevice.
class Sht31Error : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

// Enum order matters: used as a table index in sht31.cpp.
enum class Repeatability { High, Medium, Low };
enum class PeriodicRate { Mps0_5, Mps1, Mps2, Mps4, Mps10 };

struct Measurement {
    double temperature_c;
    double humidity_rh;
};

// Status register (datasheet table "Description of the status register").
struct Status {
    std::uint16_t raw;

    bool alertPending() const noexcept { return raw & (1u << 15); }
    bool heaterOn() const noexcept { return raw & (1u << 13); }
    bool humidityAlert() const noexcept { return raw & (1u << 11); }
    bool temperatureAlert() const noexcept { return raw & (1u << 10); }
    bool resetDetected() const noexcept { return raw & (1u << 4); }
    bool lastCommandFailed() const noexcept { return raw & (1u << 1); }
    bool writeChecksumFailed() const noexcept { return raw & (1u << 0); }
};

// Not internally thread-safe: one owner per instance.
class Sht31 {
public:
    static constexpr std::uint8_t kDefaultAddress = 0x44;    // ADDR pin low
    static constexpr std::uint8_t kAlternateAddress = 0x45;  // ADDR pin high

    explicit Sht31(const std::string& bus = "/dev/i2c-1",
                   std::uint8_t address = kDefaultAddress);

    // --- Single-shot mode (no clock stretching) ---
    Measurement readSingleShot(Repeatability rep = Repeatability::High);

    // --- Periodic mode ---
    void startPeriodic(PeriodicRate rate, Repeatability rep = Repeatability::High);
    // Returns std::nullopt when no new sample is ready (sensor NACKs the read).
    std::optional<Measurement> fetchPeriodic();
    // Sends Break. Safe to call when the mode is unknown (e.g. at startup after
    // a previous process left the sensor in periodic mode).
    void stopPeriodic();
    bool periodicActive() const noexcept { return periodic_; }

    // --- Utilities ---
    void softReset();
    Status readStatus();
    void clearStatus();
    void setHeater(bool on);

    // --- Helpers (public for testing) ---
    // CRC-8: polynomial 0x31, init 0xFF, no reflection, no final XOR.
    static constexpr std::uint8_t crc8(const std::uint8_t* data, std::size_t len) noexcept {
        std::uint8_t crc = 0xFF;
        for (std::size_t i = 0; i < len; ++i) {
            crc ^= data[i];
            for (int bit = 0; bit < 8; ++bit) {
                crc = (crc & 0x80u) ? static_cast<std::uint8_t>((crc << 1) ^ 0x31u)
                                    : static_cast<std::uint8_t>(crc << 1);
            }
        }
        return crc;
    }

    static constexpr double rawToCelsius(std::uint16_t raw) noexcept {
        return -45.0 + 175.0 * static_cast<double>(raw) / 65535.0;
    }

    static constexpr double rawToHumidity(std::uint16_t raw) noexcept {
        return 100.0 * static_cast<double>(raw) / 65535.0;
    }

private:
    void sendCommand(std::uint16_t command);
    Measurement readMeasurement();
    void requireIdle(const char* operation) const;

    hw::I2cDevice dev_;
    bool periodic_ = false;
};

}  // namespace sensors
