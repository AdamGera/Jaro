cmake_minimum_required(VERSION 3.22)
project(JaroRumble LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_C_VISIBILITY_PRESET hidden)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Build ggml into Jaro's library" FORCE)
set(GGML_METAL OFF CACHE BOOL "Use the CPU backend" FORCE)
set(GGML_BLAS OFF CACHE BOOL "Build without a separate BLAS dependency" FORCE)
set(GGML_OPENMP OFF CACHE BOOL "Build without an OpenMP dependency" FORCE)
set(GGML_BACKEND_DL OFF CACHE BOOL "Build without dynamic backend loading" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "Use target architecture flags" FORCE)
set(GGML_BUILD_EXAMPLES OFF CACHE BOOL "Build runtime libraries only" FORCE)
set(GGML_BUILD_TESTS OFF CACHE BOOL "Build runtime libraries only" FORCE)

if(ANDROID)
    add_compile_options(-ffunction-sections -fdata-sections)
endif()

add_subdirectory("${JARO_RUMBLE_SOURCE}/ggml" ggml)
set(RUMBLE_SOURCES
    jaro_rumble.cpp
    "${JARO_RUMBLE_SOURCE}/src/qwen3_asr.cpp"
    "${JARO_RUMBLE_SOURCE}/src/core/qwen3_ultra_sidecar.cpp"
    "${JARO_RUMBLE_SOURCE}/src/core/mel.cpp"
    "${JARO_RUMBLE_SOURCE}/src/core/gguf_loader.cpp"
    "${JARO_RUMBLE_SOURCE}/src/crispasr_imatrix.cpp"
    "${JARO_RUMBLE_SOURCE}/crisp_audio/src/audio_tower.cpp"
)

if(ANDROID)
    add_library(jaro_rumble SHARED ${RUMBLE_SOURCES})
    target_link_options(jaro_rumble PRIVATE
        "LINKER:--gc-sections"
        "LINKER:--exclude-libs,ALL"
        "LINKER:--version-script,${CMAKE_CURRENT_SOURCE_DIR}/exports.map"
        "LINKER:-z,max-page-size=16384"
        "LINKER:-z,common-page-size=16384"
    )
    set_property(TARGET jaro_rumble APPEND PROPERTY LINK_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/exports.map")
else()
    add_library(jaro_rumble STATIC ${RUMBLE_SOURCES})
endif()
target_include_directories(jaro_rumble PUBLIC include PRIVATE
    "${JARO_RUMBLE_SOURCE}/src"
    "${JARO_RUMBLE_SOURCE}/ggml/src"
    "${JARO_RUMBLE_SOURCE}/crisp_audio/include"
)
target_link_libraries(jaro_rumble PRIVATE ggml ggml-base)
