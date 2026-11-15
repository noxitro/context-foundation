// Kestrel sample engine (synthetic test fixture, not a real product).
#include "Render/BufferPool.h"

#include "Render/Device.h"

namespace kes::render
{
    namespace
    {
        // A buffer that sat unused this long is destroyed for real.
        constexpr uint32_t kMaxIdleFrames = 120;
    }

    BufferPool::BufferPool(uint32_t reuseDelayFrames)
        : m_reuseDelayFrames(reuseDelayFrames)
    {
    }

    BufferPool::~BufferPool()
    {
        for (Buffer* buffer : m_free)
        {
            Device::Get().DestroyBuffer(buffer->gpuHandle);
            delete buffer;
        }
    }

    Buffer* BufferPool::Acquire(const BufferDesc& desc)
    {
        // Prefer a retired buffer over a fresh allocation: creating GPU
        // memory is slow and fragments the heap.
        if (Buffer* reused = FindReusable(desc))
        {
            ++m_liveCount;
            return reused;
        }
        Buffer* created = new Buffer();
        created->desc = desc;
        created->gpuHandle = Device::Get().CreateBuffer(desc.byteSize, desc.usageFlags);
        ++m_liveCount;
        return created;
    }

    void BufferPool::Release(Buffer* buffer)
    {
        // The GPU may still read the buffer for the frames in flight, so
        // it only becomes reusable after m_reuseDelayFrames.
        buffer->retiredAtFrame = m_currentFrame;
        m_free.push_back(buffer);
        --m_liveCount;
    }

    Buffer* BufferPool::FindReusable(const BufferDesc& desc)
    {
        for (size_t i = 0; i < m_free.size(); ++i)
        {
            Buffer* candidate = m_free[i];
            const bool rested = m_currentFrame - candidate->retiredAtFrame >= m_reuseDelayFrames;
            const bool fits = candidate->desc.byteSize >= desc.byteSize
                && candidate->desc.usageFlags == desc.usageFlags;
            if (rested && fits)
            {
                m_free[i] = m_free.back();
                m_free.pop_back();
                return candidate;
            }
        }
        return nullptr;
    }

    void BufferPool::CollectGarbage(uint32_t frameIndex)
    {
        m_currentFrame = frameIndex;
        for (size_t i = 0; i < m_free.size();)
        {
            Buffer* buffer = m_free[i];
            if (frameIndex - buffer->retiredAtFrame > kMaxIdleFrames)
            {
                Device::Get().DestroyBuffer(buffer->gpuHandle);
                delete buffer;
                m_free[i] = m_free.back();
                m_free.pop_back();
            }
            else
            {
                ++i;
            }
        }
    }
} // namespace kes::render
