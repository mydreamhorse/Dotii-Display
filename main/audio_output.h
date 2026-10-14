#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

/* Reserve DMA resources before display initialization; never play on boot. */
esp_err_t audio_output_prepare(void);

/* Called only by the network task, after validating a fresh snapshot. */
bool audio_output_test(const char *request_id, uint8_t volume, uint32_t ttl_ms);
