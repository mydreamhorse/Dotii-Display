#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>

#include "audio_level.h"

int main(void)
{
    assert(audio_replay_gain(0, 0) == 1.0f);
    /* Measured idle input, including the expected +6 dB mic gain. */
    assert(audio_replay_gain(102, 24) == 1.0f);
    assert(audio_replay_gain(204, 48) == 1.0f);
    /* Regression: the user's speech used to reach only RMS 764 at 4x. */
    float gain = audio_replay_gain(1399, 191);
    assert(191 * gain >= 4500 && 191 * gain <= 5001);
    /* The second real recording had RMS 350 and peak 3203: one peak must
       no longer constrain the entire recording to RMS 2185. */
    gain = audio_replay_gain(3203, 350);
    assert(350 * gain >= 4999 && 350 * gain <= 5001);
    assert(audio_replay_sample(3203, gain, 1) < 28000);
    assert(audio_replay_gain(32768, 20000) < 1);
    assert(audio_replay_gain(32768, 50) == 1);
    for (uint32_t peak = 1; peak <= 32768; peak++) {
        uint32_t levels[] = {0, 1, 59, 60, peak / 8, peak / 2, peak};
        for (unsigned i = 0; i < sizeof(levels) / sizeof(levels[0]); i++) {
            if (levels[i] > peak) continue;
            gain = audio_replay_gain(peak, levels[i]);
            assert(isfinite(gain) && gain > 0 && gain <= 24);
            if (levels[i] < 60 || peak < 300) assert(gain <= 1);
            int16_t positive = audio_replay_sample(peak > 32767 ? 32767 : peak, gain, 1);
            int16_t negative = audio_replay_sample(-(int)peak, gain, 1);
            assert(positive >= 0 && negative <= 0);
            assert(positive < 28000 && negative > -28000);
        }
    }
    /* Exercise every PCM value at maximum gain: no wrap, monotonic response,
       preserved polarity, and silence at faded boundaries. */
    int previous = -28000;
    for (int sample = -32768; sample <= 32767; sample++) {
        int output = audio_replay_sample(sample, 24, 1);
        assert(output >= previous && output > -28000 && output < 28000);
        assert(audio_replay_sample(sample, 24, 0) == 0);
        previous = output;
    }
    /* A synthetic utterance with pauses and one plosive, matching the real
       peak/RMS ratio, should approach the cue level after processing. */
    uint64_t input_energy = 0, output_energy = 0;
    for (int i = 0; i < 80000; i++) {
        int sample = i == 0 ? 3203 : i < 16000 ? (int)(1100 * sin(i * 0.1)) : 0;
        input_energy += (int64_t)sample * sample;
    }
    gain = audio_replay_gain(3203, (uint32_t)sqrt(input_energy / 80000.0));
    for (int i = 0; i < 80000; i++) {
        int sample = i == 0 ? 3203 : i < 16000 ? (int)(1100 * sin(i * 0.1)) : 0;
        int output = audio_replay_sample(sample, gain, 1);
        output_energy += (int64_t)output * output;
    }
    double output_rms = sqrt(output_energy / 80000.0);
    assert(output_rms > 4500 && output_rms < 5100);
    puts("audio level regression, silence, and PCM range checks passed");
    return 0;
}
