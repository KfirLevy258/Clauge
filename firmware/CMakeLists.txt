cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(claude_usage_display)

# src/ on the global include path so the mbedTLS build can find
# src/mbedtls_user_config.h (CONFIG_MBEDTLS_USER_CONFIG_FILE).
zephyr_include_directories(src)

target_sources(app PRIVATE
	src/main.c
	src/proto.c
	src/msg_parse.c
	src/usage_view.c
	src/fmt.c
	src/cfg_store.c
	src/net_time.c
	src/ui_boot.c
	src/bootanim_dec.c
	src/bootclip.c
	src/ui_sleep.c
	src/sleep_gate.c
	src/upd_row.c
	src/logo_parse.c
	src/logo.c
	src/ui_settings.c
	src/ui_anim.c
	src/ui_slide.c
	src/ui_swipe.c
	src/backlight.c
	src/ota_parse.c
	src/ota.c
	src/ui_touchfx.c
	src/usage_state.c
	src/usage_freshness.c
)

# Standalone WiFi mode only. Not stubbed and not dead-stripped -- with
# CONFIG_BLINK_WIFI_MODE off these are never handed to the compiler, so the
# OAuth client id, the spoofed User-Agent and the token writer are absent from
# the image rather than merely unreachable in it. See firmware/Kconfig.
target_sources_ifdef(CONFIG_BLINK_WIFI_MODE app PRIVATE
	src/net_wifi.c
	src/portal.c
	src/ui_setup.c
	src/dns_hijack.c
	src/oauth.c
	src/usage_parse.c
	src/usage_client.c
	src/tz_fetch.c
)

# Diagnostic only; CONFIG_BLINK_TOUCH_TRACE is off in release builds.
target_sources_ifdef(CONFIG_BLINK_TOUCH_TRACE app PRIVATE src/touch_trace.c)

