#!/bin/sh
# Starts the installed Google Chrome as a native Apple-silicon (arm64) app, with the arguments
# Playwright passes. Used when the backend's Python is an Intel build running under Rosetta: its
# child processes would otherwise run Chrome under Rosetta too, where LinkedIn's pages are several
# times slower (see playwright_driver._launch_options).
exec /usr/bin/arch -arm64 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "$@"
