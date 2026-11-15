# Rendering design notes

Synthetic test fixture for context-foundation. It describes a made-up
sample engine and is not documentation of a real product.

## Frame resources

Every frame the renderer needs short-lived GPU memory: vertex streams for
dynamic geometry, per-view constants and staging buffers for uploads.
Creating and destroying that memory each frame is too slow, so the
renderer hands the buffers back to a pool when a frame is done with them.

### Reuse delay

A buffer that was released is not handed out again straight away. The GPU
runs a few frames behind the CPU, and a command list that was recorded
earlier may still read the old contents. The pool therefore waits for
three frames before it reuses a released buffer. Three matches the number
of frames the swap chain keeps in flight on the target platforms, plus one
frame of margin for the capture tools.

### Idle trimming

Buffers that nobody asked for during two seconds of frames are destroyed,
so a level with a brief peak in memory use does not keep that memory for
the rest of the session.

## Pass ordering

Passes declare what they read and write. The frame scheduler sorts the
passes so that a pass runs after every pass that produces its inputs, and
drops passes whose outputs nothing reads.

## Blend modes

Materials choose one blend mode. Opaque and masked surfaces are drawn
front to back to benefit from early depth rejection. Translucent and
additive surfaces are drawn afterwards, sorted back to front.
