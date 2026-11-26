// SHT31 ambient logger: one CSV row per time window, built to be joined with
// the SLF3S flow logs on UTC time.
//
// Usage: sht31_logger --sensor-id TAMB1 --tanda T03 [options]   (--help for the list)
//
// One ambient record per tanda (block of simultaneous flow runs): the tanda is
// written to the header exactly as the flow CSVs carry it (`tanda : T03`), so the
// analysis picks the ambient file of a run by its tanda and then joins on time.
// Start the logger when the tanda is prepared, before the first pump is
// launched, and stop it after the last run of the tanda has finished.
//
// Correlation design:
//   - Timestamps are Unix epoch seconds (UTC), the same time base as the flow
//     CSV's UTC_Time column, so the two files join on time with no conversion.
//   - Windows are aligned to multiples of --window-s since the Unix epoch
//     (12:00:00, 12:01:00, ...), not to the logger's start. Every host running
//     this logger therefore produces the same window boundaries.
//   - The header records whether the system clock was NTP-synchronised. Without
//     it, the hosts' clocks drift apart and a time join is meaningless.
//   - With --tanda: one file per tanda, ambient_<sensor>_<tanda>.csv. Without
//     it (free logging): one file per UTC day. Every file is self-describing
//     (full header) and fsync'ed after every row: a power cut loses at most one
//     window, and a restart appends to the same file under a new header block.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <exception>
#include <filesystem>
#include <limits>
#include <memory>
#include <random>
#include <regex>
#include <string>
#include <vector>
#include <system_error>
#include <thread>

#include <sys/stat.h>
#include <sys/timex.h>
#include <unistd.h>

#include "sht31.hpp"

namespace {

constexpr int kFormatVersion = 1;
constexpr int kReinitAfterErrors = 30;  // consecutive failures before a sensor reset

volatile std::sig_atomic_t g_stop = 0;
void onSignal(int) { g_stop = 1; }

struct Options {
    std::string bus = "/dev/i2c-1";
    std::uint8_t address = sensors::Sht31::kDefaultAddress;
    std::string sensor_id;
    std::string location = "unspecified";
    std::string device_id = "UNKNOWN";
    std::string experiment = "none";
    std::string campaign = "none";
    std::string tanda;                                      // empty = free logging
    std::vector<std::pair<std::string, std::string>> meta;  // extra key=value fields
    std::string out_dir = "data";
    int interval_ms = 1000;
    int window_s = 60;
    double duration_h = 0.0;  // 0 = until stopped
    bool dry_run = false;
};

void usage(const char* prog) {
    std::printf(
        "Usage: %s --sensor-id ID [options]\n"
        "  --sensor-id ID      physical sensor label, e.g. TAMB1 (required)\n"
        "  --tanda TNN         tanda being recorded, e.g. T03: one file per tanda\n"
        "                      (omit for free logging, one file per UTC day)\n"
        "  --experiment ID     experiment id, as in the manifest (e.g. pump-flow)\n"
        "  --campaign ID       campaign id, as in the manifest (e.g. main)\n"
        "  --meta KEY=VALUE    extra header field, repeatable\n"
        "  --location TEXT     where it is mounted, e.g. junto_a_restrictores\n"
        "  --device-id ID      logging host, e.g. pi12\n"
        "  --bus PATH          I2C bus (default /dev/i2c-1)\n"
        "  --address ADDR      0x44 (ADDR low, default) or 0x45\n"
        "  --interval-ms N     time between readings (default 1000)\n"
        "  --window-s N        output window, must divide 86400 (default 60)\n"
        "  --out-dir DIR       output directory (default ./data)\n"
        "  --duration-h H      stop after H hours (default: run until stopped)\n"
        "  --dry-run           synthetic readings, no I2C (to test the logger)\n",
        prog);
}

Options parseArgs(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) throw std::invalid_argument(a + " needs a value");
            return argv[++i];
        };
        if (a == "--help" || a == "-h") { usage(argv[0]); std::exit(EXIT_SUCCESS); }
        else if (a == "--sensor-id")   o.sensor_id = next();
        else if (a == "--location")    o.location = next();
        else if (a == "--device-id")   o.device_id = next();
        else if (a == "--tanda")       o.tanda = next();
        else if (a == "--experiment")  o.experiment = next();
        else if (a == "--campaign")    o.campaign = next();
        else if (a == "--meta") {
            const std::string kv = next();
            const auto eq = kv.find('=');
            if (eq == std::string::npos || eq == 0 || eq + 1 == kv.size())
                throw std::invalid_argument("--meta expects KEY=VALUE, got " + kv);
            o.meta.emplace_back(kv.substr(0, eq), kv.substr(eq + 1));
        }
        else if (a == "--bus")         o.bus = next();
        else if (a == "--address")     o.address = static_cast<std::uint8_t>(std::stoul(next(), nullptr, 0));
        else if (a == "--interval-ms") o.interval_ms = std::stoi(next());
        else if (a == "--window-s")    o.window_s = std::stoi(next());
        else if (a == "--out-dir")     o.out_dir = next();
        else if (a == "--duration-h")  o.duration_h = std::stod(next());
        else if (a == "--dry-run")     o.dry_run = true;
        else throw std::invalid_argument("unknown option " + a);
    }
    if (o.sensor_id.empty()) throw std::invalid_argument("--sensor-id is required");
    // Same pattern as the manifest's [fields.patterns].tanda.
    if (!o.tanda.empty() && !std::regex_match(o.tanda, std::regex("T[0-9]{2}[a-z]?")))
        throw std::invalid_argument("--tanda must look like T01..T99 or T02b, got " + o.tanda);
    if (o.interval_ms < 100) throw std::invalid_argument("--interval-ms must be >= 100");
    if (o.window_s <= 0 || 86400 % o.window_s != 0)
        throw std::invalid_argument("--window-s must divide 86400 (e.g. 10, 30, 60, 300)");
    if (o.window_s * 1000 < 2 * o.interval_ms)
        throw std::invalid_argument("--window-s must hold at least 2 readings");
    return o;
}

