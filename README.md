# TV Logos

Automated channel logo repository for IPTV and Xtream UI streaming servers.

This repository stores channel logos with clean, human-readable slugs and serves them directly via GitHub raw URLs:
```text
https://raw.githubusercontent.com/<OWNER>/tv-logos/<BRANCH>/logos/<clean-name>.<ext>
```

## Features

- **Clean File Naming**: Transliterates Latin accents (e.g. `č, ć, ž, š, đ`), collapses punctuation and spaces to `-`, and produces clean lowercase slugs from `stream_display_name`.
- **Collision Resolution**: Resolves filename collisions by cleanly appending stream primary key IDs when multiple distinct streams produce the same filename.
- **Deduplication**: Reuses existing logo files and raw URLs when multiple streams share the identical original source image URL.
- **Format Validation**: Automatically validates images via MIME type and header magic bytes (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.svg`), refusing HTML error pages.
- **Idempotency**: Skips streams already migrated to this repository whose logo files exist.
- **Database Safety**: 
  - Validates and pushes files to GitHub before updating the database.
  - Verifies public raw URLs via HTTP before writing to MySQL.
  - Uses parameterized SQL updates.
  - Automatically exports a local mapping backup (`stream_icon_backup.csv`) ignored by git.

## Directory Structure

```text
tv-logos/
├── logos/              # Channel logo images
├── sync_logos.py       # Synchronization and database migration script
├── requirements.txt    # Python dependencies
├── README.md           # Documentation
└── .gitignore          # Ignores credentials, backups, and caches
```

## Setup & Usage

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Set credentials via environment variables (never commit credentials into Git):

```bash
export DB_HOST="your-db-host"
export DB_PORT="3306"
export DB_USER="your-db-user"
export DB_PASSWORD="your-db-password"
export DB_NAME="xui"
export GITHUB_TOKEN="your-github-token"   # Optional if gh CLI is authenticated
```

On Windows PowerShell:

```powershell
$env:DB_HOST="your-db-host"
$env:DB_PORT="3306"
$env:DB_USER="your-db-user"
$env:DB_PASSWORD="your-db-password"
$env:DB_NAME="xui"
```

### 3. Dry Run

To inspect the database, check URLs, simulate filenames, and verify proposed mappings without modifying MySQL or pushing commits:

```bash
python sync_logos.py --dry-run
```

### 4. Live Synchronization

To download missing logos, commit and push to GitHub, and update MySQL:

```bash
python sync_logos.py
```
