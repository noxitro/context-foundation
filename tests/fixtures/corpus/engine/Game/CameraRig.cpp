// Kestrel sample engine (synthetic test fixture, not a real product).
#include "Game/CameraRig.h"

#include <cmath>

namespace kes::game
{
    void CameraRig::SetFollowTarget(const Vec3& worldPosition)
    {
        m_target = worldPosition;
    }

    void CameraRig::Update(float deltaSeconds)
    {
        // Close a fixed fraction of the remaining distance every second,
        // independent of the frame rate, so the camera eases toward the
        // player instead of snapping.
        const float blend = 1.0f - std::exp(-m_followSharpness * deltaSeconds);
        m_position.x += (m_target.x - m_position.x) * blend;
        m_position.y += (m_target.y - m_position.y) * blend;
        m_position.z += (m_target.z - m_position.z) * blend;
        ClampToBounds();
    }

    void CameraRig::ClampToBounds()
    {
        m_position.x = std::fmin(std::fmax(m_position.x, m_boundsMin.x), m_boundsMax.x);
        m_position.z = std::fmin(std::fmax(m_position.z, m_boundsMin.z), m_boundsMax.z);
    }
} // namespace kes::game