double nowEpoch() {
    using namespace std::chrono;
    return duration<double>(system_clock::now().time_since_epoch()).count();
}

std::string isoUtc(double epoch, bool with_ms) {
    const auto secs = static_cast<std::time_t>(std::floor(epoch));
    std::tm tm{};
    gmtime_r(&secs, &tm);
    char buf[40];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tm);
    std::string s = buf;
    if (with_ms) {
        char ms[8];
        std::snprintf(ms, sizeof(ms), ".%03d", static_cast<int>((epoch - std::floor(epoch)) * 1000));
        s += ms;
    }
    return s + "Z";
}

std::string utcDate(double epoch) {
    const auto secs = static_cast<std::time_t>(std::floor(epoch));
    std::tm tm{};
    gmtime_r(&secs, &tm);
    char buf[16];
    std::strftime(buf, sizeof(buf), "%Y%m%d", &tm);
    return buf;
}

// Kernel view of NTP discipline: synchronised unless STA_UNSYNC is set.
bool clockSynchronised() {
    timex tx{};
    const int state = ntp_adjtime(&tx);
    return state != TIME_ERROR && !(tx.status & STA_UNSYNC);
}

std::string hostName() {
    char buf[256] = {};
    return gethostname(buf, sizeof(buf) - 1) == 0 ? buf : "unknown";
}

struct Stat {
    long n = 0;
    double sum = 0, sumsq = 0;
    double min = std::numeric_limits<double>::infinity();
    double max = -std::numeric_limits<double>::infinity();

    void add(double x) {
        ++n; sum += x; sumsq += x * x;
        min = std::min(min, x); max = std::max(max, x);
    }
    double mean() const { return n ? sum / static_cast<double>(n) : std::nan(""); }
    double sd() const {
        if (n < 2) return std::nan("");
        const double m = mean();
        const double v = (sumsq - static_cast<double>(n) * m * m) / static_cast<double>(n - 1);
        return std::sqrt(std::max(v, 0.0));
    }
};

struct Window {
    long long index = -1;  // floor(epoch / window_s)
    Stat temp, rh;
    double ts_sum = 0;     // timestamps of successful readings
    long errors = 0;
};

// Source of readings: the real sensor, or a synthetic one for --dry-run.
class Source {
public:
    explicit Source(const Options& o) : opt_(o) {
        if (!o.dry_run) {
            sht_ = std::make_unique<sensors::Sht31>(o.bus, o.address);
            reset();
        }
    }

