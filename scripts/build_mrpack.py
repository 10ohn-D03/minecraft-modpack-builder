#!/usr/bin/env python3
"""Resolve the supplied mod wishlist against Modrinth and build a Prism .mrpack.

Only exact, strongly matched Modrinth projects with a Minecraft 1.21.1 NeoForge
version are included. Uncertain matches and missing CurseForge-only projects are
reported rather than guessed.
"""
from __future__ import annotations

import csv
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "input" / "mods-original.txt"
ALIASES_FILE = ROOT / "input" / "mod-aliases.json"
OUT = ROOT / "dist"
MC_VERSION = os.getenv("MC_VERSION", "1.21.1")
LOADER = "neoforge"
API = "https://api.modrinth.com/v2"
USER_AGENT = os.getenv("MODRINTH_USER_AGENT", "MinecraftModpackBuilder/1.0 (modpack resolver)")
PACK_NAME = "Adventure Modpack - 1.21.1 NeoForge"

# These are deliberately limited to broadly useful performance projects.
# A project is only included if it has a real 1.21.1 NeoForge release.
PERFORMANCE_REQUESTS = [
    "Sodium",
    "ModernFix",
    "FerriteCore",
    "ImmediatelyFast",
    "Entity Culling",
    "More Culling",
    "Dynamic FPS",
    "Lithium",
]

SESSION_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
_LAST_REQUEST_AT = 0.0

def throttle() -> None:
    """Keep below the public API rate ceiling with a little headroom."""
    global _LAST_REQUEST_AT
    now = time.monotonic()
    delay = 0.30 - (now - _LAST_REQUEST_AT)
    if delay > 0:
        time.sleep(delay)
    _LAST_REQUEST_AT = time.monotonic()


def request_json(url: str, retries: int = 5) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries):
        throttle()
        req = urllib.request.Request(url, headers=SESSION_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 or 500 <= exc.code < 600:
                wait = int(exc.headers.get("Retry-After", 0) or (2 ** attempt))
                time.sleep(min(max(wait, 1), 30))
                continue
            body = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"Request failed after {retries} tries: {url}: {last_error}")


def request_text(url: str) -> str:
    throttle()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read().decode("utf-8")


def normalize(value: str) -> str:
    value = value.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "", value)


