# YouTube-to-Bilibili Automation Pipeline DevLog

## v1.0 - The Foundation (Initial Prototype)
- **Core Functionality**: Base classes for YouTube scanning (`yt-dlp`), downloading, and Bilibili uploading (`biliup`).
- **Metadata Management**: Capturing titles, descriptions, and thumbnails.
- **Intro Prepending**: Basic logic to merge a fixed intro with downloaded videos.

## v1.5 - Stability & UI Evolution
- **Web UI**: Transitioned from CLI to a modern, glassmorphism-inspired Web Dashboard.
- **Cancellation System**: Implemented `_cancel_requested` global flags and `taskkill` logic to allow users to safely abort long-running downloads or transcodes.
- **Progress Visibility**: Added SSE (Server-Sent Events) to stream real-time progress bars to the browser.
- **Bug Fixes**:
    - Handled UTF-8 character issues on Windows.
    - Improved .webp to .jpg conversion for Bilibili cover compatibility.

## v1.8 - Version Control & State Persistence
- **Git Integration**: Initialized Git repository and synced with GitHub.
- **Selective Sync**: Implemented `.gitignore` to protect cookies and large media files.
- **Dynamic Meta**: Fixed UI bug where filesize labels didn't update when switching between 1080p and 4K.

## v2.0 - High-Performance Pipeline (Current)
- **Full GPU Hardware Acceleration**: 
    - Offloaded decoding, scaling, and encoding to NVENC/CUDA.
    - Switched from CPU `scale` to `scale_cuda` to eliminate memory transfer bottlenecks.
- **Resolution-Matching Architecture**: 
    - Removed 1080p caps. The pipeline now natively supports 4K-to-4K and 1080p-to-1080p processing.
- **Smart Metadata**:
    - **Dynamic Tagging**: Automatically extracts up to 10 relevant tags from YouTube video keywords.
    - **Original Content Logic**: Strictly enforces Bilibili "Original" status (`--copyright 1`) and automatically omits the source URL as per platform requirements.
- **UI & Logging Enhancements**:
    - **Persistent State**: User quality choices are now persisted to the server state immediately upon selection.
    - **Progress Parsing**: Enhanced regex-based parsing for `biliup` output to provide smooth percentage updates in the log panel.
    - **Throttled Logging**: Added 10% interval progress logs to keep the text panel clean but informative.

---
*Maintained by Antigravity (Advanced Agentic Coding)*