    // Status register at start-up, as text for the header.
    std::string startStatus() const { return start_status_; }

    sensors::Measurement read() {
        if (!sht_) {
            const double t = nowEpoch();
            return {23.0 + 1.5 * std::sin(2 * M_PI * t / 86400.0) + noise_(rng_),
                    45.0 + 5.0 * std::cos(2 * M_PI * t / 86400.0) + 10 * noise_(rng_)};
        }
        return sht_->readSingleShot(sensors::Repeatability::High);
    }

    // Recover from a previous process that left periodic mode on, or from a
    // run of bus errors.
    void reset() {
        if (!sht_) return;
        sht_->stopPeriodic();
        sht_->softReset();
        const sensors::Status st = sht_->readStatus();
        char buf[24];
        std::snprintf(buf, sizeof(buf), "0x%04X", st.raw);
        start_status_ = buf;
        sht_->setHeater(false);
        sht_->clearStatus();
    }

private:
    const Options& opt_;
    std::unique_ptr<sensors::Sht31> sht_;
    std::string start_status_ = "dry-run";
    std::mt19937 rng_{12345};
    std::normal_distribution<double> noise_{0.0, 0.02};
};

class CsvWriter {
public:
    CsvWriter(const Options& o, std::string start_status)
        : opt_(o), start_status_(std::move(start_status)) {}
    // A signal (Ctrl+C, systemctl stop) is the normal end of a tanda: STOPPED.
    ~CsvWriter() { close(g_stop ? "STOPPED" : "COMPLETE"); }

    void write(const Window& w) {
        const double start = static_cast<double>(w.index) * opt_.window_s;
        // A tanda keeps one file however many days it lasts; free logging rotates daily.
        const std::string key = opt_.tanda.empty() ? utcDate(start) : opt_.tanda;
        if (!f_ || key != key_) {
            close("ROTATED");
            open(key, start);
        }
        const double ts = w.temp.n ? w.ts_sum / static_cast<double>(w.temp.n)
                                   : start + opt_.window_s / 2.0;
        std::fprintf(f_, "%lld,%.3f,%.3f,%.4f,%.3f,%.3f,%.2f,%.3f,%.2f,%.2f,%ld,%ld\n",
                     static_cast<long long>(start), ts,
                     w.temp.mean(), w.temp.sd(), w.temp.n ? w.temp.min : std::nan(""),
                     w.temp.n ? w.temp.max : std::nan(""),
                     w.rh.mean(), w.rh.sd(), w.rh.n ? w.rh.min : std::nan(""),
                     w.rh.n ? w.rh.max : std::nan(""), w.temp.n, w.errors);
        ++rows_;
        std::fflush(f_);
        fsync(fileno(f_));
    }

