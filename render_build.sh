#!/usr/bin/env bash
# Render build script — set this as the Build Command: ./render_build.sh
# (and make it executable: chmod +x render_build.sh before committing)

# Stop on the first error so a broken build fails loudly instead of
# deploying a service with no browser installed.
set -e

pip install -r requirements.txt

# Chromium is not part of Render's image, so download it at build time.
# --with-deps also pulls the system libraries Chromium needs to run headless.
playwright install chromium --with-deps
