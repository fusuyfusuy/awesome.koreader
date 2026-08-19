#!/usr/bin/env python3
"""
crawl.py — Automated Multi-Vector GitHub Crawler & Catalog Builder for KOReader

Self-contained crawler and catalog generator:
1. Harvests KOReader plugins, user patches, and companion tools from GitHub API and upstream awesome-koreader.
2. Normalizes, deduplicates, and classifies all entries across 15 standard categories.
3. Rebuilds plugins.json (machine-readable database) and PLUGINS.md (human-readable catalog).
"""

import os
import sys
import json
import time
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# Base repository directory (parent of .github/scripts)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGINS_JSON_PATH = os.path.join(REPO_ROOT, "plugins.json")
PLUGINS_MD_PATH = os.path.join(REPO_ROOT, "PLUGINS.md")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "awesome-koreader-crawler/1.0"
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

SEARCH_QUERIES = [
    ("topic:koreader-plugin fork:false", "github-topic:koreader-plugin"),
    ("topic:koreader-plugin fork:only stars:>=1", "github-topic:koreader-plugin-fork"),
    ("in:name .koplugin fork:false", "github-name:.koplugin"),
    ("in:name .koplugin fork:only stars:>=1", "github-name:.koplugin-fork"),
    ("topic:koreader-user-patch fork:false", "github-topic:koreader-user-patch"),
    ("in:name \"KOReader.patches\"", "github-name:KOReader.patches"),
    ("\"koreader plugin\" in:description fork:false", "github-search:koreader-plugin-desc"),
    ("in:name koreader-plugin fork:false", "github-name:koreader-plugin"),
]

CATEGORIES = [
    ("🤖 AI & Intelligent Assistants", "Large Language Models, translation, summarization, and AI reading assistants (Claude, GPT, Ollama, DeepSeek, Gemini)."),
    ("📚 Book Discovery & Library Catalogs", "OPDS clients, Calibre content server, Zotero, and book downloaders."),
    ("🔄 Sync, Cloud & File Transfer", "Progress synchronization, cloud storage (WebDAV, Dropbox, Nextcloud), LocalSend, Tailscale, and Syncthing."),
    ("✍️ Notes, Highlights & Flashcards", "Exporting and syncing annotations to Obsidian, Anki, Notion, Readwise, Joplin, and Flomo."),
    ("📖 Reading & Typography", "Vertical text rendering (Tategumi), reading rulers, speed reading, autoturn, and layout tools."),
    ("🌐 Dictionaries & Translation", "Offline translation, popup dictionaries, multilingual vocabulary, and word reference tools."),
    ("📊 Reading Stats, Tracking & Goals", "Reading timers, streak trackers, gamification (ReadMastery), and reading speed analytics."),
    ("🎨 Comics, Manga & Graphic Novels", "Panel-by-panel reading (Panels+), manga source fetchers (Rakuyomi), and CBZ/CBR enhancers."),
    ("📰 RSS & Read-It-Later", "RSS feed readers, Wallabag, Instapaper, Omnivore, and Readeck integrations."),
    ("🖼️ UI, Themes & Customization", "Alternative home screens (ProjectTitle, SimpleUI, Zen UI), custom menu organizers, and screensavers."),
    ("⚡ Hardware, Device & Controls", "Bluetooth remotes, gamepads, Kindle/Kobo hardware integrations, frontlight automation, and battery stats."),
    ("🎮 Games & Entertainment", "Sudoku, Chess, Solitaire, Sokoban, 2048, Crosswords, and interactive fiction engines (Frotz)."),
    ("🛠️ Utilities & Workflow Tools", "Terminal emulators, code/text editors, QR clipboard, HTTP debugging, folder lockers, and productivity tools."),
    ("🖥️ Companion Tools & Sync Servers", "Desktop GUIs (KoHighlights, KoInsight), self-hosted sync backends, and Calibre desktop plugins."),
    ("🧩 User Patch Collections", "Community user-patch scripts and visual mod suites for KOReader's patch loader."),
]

