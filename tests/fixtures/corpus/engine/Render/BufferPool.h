// Kestrel sample engine (synthetic test fixture, not a real product).
#pragma once

#include <cstdint>
#include <vector>

namespace kes::render
{
    struct BufferDesc
    {
        uint32_t byteSize = 0;
        uint32_t usageFlags = 0;
    };

    struct Buffer
    {
        BufferDesc desc;
        uint64_t gpuHandle = 0;
        uint32_t retiredAtFrame = 0;
    };

    //! Hands out GPU buffers and takes them back when the caller is done.
    //! A released buffer is not destroyed. It waits in a free list and is
    //! handed out again to a later request of a compatible size.
    class KES_API BufferPool final
    {
    public:
        explicit BufferPool(uint32_t reuseDelayFrames);
        ~BufferPool();

        //! Returns a buffer that satisfies the description. Reuses a
        //! retired buffer when one is large enough, otherwise creates one.
        Buffer* Acquire(const BufferDesc& desc);

        //! Gives a buffer back. It becomes reusable after the delay.
        void Release(Buffer* buffer);

        //! Call once per frame. Destroys buffers that stayed unused.
        void CollectGarbage(uint32_t frameIndex);

        uint32_t LiveCount() const { return m_liveCount; }

    private:
        Buffer* FindReusable(const BufferDesc& desc);

        std::vector<Buffer*> m_free;
        uint32_t m_reuseDelayFrames = 3;
        uint32_t m_currentFrame = 0;
        uint32_t m_liveCount = 0;
    };
} // namespace kes::render
