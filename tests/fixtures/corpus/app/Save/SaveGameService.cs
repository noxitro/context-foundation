// Kestrel sample app (synthetic test fixture, not a real product).
using System;
using System.IO;
using System.Threading.Tasks;

namespace Kestrel.App.Save;

/// <summary>Writes and reads save slots on disk.</summary>
public sealed class SaveGameService
{
    private const uint Magic = 0x4B53_4156; // "KSAV"
    private const int FormatVersion = 3;
    private readonly string _root;

    public SaveGameService(string root)
    {
        _root = root;
        Directory.CreateDirectory(_root);
    }

    /// <summary>
    /// Saves a slot without ever leaving a half-written file behind: the
    /// data goes to a temporary file first and replaces the real one only
    /// after it is complete.
    /// </summary>
    public async Task SaveAsync(string slot, byte[] payload)
    {
        string finalPath = Path.Combine(_root, slot + ".sav");
        string tempPath = finalPath + ".tmp";
        await using (var stream = new FileStream(tempPath, FileMode.Create))
        await using (var writer = new BinaryWriter(stream))
        {
            writer.Write(Magic);
            writer.Write(FormatVersion);
            writer.Write(payload.Length);
            writer.Write(Checksum(payload));
            writer.Write(payload);
        }
        File.Move(tempPath, finalPath, overwrite: true);
    }

    /// <summary>Loads a slot, or returns null when it is damaged.</summary>
    public byte[]? Load(string slot)
    {
        string path = Path.Combine(_root, slot + ".sav");
        if (!File.Exists(path))
        {
            return null;
        }
        using var reader = new BinaryReader(File.OpenRead(path));
        if (reader.ReadUInt32() != Magic || reader.ReadInt32() > FormatVersion)
        {
            return null;
        }
        int length = reader.ReadInt32();
        uint expected = reader.ReadUInt32();
        byte[] payload = reader.ReadBytes(length);
        return Checksum(payload) == expected ? payload : null;
    }

    /// <summary>CRC-32 of the payload, stored in the header.</summary>
    public static uint Checksum(ReadOnlySpan<byte> data)
    {
        uint crc = 0xFFFFFFFFu;
        foreach (byte value in data)
        {
            crc ^= value;
            for (int bit = 0; bit < 8; bit++)
            {
                crc = (crc & 1u) != 0 ? 0xEDB88320u ^ (crc >> 1) : crc >> 1;
            }
        }
        return crc ^ 0xFFFFFFFFu;
    }
}
