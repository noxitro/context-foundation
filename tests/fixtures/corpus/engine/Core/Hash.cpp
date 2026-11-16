// Kestrel sample engine (synthetic test fixture, not a real product).
#include "Core/Hash.h"

namespace kes::core
{
    namespace
    {
        uint32_t g_crcTable[256];
        bool g_crcTableReady = false;

        void BuildCrcTable()
        {
            for (uint32_t n = 0; n < 256; ++n)
            {
                uint32_t value = n;
                for (int bit = 0; bit < 8; ++bit)
                {
                    value = (value & 1u) ? (0xEDB88320u ^ (value >> 1)) : (value >> 1);
                }
                g_crcTable[n] = value;
            }
            g_crcTableReady = true;
        }
    }

    //! CRC-32 (IEEE 802.3, reflected polynomial 0xEDB88320) of a byte range.
    //! Used to detect corrupted asset packs when they are mounted.
    uint32_t Crc32(const uint8_t* data, size_t size)
    {
        if (!g_crcTableReady)
        {
            BuildCrcTable();
        }
        uint32_t crc = 0xFFFFFFFFu;
        for (size_t i = 0; i < size; ++i)
        {
            crc = g_crcTable[(crc ^ data[i]) & 0xFFu] ^ (crc >> 8);
        }
        return crc ^ 0xFFFFFFFFu;
    }

    //! FNV-1a over a string, for cheap name lookups. Not for integrity.
    uint64_t HashName(const char* text)
    {
        uint64_t hash = 14695981039346656037ull;
        while (*text)
        {
            hash ^= static_cast<uint8_t>(*text++);
            hash *= 1099511628211ull;
        }
        return hash;
    }
} // namespace kes::core
