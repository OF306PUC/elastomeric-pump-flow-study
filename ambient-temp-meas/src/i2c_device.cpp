#include "i2c_device.hpp"

#include <cerrno>
#include <cstdio>
#include <stdexcept>
#include <system_error>
#include <utility>

#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace hw {

namespace {

std::string describe(const std::string& bus, std::uint8_t address, const char* op) {
    char addr[8];
    std::snprintf(addr, sizeof(addr), "0x%02X", address);
    return std::string(op) + " (" + bus + ", addr " + addr + ")";
}

}  // namespace

I2cDevice::I2cDevice(const std::string& bus_path, std::uint8_t address)
    : bus_(bus_path), address_(address) {
    if (address_ > 0x7F) {
        throw std::invalid_argument("I2C address must be 7-bit (<= 0x7F)");
    }

    fd_ = ::open(bus_.c_str(), O_RDWR | O_CLOEXEC);
    if (fd_ < 0) {
        throw std::system_error(errno, std::generic_category(),
                                describe(bus_, address_, "open"));
    }

    if (::ioctl(fd_, I2C_SLAVE, static_cast<long>(address_)) < 0) {
        const int err = errno;
        close();
        throw std::system_error(err, std::generic_category(),
                                describe(bus_, address_, "ioctl(I2C_SLAVE)"));
    }
}

I2cDevice::~I2cDevice() { close(); }

I2cDevice::I2cDevice(I2cDevice&& other) noexcept
    : bus_(std::move(other.bus_)),
      address_(other.address_),
      fd_(std::exchange(other.fd_, -1)) {}

I2cDevice& I2cDevice::operator=(I2cDevice&& other) noexcept {
    if (this != &other) {
        close();
        bus_ = std::move(other.bus_);
        address_ = other.address_;
        fd_ = std::exchange(other.fd_, -1);
    }
    return *this;
}

void I2cDevice::write(const std::uint8_t* data, std::size_t len) {
    ssize_t n;
    do {
        n = ::write(fd_, data, len);
    } while (n < 0 && errno == EINTR);

    if (n < 0) {
        throw std::system_error(errno, std::generic_category(),
                                describe(bus_, address_, "write"));
    }
    if (static_cast<std::size_t>(n) != len) {
        throw std::system_error(EIO, std::generic_category(),
                                describe(bus_, address_, "short write"));
    }
}

void I2cDevice::read(std::uint8_t* data, std::size_t len) {
    ssize_t n;
    do {
        n = ::read(fd_, data, len);
    } while (n < 0 && errno == EINTR);

    if (n < 0) {
        throw std::system_error(errno, std::generic_category(),
                                describe(bus_, address_, "read"));
    }
    if (static_cast<std::size_t>(n) != len) {
        throw std::system_error(EIO, std::generic_category(),
                                describe(bus_, address_, "short read"));
    }
}

void I2cDevice::close() noexcept {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

}  // namespace hw
