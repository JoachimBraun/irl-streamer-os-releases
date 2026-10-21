cmake_minimum_required(VERSION 3.22)
project(irl-srt-source VERSION 1.0.0 LANGUAGES C)

find_package(PkgConfig REQUIRED)
pkg_check_modules(FFMPEG REQUIRED IMPORTED_TARGET libavformat libavcodec libavutil libswresample libswscale)

# OBS 32.2.2 aus dem offiziellen .deb: Header unter /usr/local/include/obs, libobs unter /usr/local/lib/x86_64-linux-gnu
find_path(OBS_INCLUDE_DIR obs-module.h PATHS /usr/local/include/obs /usr/include/obs REQUIRED)
find_library(OBS_LIBRARY NAMES obs PATHS /usr/local/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu REQUIRED)

add_library(irl-srt-source MODULE irl-srt-source.c hwdec.c)
set_target_properties(irl-srt-source PROPERTIES PREFIX "" C_STANDARD 11)
target_compile_definitions(irl-srt-source PRIVATE IRL_SRT_SOURCE_VERSION="${PROJECT_VERSION}")
target_compile_options(irl-srt-source PRIVATE -O2 -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers)
target_include_directories(irl-srt-source PRIVATE ${OBS_INCLUDE_DIR})
target_link_libraries(irl-srt-source PRIVATE ${OBS_LIBRARY} PkgConfig::FFMPEG m pthread)
