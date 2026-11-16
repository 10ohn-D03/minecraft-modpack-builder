# Files in this repository bundle

- `.github/workflows/build-prism-pack.yml` — manual/automatic GitHub Actions job that resolves the wishlist and uploads the build artifacts.
- `input/mods-original.txt` — the original 259-entry wishlist, unchanged.
- `input/mod-aliases.json` — name corrections for common filename/project-name differences.
- `scripts/build_mrpack.py` — Modrinth API resolver, dependency walker, compatibility checks, report writer, and `.mrpack` packager.
- `README.md` — setup/run/import instructions and limitations.
- `.gitignore` — ignores local Python cache and generated output.
