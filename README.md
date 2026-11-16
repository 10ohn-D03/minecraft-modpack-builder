# Minecraft Modpack Builder

This repository generates a **Prism Launcher-importable `.mrpack`** targeting **Minecraft 1.21.1 + NeoForge** from the original mod wishlist in `input/mods-original.txt`.

## Build the pack

1. Open this repository on GitHub.
2. Select **Actions**.
3. Choose **Build Prism modpack**.
4. Select **Run workflow** and confirm.
5. When the run finishes, open it and download the artifact named `prism-modpack-1.21.1-neoforge`.
6. Extract the artifact ZIP, then import `Adventure_Modpack_1.21.1_NeoForge.mrpack` into Prism Launcher.

The workflow also runs automatically when files in `input/`, `scripts/`, or the workflow itself change on `main`.

## What the build does

- Ignores version and loader fragments in the supplied filenames when identifying requested mods.
- Searches Modrinth for the requested project and then verifies an actual **1.21.1 NeoForge** version through the Modrinth versions API.
- Prefers stable releases; uses beta/alpha only when no stable release is available.
- Adds Modrinth-declared **required** dependencies recursively when they also have a 1.21.1 NeoForge version.
- Adds a low-end performance layer where compatible: Sodium, ModernFix, FerriteCore, ImmediatelyFast, Entity Culling, More Culling, Dynamic FPS, and Lithium.
- Detects duplicate project IDs and avoids adding a project twice.
- Selects the current stable 21.1.x NeoForge version from official NeoForge Maven metadata.
- Builds a Modrinth `.mrpack` index with official download URLs and SHA-1/SHA-512 hashes for Prism to retrieve at import time.
- Emits CSV reports for resolved projects, unresolved requests, dependencies, and declared incompatibilities.

## Important limitations

This first workflow resolves **Modrinth-indexed projects only**. It does not scrape CurseForge or automatically install CurseForge-only projects. Those will appear in `dist/unresolved-mods.csv` and must be reviewed before the pack can be considered complete. It deliberately avoids guessing when a project search result is ambiguous. Mod compatibility is established by published version metadata, not by launching and playtesting the assembled pack.

Some mods may require manual review even when a release exists, especially large world-generation or content mods. Review `BUILD-SUMMARY.md`, `unresolved-mods.csv`, `dependencies.csv`, and `conflicts.csv` before using the pack in a long-lived world.