def gh_api_get(url, max_retries=3):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                remaining = resp.headers.get("x-ratelimit-remaining")
                if remaining and int(remaining) < 5:
                    print(f"[WARN] Low API rate limit remaining: {remaining}")
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"[WARN] HTTP {e.code} for {url}: {e.reason} (Attempt {attempt}/{max_retries})")
            if e.code == 403 or e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait_sec = int(retry_after) if retry_after else (attempt * 5)
                print(f"[WARN] Rate limited. Waiting {wait_sec}s before retry...")
                time.sleep(wait_sec)
            elif e.code >= 500:
                time.sleep(attempt * 2)
            else:
                return None
        except Exception as e:
            print(f"[WARN] Failed fetching {url}: {e} (Attempt {attempt}/{max_retries})")
            time.sleep(attempt * 2)
            
    print(f"[ERROR] Max retries exceeded for {url}")
    return None

def search_repositories(query, source_tag):
    repos = []
    page = 1
    per_page = 100
    encoded_q = urllib.parse.quote(query)
    
    while page <= 10:  # GitHub search pagination max 1000 items (10 pages)
        url = f"https://api.github.com/search/repositories?q={encoded_q}&sort=stars&order=desc&per_page={per_page}&page={page}"
        print(f"[*] Querying: '{query}' (page {page})...")
        data = gh_api_get(url)
        if not data or "items" not in data or not data["items"]:
            break
        
        items = data["items"]
        for it in items:
            it["_source_tag"] = source_tag
            repos.append(it)
            
        if len(items) < per_page or len(repos) >= data.get("total_count", 0):
            break
        page += 1
        time.sleep(1.0)
        
    print(f"    -> Found {len(repos)} items for '{query}'")
    return repos

