// Kestrel sample engine (synthetic test fixture, not a real product).
#pragma once

#include "Core/EnumTraits.h"

namespace kes::core
{
    // The macro below declares an enum together with its name table. It
    // is not valid C++ on its own, which a syntax-aware chunker must cope
    // with.
    KES_ENUM_WITH_NAMES(BlendMode, uint8_t,
        (Opaque, 0),

        // Alpha-tested cutouts such as foliage and fences.
        (Masked, 1),

        // Sorted back to front and blended over the scene.
        (Translucent, 2),

        // Added on top of the scene, for glow and sparks.
        (Additive, 3),

        // Multiplies the scene colour, for stains and shadows.
        (Modulate, 4),

        // Drawn into the distortion buffer instead of the scene.
        (Refractive, 5),

        // Keeps the count of the entries above.
        (Count, 6))

    //! Returns the display name registered for a blend mode.
    inline const char* ToString(BlendMode mode)
    {
        return EnumTraits<BlendMode>::Name(mode);
    }
} // namespace kes::core
