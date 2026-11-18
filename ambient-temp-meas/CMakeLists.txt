cmake_minimum_required(VERSION 3.16)
project(sht31_driver LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_library(sht31 STATIC
    src/i2c_device.cpp
    src/sht31.cpp
)
target_include_directories(sht31 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_options(sht31 PRIVATE -Wall -Wextra -Wpedantic -Wconversion)

add_executable(sht31_example examples/main.cpp)
target_link_libraries(sht31_example PRIVATE sht31)
target_compile_options(sht31_example PRIVATE -Wall -Wextra -Wpedantic)

add_executable(sht31_logger apps/sht31_logger.cpp)
target_link_libraries(sht31_logger PRIVATE sht31)
target_compile_options(sht31_logger PRIVATE -Wall -Wextra -Wpedantic)