    void close(const char* status) {
        if (!f_) return;
        std::fprintf(f_, "# END rows=%ld status=%s\n", rows_, status);
        std::fflush(f_);
        fsync(fileno(f_));
        std::fclose(f_);
        f_ = nullptr;
    }

private:
    void open(const std::string& key, double first_window) {
        std::error_code ec;
        std::filesystem::create_directories(opt_.out_dir, ec);  // whole path, e.g. data/offset
        if (ec) throw std::system_error(ec, "create " + opt_.out_dir);
        const std::string path = opt_.out_dir + "/ambient_" + opt_.sensor_id + "_" + key + ".csv";
        const bool exists = access(path.c_str(), F_OK) == 0;
        f_ = std::fopen(path.c_str(), "a");
        if (!f_) throw std::system_error(errno, std::generic_category(), "open " + path);
        key_ = key;
        rows_ = 0;
        // Appending to an existing day file (logger restarted): a new header
        // block marks the restart; readers skip every '#' line.
        const bool synced = clockSynchronised();
        if (!synced) {
            std::fprintf(stderr, "warning: system clock is NOT NTP-synchronised — "
                                 "timestamps cannot be trusted for correlation\n");
        }
        char addr[8];
        std::snprintf(addr, sizeof(addr), "0x%02X", opt_.address);
        std::fprintf(f_,
            "# ── Ambient metadata ──────────────────────────────────────────\n"
            "# format_version     : %d\n"
            "# kind               : ambient\n"
            "# experiment         : %s\n"
            "# campaign           : %s\n"
            "# tanda              : %s\n"
            "# sensor_model       : SHT31\n"
            "# sensor_id          : %s\n"
            "# location           : %s\n"
            "# device_id          : %s\n"
            "# hostname           : %s\n"
            "# i2c_bus            : %s\n"
            "# i2c_address        : %s\n"
            "# repeatability      : high\n"
            "# heater             : off\n"
            "# interval_ms        : %d\n"
            "# window_s           : %d\n"
            "# window_alignment   : utc_epoch\n"
            "# file_opened_utc    : %s\n"
            "# first_window_utc   : %s\n"
            "# clock_synchronized : %s\n"
            "# status_at_start    : %s\n"
            "# appended           : %s\n"
            "# dry_run            : %s\n",
            kFormatVersion, opt_.experiment.c_str(), opt_.campaign.c_str(),
            opt_.tanda.empty() ? "none" : opt_.tanda.c_str(), opt_.sensor_id.c_str(), opt_.location.c_str(),
            opt_.device_id.c_str(), hostName().c_str(), opt_.bus.c_str(), addr,
            opt_.interval_ms, opt_.window_s, isoUtc(nowEpoch(), true).c_str(),
            isoUtc(first_window, false).c_str(), synced ? "yes" : "no",
            start_status_.c_str(), exists ? "yes" : "no", opt_.dry_run ? "yes" : "no");
        if (!opt_.meta.empty()) {
            std::fprintf(f_, "# ── Extra fields ─────────────────────────────────────────────\n");
            for (const auto& [k, v] : opt_.meta) std::fprintf(f_, "# %-18s : %s\n", k.c_str(), v.c_str());
        }
        std::fprintf(f_, "# ─────────────────────────────────────────────────────────────\n");
        if (!exists) {
            std::fprintf(f_, "window_start,UTC_Time,Temp_degC,Temp_std_degC,Temp_min_degC,"
                             "Temp_max_degC,RH_pct,RH_std_pct,RH_min_pct,RH_max_pct,"
                             "N_samples,N_errors\n");
        }
        std::fflush(f_);
        std::fprintf(stderr, "writing %s\n", path.c_str());
    }

    const Options& opt_;
    std::string start_status_;
    std::FILE* f_ = nullptr;
    std::string key_;
    long rows_ = 0;
};

}  // namespace

int main(int argc, char** argv) {
    try {
        const Options opt = parseArgs(argc, argv);
        std::signal(SIGINT, onSignal);
        std::signal(SIGTERM, onSignal);

        Source src(opt);
        CsvWriter out(opt, src.startStatus());

        const auto period = std::chrono::milliseconds(opt.interval_ms);
        const double t_end = opt.duration_h > 0 ? nowEpoch() + opt.duration_h * 3600 : 0;
        auto deadline = std::chrono::steady_clock::now();
        Window w;
        int consecutive_errors = 0;

        while (!g_stop) {
            const double t = nowEpoch();
            if (t_end && t >= t_end) break;
            const long long idx = static_cast<long long>(std::floor(t / opt.window_s));
            if (w.index != idx) {
                if (w.index >= 0) out.write(w);
                w = Window{};
                w.index = idx;
            }
            try {
                const sensors::Measurement m = src.read();
                w.temp.add(m.temperature_c);
                w.rh.add(m.humidity_rh);
                w.ts_sum += t;
                consecutive_errors = 0;
            } catch (const std::exception& e) {
                ++w.errors;
                std::fprintf(stderr, "warning: %s\n", e.what());
                if (++consecutive_errors % kReinitAfterErrors == 0) {
                    try { src.reset(); } catch (const std::exception& e2) {
                        std::fprintf(stderr, "warning: sensor reset failed: %s\n", e2.what());
                    }
                }
            }
            deadline += period;
            const auto now = std::chrono::steady_clock::now();
            if (deadline > now) std::this_thread::sleep_until(deadline);
            else deadline = now;  // fell behind: resynchronise instead of bursting
        }
        // The last window is partial; keep it — N_samples says how partial.
        if (w.index >= 0 && (w.temp.n || w.errors)) out.write(w);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
