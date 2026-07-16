#!/bin/bash
# OverlayGen launcher (macOS) — double-click to render an FPS comparison overlay.
cd "$(dirname "$0")"
python3 render_overlay.py --interactive
echo
read -r -p "Press Enter to close..."
