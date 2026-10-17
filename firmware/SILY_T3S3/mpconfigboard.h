// SILy firmware for the LilyGo T3S3: the upstream LILYGO_T3_S3 board
// configuration (the esp32 port directory is on the include path), renamed so
// that sys.implementation._machine identifies the SILy firmware.
//
// Guarded: some port sources include mpconfigport.h twice, and the second
// pass would redefine the board name.
#ifndef SILY_T3S3_MPCONFIGBOARD_H
#define SILY_T3S3_MPCONFIGBOARD_H

#include "boards/LILYGO_T3_S3/mpconfigboard.h"

#undef MICROPY_HW_BOARD_NAME
#define MICROPY_HW_BOARD_NAME               "LILYGO T3-S3 (SILy)"

#endif