def clean_filename(filename: str) -> str:
    """Turn a jar filename into a search-friendly project name."""
    name = Path(filename.strip()).name
    name = re.sub(r"(?i)\.jar$", "", name)
    name = re.sub(r"(?i)\.mod$", "", name)
    # Loader/platform labels are normally suffixes. Cut at the first one.
    loader_match = re.search(r"(?i)(?:^|[\s_-])(neoforge|forge|fabric|quilt|universal|bundled)(?:$|[\s_-])", name)
    if loader_match:
        name = name[:loader_match.start()].strip(" _-")
    # Remove embedded MC-version labels, then ordinary release numbers.
    mc_match = re.search(r"(?i)(?:^|[\s_-])(?:mc|minecraft)\s*\d+(?:\.\d+){0,3}[^ ]*", name)
    if mc_match:
        name = name[:mc_match.start()].strip(" _-")
    version_match = re.search(r"(?<![A-Za-z])v?\d+\.\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9._-]+)?", name)
    if version_match:
        name = name[:version_match.start()].strip(" _-")
    name = re.sub(r"(?i)\s+mc\s*$", "", name)
    name = re.sub(r"[_.-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def best_candidate_score(name: str, hit: dict[str, Any]) -> float:
    target = normalize(name)
    title = normalize(str(hit.get("title", "")))
    slug = normalize(str(hit.get("slug", "")))
    title_score = difflib.SequenceMatcher(None, target, title).ratio() if target and title else 0
    slug_score = difflib.SequenceMatcher(None, target, slug).ratio() if target and slug else 0
    score = max(title_score, slug_score)
    if target and (target in title or target in slug or title in target or slug in target):
        score = max(score, 0.88)
    return score


class Resolver:
    def __init__(self, aliases: dict[str, str]):
        self.aliases = aliases
        self.search_cache: dict[str, list[dict[str, Any]]] = {}
        self.project_cache: dict[str, dict[str, Any]] = {}
        self.version_cache: dict[str, list[dict[str, Any]]] = {}
        self.added: dict[str, dict[str, Any]] = {}
        self.file_records: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, str]] = []
        self.dependencies_report: list[dict[str, str]] = []
        self.conflicts: list[dict[str, str]] = []

    def search_projects(self, query: str) -> list[dict[str, Any]]:
        key = query.casefold()
        if key in self.search_cache:
            return self.search_cache[key]
        url = API + "/search?" + urllib.parse.urlencode({"query": query, "limit": "20", "index": "relevance"})
        data = request_json(url)
        hits = [h for h in data.get("hits", []) if h.get("project_type") == "mod"]
        self.search_cache[key] = hits
        return hits

    def identify_project(self, requested_name: str) -> tuple[dict[str, Any] | None, str]:
        search_name = self.aliases.get(requested_name.casefold(), requested_name)
        hits = self.search_projects(search_name)
        ranked = sorted(((best_candidate_score(search_name, h), h) for h in hits), key=lambda x: x[0], reverse=True)
        if not ranked:
            return None, "No Modrinth project search result"
        top_score, top = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if top_score < 0.82:
            return None, f"No sufficiently close title/slug match (best score {top_score:.2f})"
        if second_score and top_score - second_score < 0.07 and top_score < 0.97:
            return None, f"Ambiguous Modrinth search results ({top.get('title')} {top_score:.2f}; next {second_score:.2f})"
        project_id = top["project_id"]
        if project_id not in self.project_cache:
            self.project_cache[project_id] = request_json(f"{API}/project/{project_id}")
        return self.project_cache[project_id], ""

    def compatible_versions(self, project_id: str) -> list[dict[str, Any]]:
        if project_id in self.version_cache:
            return self.version_cache[project_id]
        params = urllib.parse.urlencode({
            "loaders": json.dumps([LOADER]),
            "game_versions": json.dumps([MC_VERSION]),
            "include_changelog": "false",
        })
        versions = request_json(f"{API}/project/{project_id}/version?{params}")
        # Prefer stable releases; use beta/alpha only when no release exists.
        priority = {"release": 0, "beta": 1, "alpha": 2}
        versions = sorted(versions, key=lambda v: (priority.get(v.get("version_type"), 3), v.get("date_published", "")), reverse=False)
        if versions:
            best_priority = min(priority.get(v.get("version_type"), 3) for v in versions)
            versions = [v for v in versions if priority.get(v.get("version_type"), 3) == best_priority]
            versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        self.version_cache[project_id] = versions
        return versions

    def get_exact_version(self, version_id: str) -> dict[str, Any] | None:
        try:
            return request_json(f"{API}/version/{version_id}")
        except Exception:
            return None

    def add_project(self, project: dict[str, Any], reason: str, preferred_version_id: str | None = None) -> tuple[bool, str]:
        pid = project.get("id")
        if not pid:
            return False, "Project has no Modrinth ID"
        if pid in self.added:
            return True, "already included"

        chosen = None
        if preferred_version_id:
            exact = self.get_exact_version(preferred_version_id)
            if exact and MC_VERSION in exact.get("game_versions", []) and LOADER in exact.get("loaders", []):
                chosen = exact
        versions = self.compatible_versions(pid)
        if not chosen and versions:
            chosen = versions[0]
        if not chosen:
            return False, f"No {MC_VERSION} {LOADER} version"

        files = chosen.get("files", [])
        jar_files = [f for f in files if str(f.get("filename", "")).lower().endswith(".jar")]
        primary = next((f for f in jar_files if f.get("primary")), None) or (jar_files[0] if jar_files else None)
        if not primary or not primary.get("url"):
            return False, "Compatible version has no downloadable JAR"
        hashes = primary.get("hashes", {})
        if not hashes.get("sha1") or not hashes.get("sha512"):
            return False, "Modrinth file is missing required SHA-1/SHA-512 hashes"

        project_info = {
            "id": pid,
            "title": project.get("title", pid),
            "slug": project.get("slug", ""),
            "version_id": chosen.get("id"),
            "version_number": chosen.get("version_number", ""),
            "filename": primary.get("filename"),
            "url": primary.get("url"),
            "size": primary.get("size", 0),
            "hashes": hashes,
            "client_side": project.get("client_side", "required"),
            "server_side": project.get("server_side", "required"),
            "reason": reason,
            "dependencies": chosen.get("dependencies", []),
        }
        self.added[pid] = project_info
        self.file_records.append(project_info)
        return True, chosen.get("version_number", "")

    def resolve_roots_and_dependencies(self, requests: list[tuple[str, str]]) -> None:
        queue: deque[tuple[dict[str, Any], str, str | None]] = deque()
        # Resolve roots first. Keep the original filename in the report.
        seen_root_names: set[str] = set()
        for original, display_name in requests:
            norm = normalize(display_name)
            if norm in seen_root_names:
                continue
            seen_root_names.add(norm)
            project, error = self.identify_project(display_name)
            if not project:
                self.unresolved.append({"requested": original, "identified_name": display_name, "reason": error, "source": "root mod"})
                continue
            queue.append((project, f"requested: {display_name}", None))

        processed: set[str] = set()
        while queue:
            project, reason, preferred_version_id = queue.popleft()
            pid = project.get("id")
            if not pid or pid in processed:
                continue
            processed.add(pid)
            ok, detail = self.add_project(project, reason, preferred_version_id)
            if not ok:
                self.unresolved.append({"requested": project.get("title", pid), "identified_name": project.get("slug", ""), "reason": detail, "source": "dependency" if reason.startswith("dependency") else "root mod"})
                continue
            added = self.added.get(pid)
            if not added:
                continue
            for dep in added.get("dependencies", []):
                if dep.get("dependency_type") == "incompatible" and dep.get("project_id"):
                    self.conflicts.append({"project": added["title"], "conflicts_with": dep.get("project_id", ""), "source_version": added["version_number"]})
                if dep.get("dependency_type") != "required":
                    continue
                dep_pid = dep.get("project_id")
                dep_version_id = dep.get("version_id")
                if not dep_pid:
                    self.dependencies_report.append({"parent": added["title"], "dependency": dep.get("file_name") or dep.get("version_id") or "unknown", "status": "external/unindexed dependency; manual review needed"})
                    continue
                if dep_pid in processed or dep_pid in self.added:
                    continue
                try:
                    dep_project = request_json(f"{API}/project/{dep_pid}")
                except Exception as exc:
                    self.unresolved.append({"requested": dep_pid, "identified_name": dep_pid, "reason": f"Required dependency project lookup failed: {exc}", "source": "dependency"})
                    continue
                queue.append((dep_project, f"dependency of {added['title']}", dep_version_id))
                self.dependencies_report.append({"parent": added["title"], "dependency": dep_project.get("title", dep_pid), "status": "queued for compatible version resolution"})


