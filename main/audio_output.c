#include "audio_output.h"

#include <math.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "connectivity.h"
#include "esp_codec_dev.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#define SAMPLE_RATE 22050
#define TONE_SAMPLES (SAMPLE_RATE / 2)
#define BLOCK_SAMPLES 256

typedef struct {
    char id[17];
    uint8_t volume;
    int64_t deadline_us;
} audio_test_t;

static const char *TAG = "audio_output";
static QueueHandle_t s_queue;
static char s_last_id[17];
static esp_codec_dev_handle_t s_speaker;

esp_err_t audio_output_prepare(void)
{
    if (s_speaker != NULL) return ESP_OK;
    s_speaker = bsp_audio_codec_speaker_init();
    if (s_speaker == NULL) return ESP_ERR_NOT_FOUND;
    /* The codec default maps 15 to -42.5 dB, which is nearly inaudible for
       this short, quarter-scale tone. Map the UI percentage to amplitude
       (20*log10(volume/100)); retain the 50% cap and explicit zero mute. */
    esp_codec_dev_vol_map_t map[] = {
        {.vol = 0, .db_value = -96.0f}, {.vol = 1, .db_value = -40.0f},
        {.vol = 5, .db_value = -26.02f}, {.vol = 15, .db_value = -16.48f},
        {.vol = 30, .db_value = -10.46f}, {.vol = 50, .db_value = -6.02f},
    };
    esp_codec_dev_vol_curve_t curve = {.vol_map = map, .count = sizeof(map) / sizeof(map[0])};
    if (esp_codec_dev_set_vol_curve(s_speaker, &curve) != ESP_CODEC_DEV_OK) return ESP_FAIL;
    return esp_codec_dev_set_out_vol(s_speaker, 0) == ESP_CODEC_DEV_OK ? ESP_OK : ESP_FAIL;
}

static int play_tone(uint8_t volume)
{
    if (s_speaker == NULL) return ESP_CODEC_DEV_NOT_FOUND;
    esp_codec_dev_sample_info_t format = {
        .sample_rate = SAMPLE_RATE, .channel = 1, .bits_per_sample = 16,
    };
    int result = esp_codec_dev_open(s_speaker, &format);
    if (result != ESP_CODEC_DEV_OK) return result;
    result = esp_codec_dev_set_out_vol(s_speaker, volume);
    int16_t samples[BLOCK_SAMPLES];
    for (int offset = 0; result == ESP_CODEC_DEV_OK && offset < TONE_SAMPLES; offset += BLOCK_SAMPLES) {
        int count = TONE_SAMPLES - offset;
        if (count > BLOCK_SAMPLES) count = BLOCK_SAMPLES;
        for (int i = 0; i < count; i++) {
            int position = offset + i;
            int edge = position < TONE_SAMPLES - 1 - position ? position : TONE_SAMPLES - 1 - position;
            float envelope = edge < 220 ? (float)edge / 220.0f : 1.0f;
            samples[i] = (int16_t)(8000.0f * envelope * sinf(2.0f * 3.14159265358979323846f * 880.0f * position / SAMPLE_RATE));
        }
        result = esp_codec_dev_write(s_speaker, samples, count * (int)sizeof(samples[0]));
    }
    /* Let the final DMA block drain before disabling the amplifier. */
    vTaskDelay(pdMS_TO_TICKS(30));
    int close_result = esp_codec_dev_close(s_speaker);
    return result == ESP_CODEC_DEV_OK ? close_result : result;
}

static void audio_task(void *argument)
{
    (void)argument;
    audio_test_t test;
    while (true) {
        if (xQueueReceive(s_queue, &test, portMAX_DELAY) != pdTRUE) continue;
        const int result = esp_timer_get_time() < test.deadline_us ? play_tone(test.volume) : ESP_CODEC_DEV_INVALID_ARG;
        ESP_LOGI(TAG, "Speaker test %s: result=%d volume=%u", test.id, result, test.volume);
        for (int retry = 0; retry < 3; retry++) {
            if (connectivity_audio_result(test.id, result)) break;
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
}

bool audio_output_test(const char *request_id, uint8_t volume, uint32_t ttl_ms)
{
    if (request_id == NULL || strlen(request_id) != 16 || volume == 0 || volume > 50 || ttl_ms == 0 || ttl_ms > 30000) return false;
    for (int i = 0; i < 16; i++) {
        if (!((request_id[i] >= '0' && request_id[i] <= '9') || (request_id[i] >= 'a' && request_id[i] <= 'f'))) return false;
    }
    if (strcmp(s_last_id, request_id) == 0) return true;
    ESP_LOGI(TAG, "Speaker request %s, free internal heap=%u", request_id,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (s_queue == NULL) {
        s_queue = xQueueCreate(1, sizeof(audio_test_t));
        if (s_queue == NULL) {
            ESP_LOGE(TAG, "Speaker queue allocation failed");
            return false;
        }
        if (xTaskCreateWithCaps(audio_task, "speaker", 6144, NULL, 3, NULL,
                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
            ESP_LOGE(TAG, "Speaker task allocation failed");
            vQueueDelete(s_queue);
            s_queue = NULL;
            return false;
        }
    }
    audio_test_t test = {.volume = volume, .deadline_us = esp_timer_get_time() + (int64_t)ttl_ms * 1000};
    strlcpy(test.id, request_id, sizeof(test.id));
    if (xQueueSend(s_queue, &test, 0) != pdTRUE) return false;
    strlcpy(s_last_id, request_id, sizeof(s_last_id));
    return true;
}
