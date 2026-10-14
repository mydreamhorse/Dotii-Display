#pragma once

#include <math.h>
#include <stdint.h>

/* Use one gain for the whole recording to avoid pumping during pauses.
   The target is close to the cue's RMS; the separate soft limiter handles
   brief speech peaks without turning down every other sample. */
static inline float audio_replay_gain(uint32_t peak, uint32_t rms)
{
    if (peak == 0) return 1.0f;
    float gain = 1.0f;
    if (rms >= 60 && peak >= 300) gain = fminf(24.0f, 5000.0f / rms);
    return gain;
}

static inline int16_t audio_replay_sample(int16_t sample, float gain, float fade)
{
    float value = sample * gain;
    float magnitude = fabsf(value);
    /* Continuous value and slope at 18000, approaching 28000 without hard
       clipping or int16 overflow, even at the maximum permitted gain. */
    if (magnitude > 18000.0f) {
        float excess = magnitude - 18000.0f;
        magnitude = 18000.0f + 10000.0f * (excess / (10000.0f + excess));
    }
    return (int16_t)(copysignf(magnitude, value) * fade);
}
