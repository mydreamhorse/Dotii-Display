#include "audio_output.h"
#include "audio_level.h"

#include <math.h>
#include <stdatomic.h>
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

#define SAMPLE_RATE 16000
#define TONE_SAMPLES (SAMPLE_RATE / 2)
#define BLOCK_SAMPLES 256
#define RECORD_RATE SAMPLE_RATE
#define RECORD_SAMPLES (RECORD_RATE * 5)

typedef struct {
    char id[17];
    uint8_t volume;
    int64_t deadline_us;
    bool record_playback;
} audio_test_t;

static const char *TAG = "audio_output";
static QueueHandle_t s_queue;
static char s_last_id[17];
static esp_codec_dev_handle_t s_speaker;
static esp_codec_dev_handle_t s_microphone;
static atomic_bool s_busy;

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
    if (result != ESP_CODEC_DEV_OK) {
        (void)esp_codec_dev_close(s_speaker);
        return result;
    }
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

static int record_and_play(const audio_test_t *test, audio_recording_metrics_t *metrics)
{
    /* The BSP enables MIC1/MIC2 in standard I2S. Select only the left MIC1
       slot, using the same mono DMA size as playback. Reallocating stereo
       DMA after the display starts can exhaust the remaining internal heap. */
    if (s_microphone == NULL) s_microphone = bsp_audio_codec_microphone_init();
    if (s_microphone == NULL) return ESP_CODEC_DEV_NOT_FOUND;
    int16_t *recording = heap_caps_malloc(RECORD_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (recording == NULL) return ESP_CODEC_DEV_NO_MEM;
    int result = ESP_CODEC_DEV_OK;
    bool microphone_open = false;
    bool speaker_open = false;
    esp_codec_dev_sample_info_t input = {.sample_rate = RECORD_RATE, .channel = 1, .bits_per_sample = 16};
    esp_codec_dev_sample_info_t output = {.sample_rate = RECORD_RATE, .channel = 1, .bits_per_sample = 16};
    int16_t discard[BLOCK_SAMPLES];
    /* Confirm delivery before recording, so a disconnected/expired command
       cannot silently capture audio after the management center has given up. */
    if (!connectivity_audio_report(test->id, "record_playback", "recording", 0, NULL)) {
        result = ESP_ERR_TIMEOUT;
        goto cleanup;
    }
    result = play_tone(test->volume);
    if (result != ESP_CODEC_DEV_OK) goto cleanup;
    microphone_open = true; /* open may partially enable the driver on failure */
    result = esp_codec_dev_open(s_microphone, &input);
    if (result != ESP_CODEC_DEV_OK) goto cleanup;
    result = esp_codec_dev_set_in_gain(s_microphone, 36.0f);
    if (result != ESP_CODEC_DEV_OK) goto cleanup;
    /* Drain old DMA samples and let the ADC settle after the cue. */
    for (int i = 0; i < 16; i++) {
        result = esp_codec_dev_read(s_microphone, discard, sizeof(discard));
        if (result != ESP_CODEC_DEV_OK) goto cleanup;
    }
    int64_t started = esp_timer_get_time();
    for (int offset = 0; offset < RECORD_SAMPLES; offset += BLOCK_SAMPLES) {
        if (esp_timer_get_time() >= test->deadline_us) { result = ESP_ERR_TIMEOUT; goto cleanup; }
        int count = RECORD_SAMPLES - offset;
        if (count > BLOCK_SAMPLES) count = BLOCK_SAMPLES;
        result = esp_codec_dev_read(s_microphone, recording + offset, count * sizeof(int16_t));
        if (result != ESP_CODEC_DEV_OK) goto cleanup;
        metrics->samples += count;
    }
    result = esp_codec_dev_close(s_microphone);
    microphone_open = false;
    if (result != ESP_CODEC_DEV_OK) goto cleanup;
    ESP_LOGI(TAG, "Recording %s: samples=%lu elapsed_ms=%lld", test->id,
             (unsigned long)metrics->samples, (esp_timer_get_time() - started) / 1000);
    int64_t sum = 0;
    for (int i = 0; i < RECORD_SAMPLES; i++) sum += recording[i];
    int mean = (int)(sum / RECORD_SAMPLES);
    uint64_t energy = 0;
    for (int i = 0; i < RECORD_SAMPLES; i++) {
        if (recording[i] >= 32760 || recording[i] <= -32760) metrics->clipped_samples++;
        int sample = (int)recording[i] - mean;
        if (sample > 32767) sample = 32767;
        if (sample < -32768) sample = -32768;
        recording[i] = sample;
        uint32_t magnitude = sample < 0 ? -sample : sample;
        if (magnitude > metrics->peak) metrics->peak = magnitude;
        energy += (int64_t)sample * sample;
    }
    metrics->rms = (uint32_t)sqrt((double)energy / RECORD_SAMPLES);
    ESP_LOGI(TAG, "Recording levels: peak=%lu rms=%lu clipped=%lu", (unsigned long)metrics->peak,
             (unsigned long)metrics->rms, (unsigned long)metrics->clipped_samples);
    if (metrics->peak == 0) { result = ESP_CODEC_DEV_READ_FAIL; goto cleanup; }
    /* Raise quiet speech without raising the cue or the user's volume.
       Keep the reported levels raw, before playback compensation. */
    float gain = audio_replay_gain(metrics->peak, metrics->rms);
    uint32_t playback_peak = 0;
    uint64_t playback_energy = 0;
    for (int i = 0; i < RECORD_SAMPLES; i++) {
        int edge = i < RECORD_SAMPLES - 1 - i ? i : RECORD_SAMPLES - 1 - i;
        float fade = edge < 160 ? (float)edge / 160.0f : 1.0f;
        int sample = audio_replay_sample(recording[i], gain, fade);
        recording[i] = sample;
        uint32_t magnitude = sample < 0 ? -sample : sample;
        if (magnitude > playback_peak) playback_peak = magnitude;
        playback_energy += (int64_t)sample * sample;
    }
    ESP_LOGI(TAG, "Playback levels: mic_gain_db=36 gain=%.2f peak=%lu rms=%.0f volume=%u",
             gain, (unsigned long)playback_peak, sqrt((double)playback_energy / RECORD_SAMPLES), test->volume);
    if (!connectivity_audio_report(test->id, "record_playback", "playing", 0, metrics)) {
        result = ESP_ERR_TIMEOUT;
        goto cleanup;
    }
    speaker_open = true;
    result = esp_codec_dev_open(s_speaker, &output);
    if (result != ESP_CODEC_DEV_OK) goto cleanup;
    result = esp_codec_dev_set_out_vol(s_speaker, test->volume);
    for (int offset = 0; result == ESP_CODEC_DEV_OK && offset < RECORD_SAMPLES; offset += BLOCK_SAMPLES) {
        if (esp_timer_get_time() >= test->deadline_us) { result = ESP_ERR_TIMEOUT; break; }
        int count = RECORD_SAMPLES - offset;
        if (count > BLOCK_SAMPLES) count = BLOCK_SAMPLES;
        result = esp_codec_dev_write(s_speaker, recording + offset, count * sizeof(int16_t));
    }
    vTaskDelay(pdMS_TO_TICKS(30));
cleanup:
    if (microphone_open) {
        int closed = esp_codec_dev_close(s_microphone);
        if (result == ESP_CODEC_DEV_OK) result = closed;
    }
    if (speaker_open) {
        int closed = esp_codec_dev_close(s_speaker);
        if (result == ESP_CODEC_DEV_OK) result = closed;
    }
    memset(recording, 0, RECORD_SAMPLES * sizeof(int16_t));
    heap_caps_free(recording);
    return result;
}

static void audio_task(void *argument)
{
    (void)argument;
    audio_test_t test;
    while (true) {
        if (xQueueReceive(s_queue, &test, portMAX_DELAY) != pdTRUE) continue;
        audio_recording_metrics_t metrics = {0};
        const int result = esp_timer_get_time() >= test.deadline_us ? ESP_ERR_TIMEOUT :
                           test.record_playback ? record_and_play(&test, &metrics) : play_tone(test.volume);
        ESP_LOGI(TAG, "Audio %s test %s: result=%d volume=%u", test.record_playback ? "record" : "tone", test.id, result, test.volume);
        for (int retry = 0; retry < 3; retry++) {
            if (connectivity_audio_report(test.id, test.record_playback ? "record_playback" : "tone",
                                          result == 0 ? "played" : "failed", result,
                                          test.record_playback && metrics.samples == RECORD_SAMPLES ? &metrics : NULL)) break;
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
        atomic_store(&s_busy, false);
    }
}

bool audio_output_test(const char *request_id, uint8_t volume, uint32_t ttl_ms, bool record_playback)
{
    if (request_id == NULL || strlen(request_id) != 16 || volume == 0 || volume > 50 || ttl_ms == 0 || ttl_ms > 30000) return false;
    for (int i = 0; i < 16; i++) {
        if (!((request_id[i] >= '0' && request_id[i] <= '9') || (request_id[i] >= 'a' && request_id[i] <= 'f'))) return false;
    }
    if (strcmp(s_last_id, request_id) == 0) return true;
    if (atomic_exchange(&s_busy, true)) return false;
    ESP_LOGI(TAG, "Speaker request %s, free internal heap=%u", request_id,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (s_queue == NULL) {
        s_queue = xQueueCreate(1, sizeof(audio_test_t));
        if (s_queue == NULL) {
            ESP_LOGE(TAG, "Speaker queue allocation failed");
            atomic_store(&s_busy, false);
            return false;
        }
        if (xTaskCreateWithCaps(audio_task, "speaker", 8192, NULL, 3, NULL,
                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
            ESP_LOGE(TAG, "Speaker task allocation failed");
            vQueueDelete(s_queue);
            s_queue = NULL;
            atomic_store(&s_busy, false);
            return false;
        }
    }
    audio_test_t test = {.volume = volume, .record_playback = record_playback,
                         .deadline_us = esp_timer_get_time() + (int64_t)ttl_ms * 1000};
    strlcpy(test.id, request_id, sizeof(test.id));
    if (xQueueSend(s_queue, &test, 0) != pdTRUE) { atomic_store(&s_busy, false); return false; }
    strlcpy(s_last_id, request_id, sizeof(s_last_id));
    return true;
}
