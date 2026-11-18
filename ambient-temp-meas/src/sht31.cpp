#include "sht31.hpp"

#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <system_error>
#include <thread>

namespace sensors {

namespace {

using namespace std::chrono_literals;

namespace cmd {
constexpr std::uint16_t kSoftReset = 0x30A2;
constexpr std::uint16_t kFetchData = 0xE000;
constexpr std::uint16_t kBreak = 0x3093;
constexpr std::uint16_t kReadStatus = 0xF32D;
constexpr std::uint16_t kClearStatus = 0x3041;
constexpr std::uint16_t kHeaterOn = 0x306D;
constexpr std::uint16_t kHeaterOff = 0x3066;
}  // namespace cmd

// Datasheet sanity check: CRC of 0xBEEF must be 0x92.
constexpr std::uint8_t kCrcTestVector[] = {0xBE, 0xEF};
static_assert(Sht31::crc8(kCrcTestVector, 2) == 0x92, "CRC-8 implementation is wrong");

struct SingleShotSpec {
    std::uint16_t command;
    std::chrono::milliseconds max_duration;  // datasheet max, rounded up
};

// Single shot, clock stretching DISABLED (BCM2711 handles stretching poorly).
constexpr SingleShotSpec singleShotSpec(Repeatability rep) {
    switch (rep) {
        case Repeatability::High:   return {0x2400, 16ms};
        case Repeatability::Medium: return {0x240B, 7ms};
        case Repeatability::Low:    return {0x2416, 5ms};
    }
    return {0x2400, 16ms};
}

// Rows: PeriodicRate, columns: Repeatability (High, Medium, Low).
constexpr std::uint16_t kPeriodicCommands[5][3] = {
    {0x2032, 0x2024, 0x202F},  // 0.5 mps
    {0x2130, 0x2126, 0x212D},  // 1 mps
    {0x2236, 0x2220, 0x222B},  // 2 mps
    {0x2334, 0x2322, 0x2329},  // 4 mps
    {0x2737, 0x2721, 0x272A},  // 10 mps
};

constexpr std::uint16_t toWord(std::uint8_t msb, std::uint8_t lsb) {
    return static_cast<std::uint16_t>((msb << 8) | lsb);
}

// i2c-bcm2835 reports a NACK as EREMOTEIO; other adapters may use EIO/ENXIO.
bool isNack(const std::system_error& e) {
    const int v = e.code().value();
    return v == EREMOTEIO || v == EIO || v == ENXIO;
}

void checkCrc(const std::uint8_t* word, const char* field) {
    const std::uint8_t expected = Sht31::crc8(word, 2);
    if (expected != word[2]) {
        char msg[96];
        std::snprintf(msg, sizeof(msg), "SHT31 CRC mismatch on %s: expected 0x%02X, got 0x%02X",
                      field, expected, word[2]);
        throw Sht31Error(msg);
    }
}

}  // namespace

Sht31::Sht31(const std::string& bus, std::uint8_t address) : dev_(bus, address) {}

Measurement Sht31::readSingleShot(Repeatability rep) {
    requireIdle("readSingleShot");
    const SingleShotSpec spec = singleShotSpec(rep);
    sendCommand(spec.command);
    std::this_thread::sleep_for(spec.max_duration);
    return readMeasurement();
}

void Sht31::startPeriodic(PeriodicRate rate, Repeatability rep) {
    requireIdle("startPeriodic");
    const auto r = static_cast<std::size_t>(rate);
    const auto q = static_cast<std::size_t>(rep);
    sendCommand(kPeriodicCommands[r][q]);
    periodic_ = true;
}

std::optional<Measurement> Sht31::fetchPeriodic() {
    if (!periodic_) {
        throw Sht31Error("fetchPeriodic: periodic mode is not active");
    }
    sendCommand(cmd::kFetchData);
    try {
        return readMeasurement();
    } catch (const std::system_error& e) {
        if (isNack(e)) {
            return std::nullopt;  // no new sample since last fetch
        }
        throw;
    }
}

void Sht31::stopPeriodic() {
    try {
        sendCommand(cmd::kBreak);
    } catch (const std::system_error& e) {
        // In idle mode the sensor may NACK Break; only an error if we
        // believe periodic mode is running.
        if (!(isNack(e) && !periodic_)) {
            throw;
        }
    }
    std::this_thread::sleep_for(1ms);
    periodic_ = false;
}

void Sht31::softReset() {
    requireIdle("softReset");
    sendCommand(cmd::kSoftReset);
    std::this_thread::sleep_for(2ms);  // datasheet max 1.5 ms
}

Status Sht31::readStatus() {
    requireIdle("readStatus");
    sendCommand(cmd::kReadStatus);
    std::array<std::uint8_t, 3> buf{};
    dev_.read(buf.data(), buf.size());
    checkCrc(buf.data(), "status");
    return Status{toWord(buf[0], buf[1])};
}

void Sht31::clearStatus() {
    requireIdle("clearStatus");
    sendCommand(cmd::kClearStatus);
    std::this_thread::sleep_for(1ms);
}

void Sht31::setHeater(bool on) {
    requireIdle("setHeater");
    sendCommand(on ? cmd::kHeaterOn : cmd::kHeaterOff);
    std::this_thread::sleep_for(1ms);
}

void Sht31::sendCommand(std::uint16_t command) {
    const std::array<std::uint8_t, 2> buf{
        static_cast<std::uint8_t>(command >> 8),
        static_cast<std::uint8_t>(command & 0xFF),
    };
    dev_.write(buf.data(), buf.size());
}

Measurement Sht31::readMeasurement() {
    std::array<std::uint8_t, 6> buf{};
    dev_.read(buf.data(), buf.size());
    checkCrc(&buf[0], "temperature");
    checkCrc(&buf[3], "humidity");
    return Measurement{
        rawToCelsius(toWord(buf[0], buf[1])),
        rawToHumidity(toWord(buf[3], buf[4])),
    };
}

void Sht31::requireIdle(const char* operation) const {
    if (periodic_) {
        throw Sht31Error(std::string(operation) +
                         ": not allowed while periodic mode is active; call stopPeriodic() first");
    }
}

}  // namespace sensors
