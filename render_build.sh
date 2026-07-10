#!/usr/bin/env bash
# Render build script — set this as the Build Command: ./render_build.sh
# (and make it executable: chmod +x render_build.sh before committing)

# Stop on the first error so a broken build fails loudly instead of
# deploying a service with no browser installed.
set -e

pip install -r requirements.txt

# Chromium is not part of Render's image, so download it at build time.
# Download into the project directory (not the default ~/.cache) so the
# browser persists from the build environment into the runtime instance.
# No --with-deps: it needs root (su), which Render's free tier forbids;
# Render's image already ships the system libraries Chromium needs.
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.playwright
playwright install chromium
