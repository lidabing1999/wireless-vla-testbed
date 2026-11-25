include_guard(GLOBAL)

function(enable_vrtsim_vectorization)
  if(TARGET vrtsim AND CMAKE_C_COMPILER_ID STREQUAL "GNU")
    # The uplink IQ sum clips each UE's contribution to int16. GCC's default
    # -O2 cost model leaves this variable-length loop scalar on the test host.
    set_property(SOURCE "${CMAKE_SOURCE_DIR}/radio/vrtsim/vrtsim.c"
                 TARGET_DIRECTORY vrtsim APPEND PROPERTY COMPILE_OPTIONS
                 -fvect-cost-model=dynamic)
  endif()
endfunction()

cmake_language(DEFER CALL enable_vrtsim_vectorization)
