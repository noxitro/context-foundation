// Kestrel sample engine (synthetic test fixture, not a real product).
#include "Audio/VoiceMixer.h"

#include <algorithm>
#include <cmath>

namespace kes::audio
{
    void VoiceMixer::SetTargetGain(VoiceId id, float gain, float seconds)
    {
        Voice& voice = m_voices[id];
        voice.targetGain = std::clamp(gain, 0.0f, 1.0f);
        // Spread the change over the requested time so the listener does
        // not hear a click when the volume jumps.
        const float samples = std::max(1.0f, seconds * static_cast<float>(m_sampleRate));
        voice.gainStep = (voice.targetGain - voice.currentGain) / samples;
    }

    void VoiceMixer::MixInto(float* output, size_t frameCount)
    {
        std::fill(output, output + frameCount * 2, 0.0f);
        for (Voice& voice : m_voices)
        {
            if (!voice.active)
            {
                continue;
            }
            for (size_t frame = 0; frame < frameCount; ++frame)
            {
                // Move the gain one step toward its target, then add this
                // voice's sample to both channels of the mix.
                if (std::fabs(voice.targetGain - voice.currentGain) > std::fabs(voice.gainStep))
                {
                    voice.currentGain += voice.gainStep;
                }
                else
                {
                    voice.currentGain = voice.targetGain;
                }
                const float sample = voice.source->ReadSample() * voice.currentGain;
                output[frame * 2 + 0] += sample * voice.panLeft;
                output[frame * 2 + 1] += sample * voice.panRight;
            }
        }
        ApplyLimiter(output, frameCount);
    }

    void VoiceMixer::RetireVoices(uint32_t frameIndex)
    {
        // A stopped voice keeps its decoder for a few frames, because the
        // audio thread may still be reading from it. Free it afterwards.
        for (Voice& voice : m_voices)
        {
            if (voice.stoppedAtFrame != 0 && frameIndex - voice.stoppedAtFrame > kRetireDelayFrames)
            {
                voice.source.reset();
                voice.active = false;
                voice.stoppedAtFrame = 0;
            }
        }
    }

    void VoiceMixer::ApplyLimiter(float* output, size_t frameCount)
    {
        for (size_t i = 0; i < frameCount * 2; ++i)
        {
            output[i] = std::clamp(output[i], -1.0f, 1.0f);
        }
    }
} // namespace kes::audio
