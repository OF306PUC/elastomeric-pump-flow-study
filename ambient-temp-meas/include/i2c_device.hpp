#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace hw {

// RAII wrapper around a Linux i2c-dev bus bound to a single 7-bit slave address.
// Non-copyable, movable. All I/O errors are reported as std::system_error
// carrying the originating errno (e.g. EREMOTEIO on a NACK with i2c-bcm2835).
class I2cDevice {
public:
    I2cDevice(const std::string& bus_path, std::uint8_t address);
    ~I2cDevice();

    I2cDevice(const I2cDevice&) = delete;
    I2cDevice& operator=(const I2cDevice&) = delete;
    I2cDevice(I2cDevice&& other) noexcept;
    I2cDevice& operator=(I2cDevice&& other) noexcept;

    // Single I2C write transaction (START, addr+W, data..., STOP).
    void write(const std::uint8_t* data, std::size_t len);

    // Single I2C read transaction (START, addr+R, data..., STOP).
    void read(std::uint8_t* data, std::size_t len);

    std::uint8_t address() const noexcept { return address_; }
    const std::string& bus() const noexcept { return bus_; }

private:
    void close() noexcept;

    std::string bus_;
    std::uint8_t address_;
    int fd_ = -1;
};

}  // namespace hw