def crawl_awesome_koreader():
    print("[*] Fetching upstream jannick-holm/awesome-koreader...")
    url = "https://raw.githubusercontent.com/jannick-holm/awesome-koreader/master/README.md"
    req = urllib.request.Request(url, headers={"User-Agent": "awesome-koreader-crawler/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        links = re.findall(r"\[([^\]]+)\]\((https://github\.com/([a-zA-Z0-9_\-\.]+)/([a-zA-Z0-9_\-\.]+))\)", content)
        print(f"    -> Extracted {len(links)} links from awesome-koreader")
        return links
    except Exception as e:
        print(f"[WARN] Failed fetching awesome-koreader: {e}")
        return []

def get_category_slugs(cat_name):
    """
    Returns (primary_clean_slug, [all_compatible_slug_aliases])
    e.g. for '⚡ Hardware, Device & Controls':
    clean_slug: 'hardware-device-controls'
    aliases: ['hardware-device-controls', 'hardware-device--controls', '-hardware-device--controls']
    """
    clean = re.sub(r"[^\w\s-]", "", cat_name.lower()).strip()
    clean_slug = re.sub(r"[-\s]+", "-", clean)
    
    legacy_slug = re.sub(r"[^\w\- ]", "", cat_name.lower()).strip().replace(" ", "-")
    gfm_slug = re.sub(r"[^\w\- ]", "", cat_name.lower()).replace(" ", "-")

    aliases = []
    for s in [clean_slug, legacy_slug, gfm_slug]:
        if s and s not in aliases:
            aliases.append(s)
            
    return clean_slug, aliases

def categorize_item(item):
    item_type = item.get("type", "plugin")
    if (item_type in ("patch", "patch_collection") or
        "patch" in item.get("id", "").lower() or
        "patches" in item.get("repo_name", "").lower() or
        "topic:koreader-user-patch" in item.get("sources", [])):
        return "🧩 User Patch Collections"
    if (item_type in ("companion", "companion_tool") or
        "companion" in item.get("sources", [])):
        return "🖥️ Companion Tools & Sync Servers"

    text = " ".join([
        item.get("id", ""),
        item.get("name", ""),
        item.get("repo_name", ""),
        item.get("description", "") or "",
        " ".join(item.get("topics", []))
    ]).lower()

    # 1. Games & Entertainment
    if re.search(r"\b(game|chess|sudoku|solitaire|sokoban|2048|crossword|frotz|gameboy|minesweeper|tetris|card game|wordle|puzzle|interactive fiction|z-machine|othello|reversi|connect4|connect four)\b", text):
        return "🎮 Games & Entertainment"

    # 2. Comics, Manga & Graphic Novels
    if re.search(r"\b(manga|comic|cbr|cbz|cb7|panels\+|rakuyomi|tachiyomi|suwayomi|mangadex|webtoon|graphic novel)\b", text):
        return "🎨 Comics, Manga & Graphic Novels"

    # 3. RSS & Read-It-Later
    if re.search(r"\b(rss|feed|atom|wallabag|instapaper|omnivore|readeck|pocket|miniflux|freshrss|feedly|newsblur|newsdownloader|read-it-later|read it later)\b", text):
        return "📰 RSS & Read-It-Later"

    # 4. AI & Intelligent Assistants
    if re.search(r"\b(llm|chatgpt|gpt-4|gpt-3|gpt-4o|claude|ollama|deepseek|gemini|openai|ai assistant|ai helper|ai dictionary|koassistant|xray|x-ray|artificial intelligence|qwen|koai|openrouter|perplexity|bedrock|copilot|assistant|large language|mcp)\b", text):
        return "🤖 AI & Intelligent Assistants"

    # 5. Notes, Highlights & Flashcards
    if re.search(r"\b(highlight|anki|obsidian|notion|readwise|joplin|flomo|flashcard|annotation|annotations|vocabbuilder|export highlights|telegramhighlights|logseq|heptabase|marginnote|scratchpad|flashcards|vocabulary builder|vocabdeck|smartdeck)\b", text):
        return "✍️ Notes, Highlights & Flashcards"

    # 6. Reading Stats, Tracking & Goals
    if re.search(r"\b(streak|reading timer|reading speed|readmastery|readtimer|reading goal|readingtracker|progress tracker|reading stats|reading time|reading log|reading analytics|gamification|habittracker|habit tracker)\b", text):
        return "📊 Reading Stats, Tracking & Goals"

    # 7. Sync, Cloud & File Transfer
    if re.search(r"\b(sync|syncthing|webdav|dropbox|nextcloud|localsend|tailscale|wireguard|cloud|kosync|ssh|sftp|ftp|scp|rsync|mtp|file transfer|calibre-web|send to kindle|sendtokoreader|network share)\b", text):
        return "🔄 Sync, Cloud & File Transfer"

    # 8. Book Discovery & Library Catalogs
    if re.search(r"\b(calibre|opds|z-library|zlibrary|zotero|gutenberg|library catalog|download epub|book downloader|readest|emailtokoreader|libgen|standard ebooks|annas archive|flibusta|open library)\b", text):
        return "📚 Book Discovery & Library Catalogs"

    # 9. Dictionaries & Translation
    if re.search(r"\b(dict|dictionary|translat|stardict|dictd|wiktionary|vocabulary|lookup|multilingual|japanese|furigana|yomichan|deepl|pronunciation)\b", text):
        return "🌐 Dictionaries & Translation"

    # 10. Reading & Typography
    if re.search(r"\b(tategumi|vertical text|bionic|speed reading|ruler|autoturn|auto turn|page turn|typography|typeset|perceptionexpander|hyphenation|line spacing|page layout)\b", text):
        return "📖 Reading & Typography"

    # 11. UI, Themes & Customization
    if re.search(r"\b(zen_ui|zen ui|plainui|simpleui|projecttitle|theme|themes|launcher|home screen|homescreen|custom menu|dark mode|palette|derainbowify|screensaver|screensavers|lockscreen|wallpaper|coverimage|cover image|book cover|status bar|statusbar|visual tweak|look and feel|quick settings|quicksettings|bookshelf)\b", text):
        return "🖼️ UI, Themes & Customization"

    # 12. Hardware, Device & Controls (strictly hardware peripherals & physical device controls)
    if re.search(r"\b(bluetooth|bt remote|gamepad|page turner|frontlight|backlight|warmth|brightness|battery|power management|sleep cover|hall sensor|button remap|hardware key|airplanemode|airplane mode|cpu clock|overclock|governor|accelerometer|gyroscope|touchscreen|e-ink refresh|waveform|otg|usb host|heartbeat|keepalive|systemstat)\b", text):
        return "⚡ Hardware, Device & Controls"
    
    return "🛠️ Utilities & Workflow Tools"

def source_badge(src):
    s = src.lower()
    if "awesome" in s: return "`awesome`"
    if "contrib" in s: return "`contrib`"
    if "builtin" in s: return "`builtin`"
    if "topic:koreader-plugin" in s: return "`topic:plugin`"
    if "topic:koreader-user-patch" in s: return "`topic:patch`"
    if "name:.koplugin" in s: return "`name:.koplugin`"
    if "name:koreader.patches" in s: return "`name:patches`"
    if "desc" in s: return "`gh:desc`"
    if "code" in s: return "`gh:code`"
    return f"`{src}`"

def clean_description(desc):
    if not desc:
        return "No description provided."
    d = desc.replace("\n", " ").replace("\r", " ").replace("|", "\\|").strip()
    return re.sub(r"\s+", " ", d)

def build_markdown_and_catalog(data):
    all_items = []
    
    # Process plugins
    for item in data.get("plugins", []):
        cat = categorize_item(item)
        item["category"] = cat.split(" ", 1)[-1] if " " in cat else cat
        item["category_full"] = cat
        all_items.append(item)

    # Process patches
    for item in data.get("patches", []):
        cat = "🧩 User Patch Collections"
        item["category"] = "User Patch Collections"
        item["category_full"] = cat
        all_items.append(item)

    # Process companions
    for item in data.get("companions", []):
        cat = "🖥️ Companion Tools & Sync Servers"
        item["category"] = "Companion Tools & Sync Servers"
        item["category_full"] = cat
        all_items.append(item)

    # Sort each category by stars descending
    category_map = {cat_name: [] for cat_name, _ in CATEGORIES}
    for item in all_items:
        cat_full = item.get("category_full", "🛠️ Utilities & Workflow Tools")
        if cat_full not in category_map:
            category_map[cat_full] = []
        category_map[cat_full].append(item)

    for cat in category_map:
        category_map[cat].sort(key=lambda x: (x.get("stars", 0) or 0), reverse=True)

    # Generate PLUGINS.md
    lines = []
    lines.append("# Master KOReader Plugin Catalog\n")
    lines.append("> **Comprehensive, deduplicated index of official, community, and contrib KOReader plugins, user patches, and companion tools.**\n")
    lines.append(f"**Total Tracked Entries:** {len(all_items)} | **Plugins:** {len(data.get('plugins', []))} | **Patch Collections:** {len(data.get('patches', []))} | **Companion Tools:** {len(data.get('companions', []))}\n")
    lines.append("---\n")
    lines.append("## Table of Contents\n")

    for cat_name, _ in CATEGORIES:
        items = category_map.get(cat_name, [])
        clean_slug, _ = get_category_slugs(cat_name)
        lines.append(f"- [{cat_name}](#{clean_slug}) ({len(items)})")

    lines.append("\n---\n")

    for cat_name, desc in CATEGORIES:
        items = category_map.get(cat_name, [])
        clean_slug, aliases = get_category_slugs(cat_name)
        anchor_tags = "".join([f'<a id="{a}"></a>' for a in aliases])

        lines.append(f"{anchor_tags}\n## {cat_name}\n")
        lines.append(f"*{desc}*\n")
        lines.append(f"**Total:** {len(items)} items\n")
        lines.append("| Plugin / Folder | Description | Author | Stars | Sources |")
        lines.append("| :--- | :--- | :--- | :---: | :--- |")

        for item in items:
            name_label = item.get("id") or item.get("name") or item.get("repo_name")
            url = item.get("url") or f"https://github.com/{item.get('owner', '')}/{item.get('repo_name', '')}"
            owner = item.get("owner") or "Unknown"
            owner_url = f"https://github.com/{owner}" if owner != "Unknown" else url
            stars = item.get("stars", 0) or 0
            desc_text = clean_description(item.get("description", ""))
            sources_badges = " ".join([source_badge(s) for s in item.get("sources", [])[:3]])

            lines.append(f"| **[{name_label}]({url})** | {desc_text} | [{owner}]({owner_url}) | ⭐ {stars:,} | {sources_badges} |")

        lines.append("\n---\n")

    with open(PLUGINS_MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Update metadata in plugins.json
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "metadata" not in data:
        data["metadata"] = {}
    data["metadata"]["last_updated"] = now_iso
    data["metadata"]["generated_at"] = now_iso
    data["metadata"]["counts"] = {
        "plugins": len(data.get("plugins", [])),
        "patches": len(data.get("patches", [])),
        "companion_tools": len(data.get("companions", [])),
        "total_records": len(all_items)
    }
    data["metadata"]["total_count"] = len(all_items)
    data["metadata"]["plugins_count"] = len(data.get("plugins", []))
    data["metadata"]["patches_count"] = len(data.get("patches", []))
    data["metadata"]["companions_count"] = len(data.get("companions", []))

    with open(PLUGINS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[✓] Generated PLUGINS.md ({len(all_items)} entries) and updated plugins.json successfully.")

def main():
    # Load existing database if present
    existing_db = {}
    if os.path.exists(PLUGINS_JSON_PATH):
        try:
            with open(PLUGINS_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data.get("plugins", []) + data.get("patches", []) + data.get("companions", []):
                    key = (item.get("full_name") or f"{item.get('owner')}/{item.get('repo_name')}").lower()
                    existing_db[key] = item
        except Exception as e:
            print(f"[WARN] Error loading existing plugins.json: {e}")

    print(f"[*] Loaded {len(existing_db)} existing entries from plugins.json")

    # Step 1: Run GitHub Search Queries
    raw_repos = {}
    for query, source_tag in SEARCH_QUERIES:
        found = search_repositories(query, source_tag)
        for r in found:
            full_name = r["full_name"].lower()
            if full_name not in raw_repos:
                raw_repos[full_name] = r
                raw_repos[full_name]["_sources"] = {source_tag}
            else:
                raw_repos[full_name]["_sources"].add(source_tag)
        time.sleep(1.0)

    # Step 2: Ingest awesome-koreader links
    awesome_links = crawl_awesome_koreader()
    for title, url, owner, repo in awesome_links:
        full_name = f"{owner}/{repo}".lower()
        if full_name in raw_repos:
            raw_repos[full_name]["_sources"].add("awesome-koreader")
        elif full_name in existing_db:
            existing_db[full_name].setdefault("sources", []).append("awesome-koreader")

    # Step 3: Merge & Normalization
    plugins_list = []
    patches_list = []
    companions_list = []

    for full_name, r in raw_repos.items():
        owner = r["owner"]["login"] if isinstance(r.get("owner"), dict) else (r.get("owner") or "Unknown")
        repo_name = r["name"]
        
        entry = existing_db.get(full_name, {})
        is_patch = ("patch" in repo_name.lower() or 
                    "patches" in repo_name.lower() or 
                    "topic:koreader-user-patch" in r.get("_sources", set()) or 
                    entry.get("type") in ("patch", "patch_collection"))
        is_companion = entry.get("type") in ("companion", "companion_tool")
        
        entry_type = "patch_collection" if is_patch else ("companion_tool" if is_companion else entry.get("type", "community_plugin"))
        
        entry.update({
            "id": repo_name if repo_name.endswith(".koplugin") else f"{repo_name}.koplugin",
            "name": repo_name.replace(".koplugin", ""),
            "full_name": r["full_name"],
            "owner": owner,
            "repo_name": repo_name,
            "url": r["html_url"],
            "description": r.get("description") or entry.get("description") or "No description provided.",
            "stars": r.get("stargazers_count", 0),
            "fork": r.get("fork", False),
            "archived": r.get("archived", False),
            "default_branch": r.get("default_branch", "main"),
            "topics": r.get("topics", []),
            "pushed_at": r.get("pushed_at"),
            "created_at": r.get("created_at"),
            "type": entry_type
        })
        
        # Merge sources
        curr_sources = set(entry.get("sources", [])) | r.get("_sources", set())
        entry["sources"] = sorted(list(curr_sources))
        
        if is_patch:
            patches_list.append(entry)
        elif is_companion:
            companions_list.append(entry)
        else:
            plugins_list.append(entry)

    # Re-insert any existing entries not hit in this crawl
    for k, v in existing_db.items():
        if k not in raw_repos:
            t = v.get("type", "")
            if t in ("patch", "patch_collection") or "patch" in v.get("category", "").lower():
                patches_list.append(v)
            elif t in ("companion", "companion_tool") or "companion" in v.get("category", "").lower():
                companions_list.append(v)
            else:
                plugins_list.append(v)

    combined_db = {
        "metadata": {
            "title": "Master KOReader Plugin Database",
            "description": "Comprehensive, deduplicated catalog of all official, community, contrib, and indexed KOReader plugins, user patches, and companion tools.",
        },
        "plugins": plugins_list,
        "patches": patches_list,
        "companions": companions_list
    }

    # Step 4: Categorize, generate PLUGINS.md and update plugins.json
    build_markdown_and_catalog(combined_db)

if __name__ == "__main__":
    main()