def latest_neoforge_version() -> str:
    """Read NeoForge Maven metadata and select newest stable 21.1.x release."""
    url = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
    xml_text = request_text(url)
    root = ET.fromstring(xml_text)
    versions = [el.text.strip() for el in root.findall(".//version") if el.text]
    compatible = [v for v in versions if re.fullmatch(r"21\.1\.\d+", v)]
    if not compatible:
        raise RuntimeError("Could not find a stable NeoForge 21.1.x version in official Maven metadata")
    return max(compatible, key=lambda v: tuple(int(p) for p in v.split(".")))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")
    aliases = json.loads(ALIASES_FILE.read_text(encoding="utf-8")) if ALIASES_FILE.exists() else {}
    raw_lines = [line.strip() for line in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.strip().startswith("#")]
    if not raw_lines:
        raise RuntimeError("No mod entries were found in input/mods-original.txt")

    # Keep all raw rows in the source list, but collapse exact duplicate names for lookup.
    requests: list[tuple[str, str]] = []
    for raw in raw_lines:
        display = clean_filename(raw)
        if not display:
            continue
        display = aliases.get(display.casefold(), display)
        requests.append((raw, display))

    OUT.mkdir(parents=True, exist_ok=True)
    resolver = Resolver(aliases)
    print(f"Resolving {len(raw_lines)} input rows for Minecraft {MC_VERSION} / NeoForge...", flush=True)
    resolver.resolve_roots_and_dependencies(requests)

    # Add the performance layer through the same strict matching and dependency flow.
    performance_requests: list[tuple[str, str]] = []
    for name in PERFORMANCE_REQUESTS:
        project, error = resolver.identify_project(name)
        if project:
            performance_requests.append((f"[performance] {name}", project.get("title", name)))
        else:
            resolver.unresolved.append({"requested": name, "identified_name": name, "reason": error, "source": "performance mod"})
    resolver.resolve_roots_and_dependencies(performance_requests)

    if not resolver.added:
        raise RuntimeError("No mods resolved to valid 1.21.1 NeoForge files. No empty modpack was created.")

    neoforge_version = latest_neoforge_version()
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": f"adventure-1.21.1-neoforge-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "name": PACK_NAME,
        "summary": "Generated from the supplied mod wishlist. Only verified Modrinth 1.21.1 NeoForge files are included.",
        "files": [],
        "dependencies": {"minecraft": MC_VERSION, "neoforge": neoforge_version},
    }
    used_paths: set[str] = set()
    for item in sorted(resolver.file_records, key=lambda x: x["title"].casefold()):
        filename = re.sub(r"[^A-Za-z0-9._+() -]", "_", item["filename"])
        path = f"mods/{filename}"
        if path.casefold() in used_paths:
            stem, ext = os.path.splitext(filename)
            path = f"mods/{stem}-{item['id'][:6]}{ext}"
        used_paths.add(path.casefold())
        env = {}
        if item.get("client_side") in {"required", "optional", "unsupported"}:
            env["client"] = item["client_side"]
        if item.get("server_side") in {"required", "optional", "unsupported"}:
            env["server"] = item["server_side"]
        file_row = {
            "path": path,
            "hashes": {"sha1": item["hashes"]["sha1"], "sha512": item["hashes"]["sha512"]},
            "downloads": [item["url"]],
            "fileSize": int(item.get("size") or 0),
        }
        if env:
            file_row["env"] = env
        index["files"].append(file_row)

    resolved_rows = [{
        "project": x["title"], "slug": x["slug"], "project_id": x["id"],
        "version": x["version_number"], "version_id": x["version_id"],
        "filename": x["filename"], "reason": x["reason"],
    } for x in resolver.file_records]

    write_csv(OUT / "resolved-mods.csv", resolved_rows, ["project", "slug", "project_id", "version", "version_id", "filename", "reason"])
    write_csv(OUT / "unresolved-mods.csv", resolver.unresolved, ["requested", "identified_name", "reason", "source"])
    write_csv(OUT / "dependencies.csv", resolver.dependencies_report, ["parent", "dependency", "status"])
    write_csv(OUT / "conflicts.csv", resolver.conflicts, ["project", "conflicts_with", "source_version"])
    (OUT / "modrinth.index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    pack_path = OUT / "Adventure_Modpack_1.21.1_NeoForge.mrpack"
    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as pack:
        pack.writestr("modrinth.index.json", json.dumps(index, indent=2))
        # Keep simple human-readable report files with the pack as optional documentation.
        pack.writestr("README.txt", (
            "Adventure Modpack - Minecraft 1.21.1 NeoForge\n\n"
            f"Resolved Modrinth projects: {len(resolver.added)}\n"
            f"Unresolved requests: {len(resolver.unresolved)}\n"
            "See the separate CSV reports in the GitHub Actions artifact for details.\n"
        ))

    summary = [
        "# Modpack build report", "",
        f"- Minecraft: `{MC_VERSION}`", f"- Loader: `NeoForge {neoforge_version}`",
        f"- Original input rows: **{len(raw_lines)}**", f"- Resolved projects (including required dependencies and performance mods): **{len(resolver.added)}**",
        f"- Unresolved requests/dependencies: **{len(resolver.unresolved)}**", f"- External/unknown required dependencies: **{len(resolver.dependencies_report)}**", f"- Declared incompatibilities: **{len(resolver.conflicts)}**", "",
        "The `.mrpack` contains only Modrinth files whose metadata confirms Minecraft 1.21.1 and NeoForge support. Unresolved entries are not silently substituted.",
    ]
    (OUT / "BUILD-SUMMARY.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary), flush=True)
    print(f"Created: {pack_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        raise
