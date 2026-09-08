#!/bin/sh
# Build+run one throwaway panel prototype: tools/panel_render/proto.sh pages.c
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
HERE="$ROOT/tools/panel_render"
. "$ROOT/tools/lib_zephyr.sh"
LVGL_DIR="${LVGL_DIR:-$(blink_zephyr_ws 2>/dev/null)/modules/lib/gui/lvgl}"
BUILD="${TMPDIR:-/tmp}/blink-proto"
mkdir -p "$BUILD"
find "$LVGL_DIR/src" -name '*.c' > "$BUILD/srcs.txt"
sed -e 's|#if 0 /\* Set this to "1" to enable content \*/|#if 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_16 .*|#define LV_FONT_MONTSERRAT_16 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_20 .*|#define LV_FONT_MONTSERRAT_20 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_28 .*|#define LV_FONT_MONTSERRAT_28 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_48 .*|#define LV_FONT_MONTSERRAT_48 1|' \
    "$LVGL_DIR/lv_conf_template.h" > "$BUILD/lv_conf.h"
cc -O1 -w -DLV_CONF_INCLUDE_SIMPLE=1 -I"$BUILD" -I"$HERE" -I"$ROOT/firmware/src" -I"$LVGL_DIR" -I"$LVGL_DIR/src" \
   "$HERE/$1" "$ROOT/firmware/src/usage_view.c" "$ROOT/firmware/src/fmt.c" @"$BUILD/srcs.txt" -o "$BUILD/proto"
shift
"$BUILD/proto" "$@"
