cmake_minimum_required(VERSION 3.22)
project(jarospeech LANGUAGES C CXX)

include(FetchContent)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Link Whisper and ggml into the JNI library" FORCE)
set(WHISPER_BUILD_TESTS OFF CACHE BOOL "")
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "")
set(WHISPER_BUILD_SERVER OFF CACHE BOOL "")
set(GGML_NATIVE OFF CACHE BOOL "")
set(GGML_OPENMP OFF CACHE BOOL "")
set(GGML_LLAMAFILE OFF CACHE BOOL "")
set(GGML_CPU_ARM_ARCH armv8-a CACHE STRING "")
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
# Interactive decoding needs optimized kernels even in the debuggable APK.
add_compile_options($<$<CONFIG:Debug>:-O2>)
# Only the JNI entry points (JNIEXPORT) are exported; unused code, such as ggml's backend loader, is dropped.
set(CMAKE_C_VISIBILITY_PRESET hidden)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)
add_compile_options(-fvisibility=hidden -ffunction-sections -fdata-sections)
# A keyboard's process should not carry code that forks, executes programs, sets a ptracer or loads shared objects from
# a path: the patch keeps ggml_abort's backtrace in process (stock ggml forks gdb or lldb). A bump it no longer fits fails.
find_program(PATCH_EXECUTABLE patch REQUIRED)
FetchContent_Declare(whisper_cpp
    URL https://codeload.github.com/ggml-org/whisper.cpp/tar.gz/d1be6fde11ac6e0407606b4e42fe72d34add8037
    URL_HASH SHA256=645445da884df6dbd52b30f82d7be7c01d1767c938714e477e786b88d529e9f4
    TLS_VERIFY ON
    PATCH_COMMAND ${PATCH_EXECUTABLE} -p1 -N -t -F0 -i ${CMAKE_CURRENT_LIST_DIR}/ggml-no-debugger.patch)
FetchContent_MakeAvailable(whisper_cpp)

add_library(jarospeech SHARED jarospeech.cpp)
target_compile_features(jarospeech PRIVATE cxx_std_17)
target_link_libraries(jarospeech PRIVATE whisper android log)
target_link_options(jarospeech PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)
add_custom_command(TARGET jarospeech POST_BUILD VERBATIM
    COMMAND ${CMAKE_COMMAND} -DNM=${CMAKE_NM} -DLIB=$<TARGET_FILE:jarospeech> -P ${CMAKE_CURRENT_LIST_DIR}/check-imports.cmake)

# Build the optional Parakeet runtime separately because it vendors a different ggml.
include(ExternalProject)
set(JAROENGLISH_FLAGS "-fvisibility=hidden -ffunction-sections -fdata-sections")
ExternalProject_Add(transcribe_cpp
    URL https://codeload.github.com/anomalyco/transcribe.cpp/tar.gz/60bed89dab84f7c1fddc3a17ad7fa8a27ddfba19
    URL_HASH SHA256=45dbe23f9d061ba9182ff93770c61fa608cd53c8f3b6503c4efca1c9fd1045f6
    TLS_VERIFY ON
    PATCH_COMMAND ${PATCH_EXECUTABLE} -p1 -N -t -F0 -i ${CMAKE_CURRENT_LIST_DIR}/ggml-no-debugger.patch
        COMMAND ${PATCH_EXECUTABLE} -p1 -N -t -F0 -i ${CMAKE_CURRENT_LIST_DIR}/transcribe-parakeet-only.patch
    CMAKE_ARGS -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE} -DANDROID_ABI=${ANDROID_ABI}
        -DANDROID_PLATFORM=${ANDROID_PLATFORM} -DANDROID_STL=${ANDROID_STL}
        -DANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES=${ANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES}
        -DCMAKE_MAKE_PROGRAM=${CMAKE_MAKE_PROGRAM} -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        "-DCMAKE_C_FLAGS=${JAROENGLISH_FLAGS}" "-DCMAKE_CXX_FLAGS=${JAROENGLISH_FLAGS} -fvisibility-inlines-hidden"
        -DTRANSCRIBE_BUILD_TESTS=OFF -DTRANSCRIBE_BUILD_EXAMPLES=OFF -DTRANSCRIBE_BUILD_TOOLS=OFF -DTRANSCRIBE_METAL=OFF
        -DTRANSCRIBE_VULKAN=OFF -DTRANSCRIBE_USE_SYSTEM_BLAS=OFF -DTRANSCRIBE_USE_OPENMP=OFF -DTRANSCRIBE_BUILD_SHARED=OFF
        -DTRANSCRIBE_GGML_BACKEND_DL=OFF -DTRANSCRIBE_INSTALL=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF -DGGML_LLAMAFILE=OFF
        -DGGML_CPU_ARM_ARCH=armv8-a
    INSTALL_COMMAND ""
    BUILD_BYPRODUCTS <BINARY_DIR>/src/libtranscribe.a <BINARY_DIR>/ggml/src/libggml.a
        <BINARY_DIR>/ggml/src/libggml-cpu.a <BINARY_DIR>/ggml/src/libggml-base.a)
ExternalProject_Get_Property(transcribe_cpp SOURCE_DIR BINARY_DIR)
add_library(jaroenglish SHARED jaroenglish.cpp)
add_dependencies(jaroenglish transcribe_cpp)
target_compile_features(jaroenglish PRIVATE cxx_std_17)
target_include_directories(jaroenglish PRIVATE ${SOURCE_DIR}/include)
target_link_libraries(jaroenglish PRIVATE -Wl,--start-group ${BINARY_DIR}/src/libtranscribe.a ${BINARY_DIR}/ggml/src/libggml.a
    ${BINARY_DIR}/ggml/src/libggml-cpu.a ${BINARY_DIR}/ggml/src/libggml-base.a -Wl,--end-group android log)
target_link_options(jaroenglish PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)
add_custom_command(TARGET jaroenglish POST_BUILD VERBATIM
    COMMAND ${CMAKE_COMMAND} -DNM=${CMAKE_NM} -DLIB=$<TARGET_FILE:jaroenglish> -P ${CMAKE_CURRENT_LIST_DIR}/check-imports.cmake)

if(JARO_RUMBLE_LIBRARY)
    add_library(jaro_rumble SHARED IMPORTED)
    set_target_properties(jaro_rumble PROPERTIES IMPORTED_LOCATION "${JARO_RUMBLE_LIBRARY}")
    add_library(jarorumblejni SHARED jarorumblejni.cpp)
    target_compile_features(jarorumblejni PRIVATE cxx_std_17)
    target_include_directories(jarorumblejni PRIVATE "${JARO_RUMBLE_INCLUDE}")
    target_link_libraries(jarorumblejni PRIVATE jaro_rumble)
    target_link_options(jarorumblejni PRIVATE -Wl,--gc-sections -Wl,--exclude-libs,ALL)
    add_custom_command(TARGET jarorumblejni POST_BUILD VERBATIM
        COMMAND ${CMAKE_COMMAND} -DNM=${CMAKE_NM} -DLIB=$<TARGET_FILE:jarorumblejni> -P ${CMAKE_CURRENT_LIST_DIR}/check-imports.cmake
        COMMAND ${CMAKE_COMMAND} -DNM=${CMAKE_NM} -DLIB=${JARO_RUMBLE_LIBRARY} -P ${CMAKE_CURRENT_LIST_DIR}/check-imports.cmake)
endif()
