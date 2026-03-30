# Release v1.0.0

## Features
- **High-Efficiency GPU Pipeline**: Implementation of a 3-stage transcoding fallback (Full GPU -> Hybrid GPU -> Full CPU).
- **Resolution Matching**: Automatic detection and matching of source resolution (4K, 1080p, etc.).
- **Dynamic Tagging**: Automated tag generation with fallback mechanisms for Bilibili uploads.
- **Unified Logging**: All logs are now centralized in the `/logs` directory for easier maintenance.
- **Improved Stability**: Added 10-second hard timeouts for external translation services and robust process cleanup.

## Cleanup & Optimization
- Removed the heavy `Antigravity.exe` (246MB) and obsolete `deno.exe`.
- Relocated `bun.exe` to `bin/` directory to keep the root project clean.
- Removed outdated debug scripts and temporary log files.
- Optimized `.gitignore` for production use.

## Installation
1. Run `setup.bat` to install dependencies.
2. Configure `config.yaml` with your Bilibili and YouTube settings.
3. Run `run_web.bat` to start the automation UI.
