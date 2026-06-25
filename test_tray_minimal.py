"""Minimal test for pystray on Windows.

Run this directly with: uv run python test_tray_minimal.py
"""
from PIL import Image
import pystray

# Create a simple 64x64 blue image
img = Image.new("RGB", (64, 64), color=(0, 120, 200))

def on_settings(icon, item):
    print("Settings clicked!")

def on_exit(icon, item):
    print("Exit clicked!")
    icon.stop()

menu = pystray.Menu(
    pystray.MenuItem("Open Settings", on_settings),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("Exit", on_exit),
)

icon = pystray.Icon(
    name="PLT-Optimizer-Test",
    icon=img,
    title="PLT-Optimizer Test",
    menu=menu,
)

print("Starting system tray icon...")
icon.run()
print("Icon stopped")
