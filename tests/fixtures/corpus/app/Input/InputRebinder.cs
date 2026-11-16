// Kestrel sample app (synthetic test fixture, not a real product).
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

namespace Kestrel.App.Input
{
    /// <summary>Lets the player change which key triggers an action.</summary>
    public class InputRebinder
    {
        private readonly Dictionary<string, string> _bindings = new();
        private readonly string _path;

        public InputRebinder(string settingsFolder)
        {
            _path = Path.Combine(settingsFolder, "bindings.json");
        }

        /// <summary>Reads the stored bindings once, when the app starts.</summary>
        public void LoadAtStartup()
        {
            if (!File.Exists(_path))
            {
                return;
            }
            var stored = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(_path));
            foreach (var pair in stored ?? new())
            {
                _bindings[pair.Key] = pair.Value;
            }
        }

        /// <summary>
        /// Assigns a key to an action. If another action already uses the
        /// key, the two swap so that no action is left without a key.
        /// </summary>
        public void Rebind(string action, string key)
        {
            string? previousKey = _bindings.GetValueOrDefault(action);
            foreach (var pair in _bindings)
            {
                if (pair.Value == key && pair.Key != action && previousKey != null)
                {
                    _bindings[pair.Key] = previousKey;
                    break;
                }
            }
            _bindings[action] = key;
            File.WriteAllText(_path, JsonSerializer.Serialize(_bindings));
        }

        public string? KeyFor(string action) => _bindings.GetValueOrDefault(action);
    }
}
