// Usage: sht31_example [bus] [address]
//   sht31_example                      -> /dev/i2c-1, 0x44
//   sht31_example /dev/i2c-1 0x45

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <string>
#include <thread>

#include "sht31.hpp"

namespace {

volatile std::sig_atomic_t g_stop = 0;

void onSignal(int) { g_stop = 1; }

}  // namespace

int main(int argc, char** argv) {
    using namespace std::chrono_literals;

    const std::string bus = (argc > 1) ? argv[1] : "/dev/i2c-1";
    std::uint8_t address = sensors::Sht31::kDefaultAddress;

    try {
        if (argc > 2) {
            address = static_cast<std::uint8_t>(std::stoul(argv[2], nullptr, 0));
        }

        std::signal(SIGINT, onSignal);
        std::signal(SIGTERM, onSignal);

        sensors::Sht31 sht(bus, address);

        // Recover from a previous run that may have left the sensor in periodic mode.
        sht.stopPeriodic();
        sht.softReset();

        const sensors::Status st = sht.readStatus();
        std::printf("status=0x%04X  reset_detected=%d  heater=%d  alert=%d\n",
                    st.raw, st.resetDetected(), st.heaterOn(), st.alertPending());
        sht.clearStatus();

        while (!g_stop) {
            try {
                const sensors::Measurement m = sht.readSingleShot(sensors::Repeatability::High);
                std::printf("T = %6.2f °C   RH = %6.2f %%\n", m.temperature_c, m.humidity_rh);
                std::fflush(stdout);
            } catch (const sensors::Sht31Error& e) {
                // CRC errors are transient (noise); log and keep going.
                std::fprintf(stderr, "warning: %s\n", e.what());
            }
            std::this_thread::sleep_for(1s);
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return EXIT_FAILURE;
    }

    return EXIT_SUCCESS;
}
