#!/usr/bin/env python3
"""
tv-logos / sync_logos.py

Synchronizes channel logos from Xtream / XUI MySQL database into a public
GitHub repository (tv-logos) and updates streams.stream_icon with permanent
raw.githubusercontent.com URLs.

Safety features:
- Dry-run mode (`--dry-run`)
- Validates image files (refuses HTML error pages)
- Deduplicates identical original URLs
- Creates local CSV backup before writing to database
- Pushes files to GitHub and verifies raw URLs BEFORE database update
- Parameterized SQL updates
- Idempotent: skips streams already pointing to this repo whose logos exist
"""

import argparse
import csv
import datetime
import os
import re
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import pymysql
import requests
import urllib3

# Ensure stdout and stderr use utf-8 with replacement and line buffering
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)

# Suppress insecure request warnings for hosts with expired SSL certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SUPPORTED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg'}


def slugify_display_name(name: str) -> str:
    """
    Transforms channel display name into clean, human-readable slug.
    1. Lowercase.
    2. Transliterate Latin and Slavic accents (č, ć, ž, š, đ).
    3. Replace separators, spaces, and punctuation with '-'.
    4. Collapse multiple '-' into single '-'.
    5. Strip leading and trailing '-'.
    """
    if not name:
        return ""
    trans_map = {
        'đ': 'dj', 'Đ': 'dj',
        'č': 'c', 'Č': 'c',
        'ć': 'c', 'Ć': 'c',
        'ž': 'z', 'Ž': 'z',
        'š': 's', 'Š': 's',
    }
    for k, v in trans_map.items():
        name = name.replace(k, v)
    normalized = unicodedata.normalize('NFKD', name)
    ascii_text = normalized.encode('ascii', 'ignore').decode('ascii')
    lowered = ascii_text.lower()
    cleaned = re.sub(r'[^a-z0-9]+', '-', lowered)
    collapsed = re.sub(r'-+', '-', cleaned).strip('-')
    return collapsed


def get_url_path_extension(url: str) -> str:
    """Extracts known image extension from URL path if present."""
    parsed = urlparse(url.strip())
    path = parsed.path
    ext = os.path.splitext(path)[1].lower()
    if ext in SUPPORTED_IMAGE_EXTENSIONS:
        return ext
    # Regex fallback for URLs with subpaths like /name.png/revision/latest
    match = re.search(r'(\.(png|jpe?g|webp|gif|svg))(/|$)', path, re.I)
    if match:
        found = match.group(1).lower()
        return '.jpg' if found == '.jpeg' else found
    return ''


def detect_image_type(content: bytes, content_type: str = '') -> str:
    """
    Validates that content is a real image and detects the proper file extension.
    Returns extension with dot (e.g. '.png') or empty string if not a valid image.
    """
    if not content or len(content) < 16:
        return ''

    # Check for HTML error pages
    header_sample = content[:512].lower()
    if b'<!doctype html' in header_sample or b'<html' in header_sample:
        return ''

    # Magic byte signatures
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if content.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if content.startswith(b'GIF87a') or content.startswith(b'GIF89a'):
        return '.gif'
    if content.startswith(b'RIFF') and len(content) > 12 and content[8:12] == b'WEBP':
        return '.webp'
    if b'<svg' in header_sample:
        return '.svg'

    # Fallback to Content-Type header if magic bytes are ambiguous
    ct = (content_type or '').lower().split(';')[0].strip()
    ct_map = {
        'image/png': '.png',
        'image/jpeg': '.jpg',
        'image/pjpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'image/svg+xml': '.svg',
    }
    return ct_map.get(ct, '')


def normalize_source_url(url: str) -> str:
    """Converts github.com blob URLs to raw.githubusercontent.com URLs."""
    url = url.strip()
    m = re.match(r'^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*?)(?:\?.*)?$', url)
    if m:
        owner, repo, branch, path = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    return url


def download_single_image(url: str, timeout: tuple = (3.0, 6.0)) -> tuple[bool, bytes, str, str]:
    """
    Downloads image with fallback strategies for SSL errors, Wikimedia thumbnails, and rate-limits.
    Returns: (success: bool, content: bytes, ext: str, error_message: str)
    """
    headers = {'User-Agent': DEFAULT_USER_AGENT}
    normalized = normalize_source_url(url)
    urls_to_try = [normalized]

    # Wikimedia thumb fallback: if URL is a thumbnail, prepare original and 500px fallbacks
    if 'upload.wikimedia.org' in normalized and '/thumb/' in normalized:
        m = re.match(r'^(https://upload\.wikimedia\.org/wikipedia/commons)/thumb/([a-f0-9]/[a-f0-9]{2}/[^/]+)/.*$', normalized)
        if m:
            orig_url = f"{m.group(1)}/{m.group(2)}"
            thumb_500 = f"{m.group(1)}/thumb/{m.group(2)}/500px-{os.path.basename(m.group(2))}.png"
            urls_to_try.extend([orig_url, thumb_500])

    last_error = ""
    for try_url in urls_to_try:
        for attempt in range(3):
            try:
                # First attempt with normal SSL verification
                try:
                    resp = requests.get(try_url, headers=headers, timeout=timeout, allow_redirects=True)
                except requests.exceptions.SSLError:
                    # Fallback without SSL verification for stations with expired certs
                    resp = requests.get(try_url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)

                if resp.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue

                if resp.status_code == 200:
                    ext = detect_image_type(resp.content, resp.headers.get('content-type', ''))
                    if ext:
                        return True, resp.content, ext, ""
                    else:
                        last_error = f"Response is not a valid image (CT: {resp.headers.get('content-type')}, len: {len(resp.content)})"
                        break
                else:
                    last_error = f"HTTP {resp.status_code}"
                    if resp.status_code in (404, 403, 410):
                        break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"

    return False, b"", "", last_error


def get_git_info(target_dir: str, owner_arg: str = None, token_arg: str = None, branch_arg: str = None):
    """
    Determines authenticated GitHub username, default branch, and token.
    """
    token = token_arg or os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
            token = res.stdout.strip()
        except Exception:
            token = None

    owner = owner_arg or os.environ.get("GITHUB_OWNER")
    if not owner:
        # Try gh api user
        try:
            res = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, check=True)
            owner = res.stdout.strip()
        except Exception:
            pass

    if not owner and token:
        # Try REST API with token
        try:
            r = requests.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if r.status_code == 200:
                owner = r.json().get("login")
        except Exception:
            pass

    if not owner:
        # Fallback to inspecting git remote or local git user
        try:
            res = subprocess.run(["git", "config", "user.name"], cwd=target_dir, capture_output=True, text=True)
            owner = res.stdout.strip()
        except Exception:
            pass

    if not owner:
        raise ValueError("Could not determine GitHub username. Set GITHUB_OWNER or authenticate via `gh auth login`.")

    branch = branch_arg or os.environ.get("GITHUB_BRANCH") or "main"
    return owner, token, branch


def ensure_git_repo(repo_dir: str, owner: str, repo_name: str, branch: str, token: str = None):
    """
    Ensures local directory is initialized as git repository with remote origin configured.
    """
    git_dir = os.path.join(repo_dir, ".git")
    if not os.path.exists(git_dir):
        subprocess.run(["git", "init", "-b", branch], cwd=repo_dir, check=True)

    # Ensure git user config is set
    res_user = subprocess.run(["git", "config", "user.name"], cwd=repo_dir, capture_output=True, text=True)
    if not res_user.stdout.strip():
        subprocess.run(["git", "config", "user.name", owner], cwd=repo_dir, check=True)
    res_email = subprocess.run(["git", "config", "user.email"], cwd=repo_dir, capture_output=True, text=True)
    if not res_email.stdout.strip():
        subprocess.run(["git", "config", "user.email", f"{owner}@users.noreply.github.com"], cwd=repo_dir, check=True)

    # Check remote origin
    remote_url = f"https://github.com/{owner}/{repo_name}.git"
    res = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_dir, capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=repo_dir, check=True)
    else:
        current_remote = res.stdout.strip()
        if owner not in current_remote:
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir, check=True)


def push_to_github(repo_dir: str, branch: str, message: str = "Add and synchronize TV logos"):
    """
    Stages, commits, and pushes downloaded logos and repository files to GitHub.
    Returns: count of files staged/committed.
    """
    # Stage logos and repo files
    subprocess.run(["git", "add", "-A", "logos", "sync_logos.py", "requirements.txt", "README.md", ".gitignore"],
                   cwd=repo_dir, check=True)

    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, check=True)
    staged_lines = [l for l in status.stdout.splitlines() if l.strip()]
    if not staged_lines:
        print("Git working tree clean, nothing new to commit.")
        return 0

    print(f"Staged changes detected ({len(staged_lines)} items). Committing...")
    subprocess.run(["git", "commit", "-m", message], cwd=repo_dir, check=True)

    print(f"Pushing to origin {branch}...")
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo_dir, check=True)
    return len(staged_lines)


def verify_raw_url(url: str, retries: int = 4, delay: float = 2.0) -> bool:
    """Verifies that public raw URL returns HTTP 200 with backoff for CDN propagation."""
    headers = {'User-Agent': DEFAULT_USER_AGENT}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(delay)
    return False


def main():
    parser = argparse.ArgumentParser(description="Synchronize channel logos to GitHub and update MySQL.")
    parser.add_argument("--dry-run", action="store_true", help="Perform simulation without database updates or git push.")
    parser.add_argument("--db-host", default=os.environ.get("DB_HOST", "89.58.25.146"), help="MySQL host")
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("DB_PORT", "3306")), help="MySQL port")
    parser.add_argument("--db-user", default=os.environ.get("DB_USER", "szsb"), help="MySQL user")
    parser.add_argument("--db-password", default=os.environ.get("DB_PASSWORD", "3018570760"), help="MySQL password")
    parser.add_argument("--db-name", default=os.environ.get("DB_NAME", "xui"), help="MySQL database name")
    parser.add_argument("--repo-owner", default=os.environ.get("GITHUB_OWNER"), help="GitHub repository owner")
    parser.add_argument("--repo-name", default=os.environ.get("GITHUB_REPO", "tv-logos"), help="GitHub repository name")
    parser.add_argument("--branch", default=os.environ.get("GITHUB_BRANCH", "main"), help="GitHub default branch")
    parser.add_argument("--workers", type=int, default=30, help="Concurrent download threads")
    parser.add_argument("--timeout", type=float, default=6.0, help="Download timeout in seconds")
    parser.add_argument("--backup-file", default="stream_icon_backup.csv", help="Backup CSV mapping filepath")
    args = parser.parse_args()

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    logos_dir = os.path.join(repo_dir, "logos")
    os.makedirs(logos_dir, exist_ok=True)

    print("==================================================", flush=True)
    print("           TV Channel Logo Synchronizer           ", flush=True)
    print("==================================================", flush=True)
    print(f"Mode: {'DRY RUN (No modifications)' if args.dry_run else 'LIVE MIGRATION'}", flush=True)
    print(f"Database: {args.db_user}@{args.db_host}:{args.db_port}/{args.db_name}", flush=True)

    # Determine GitHub configuration
    owner, token, branch = get_git_info(repo_dir, args.repo_owner, None, args.branch)
    raw_url_prefix = f"https://raw.githubusercontent.com/{owner}/{args.repo_name}/{branch}/logos/"
    print(f"GitHub Target: {owner}/{args.repo_name} (branch: {branch})", flush=True)
    print(f"Raw Base URL: {raw_url_prefix}", flush=True)

    if not args.dry_run:
        ensure_git_repo(repo_dir, owner, args.repo_name, branch, token)

    # 1. Connect to MySQL and query matching streams
    print("\nConnecting to MySQL database...", flush=True)
    db_conn = pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.db_name,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )

    try:
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT id, stream_display_name, stream_icon
                FROM streams
                WHERE type IN (1, 3, 4)
                  AND stream_icon IS NOT NULL
                  AND stream_icon <> ''
                ORDER BY id ASC;
            """)
            streams = cur.fetchall()

        total_matched = len(streams)
        print(f"Streams matched from database query: {total_matched}", flush=True)

        # 2. Check for streams already migrated
        already_migrated_count = 0
        streams_to_process = []

        # Track existing files in logos directory
        existing_disk_files = set(os.listdir(logos_dir))

        for s in streams:
            current_icon = (s['stream_icon'] or '').strip()
            if current_icon.startswith(raw_url_prefix):
                filename = current_icon[len(raw_url_prefix):].split('?')[0].split('#')[0]
                if filename in existing_disk_files:
                    already_migrated_count += 1
                    continue
            streams_to_process.append(s)

        print(f"Already migrated (and verified on disk): {already_migrated_count}", flush=True)
        print(f"Streams remaining to process: {len(streams_to_process)}", flush=True)

        # 3. Deduplicate unique remote image URLs to download
        url_to_streams = {}
        for s in streams_to_process:
            url = s['stream_icon'].strip()
            if not url.startswith("http://") and not url.startswith("https://"):
                continue
            if url not in url_to_streams:
                url_to_streams[url] = []
            url_to_streams[url].append(s)

        unique_urls = list(url_to_streams.keys())
        print(f"Unique remote logo URLs to download: {len(unique_urls)}", flush=True)

        # 4. Concurrently download unique logos
        print(f"\nDownloading and validating images ({args.workers} workers, timeout {args.timeout}s)...", flush=True)
        downloaded_data = {}  # url -> (content, detected_ext)
        failed_downloads = {}  # url -> error_reason

        req_timeout = (3.0, float(args.timeout))

        def _fetch_task(u):
            success, content, ext, err = download_single_image(u, timeout=req_timeout)
            return u, success, content, ext, err

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_url = {executor.submit(_fetch_task, u): u for u in unique_urls}
            completed_count = 0
            for future in as_completed(future_to_url):
                u, success, content, ext, err = future.result()
                completed_count += 1
                if success:
                    downloaded_data[u] = (content, ext)
                else:
                    failed_downloads[u] = err

                if completed_count % 50 == 0 or completed_count == len(unique_urls):
                    pct = (completed_count / len(unique_urls)) * 100.0
                    print(f"  Progress: {completed_count}/{len(unique_urls)} ({pct:.1f}%) processed "
                          f"({len(downloaded_data)} ok, {len(failed_downloads)} failed)", flush=True)

        print(f"Downloads successful: {len(downloaded_data)}", flush=True)
        print(f"Downloads failed: {len(failed_downloads)}", flush=True)

        # 5. Determine filenames and resolve collisions
        # Filename rule:
        # 1. Base slug from stream_display_name
        # 2. First URL gets clean slug.{ext}
        # 3. If another URL would produce the same filename, append stream ID: slug-{id}.{ext}
        # All streams with the exact same original URL share the exact same file and raw URL.
        used_filenames = set(existing_disk_files)
        url_to_filename = {}
        stream_updates = []  # tuples: (stream_id, display_name, old_icon, new_icon, local_file_path)

        for url, (content, ext) in downloaded_data.items():
            rep_stream = url_to_streams[url][0]
            rep_id = rep_stream['id']
            rep_name = rep_stream['stream_display_name']

            slug = slugify_display_name(rep_name)
            if not slug:
                slug = f"stream-{rep_id}"

            candidate_filename = f"{slug}{ext}"
            if candidate_filename in used_filenames:
                candidate_filename = f"{slug}-{rep_id}{ext}"
                while candidate_filename in used_filenames:
                    candidate_filename = f"{slug}-{rep_id}-alt{ext}"

            used_filenames.add(candidate_filename)
            url_to_filename[url] = candidate_filename
            file_path = os.path.join(logos_dir, candidate_filename)

            # Write file if not in dry-run
            if not args.dry_run:
                with open(file_path, "wb") as f:
                    f.write(content)

            # Prepare update mapping for all streams sharing this URL
            new_raw_url = f"{raw_url_prefix}{candidate_filename}"
            for s in url_to_streams[url]:
                stream_updates.append((s['id'], s['stream_display_name'], s['stream_icon'], new_raw_url, file_path))

        # Collect failed stream rows for reporting
        failed_streams = []
        for url, err in failed_downloads.items():
            for s in url_to_streams[url]:
                failed_streams.append((s['id'], s['stream_display_name'], url, err))

        print(f"\nPrepared {len(stream_updates)} database row updates across {len(url_to_filename)} logo files.")

        # Show samples in dry-run or live
        print("\nSample proposed mappings (first 10):")
        for upd in stream_updates[:10]:
            print(f"  Stream ID {upd[0]:<5d} | {upd[1][:25]:<25} | {os.path.basename(upd[4])}")
            print(f"    Old: {upd[2]}")
            print(f"    New: {upd[3]}")

        if args.dry_run:
            print("\n[DRY RUN] Complete. No Git push or database modifications were performed.")
            _print_summary(total_matched, already_migrated_count, len(downloaded_data),
                           len(failed_downloads), 0, 0, 0, failed_streams)
            return

        # 6. Push to GitHub
        print("\nPushing downloaded logos to GitHub...")
        files_pushed = push_to_github(repo_dir, branch, f"Add {len(url_to_filename)} channel logos")
        print(f"GitHub files pushed/synchronized: {files_pushed}")

        # 7. Verify public raw URLs
        print("\nVerifying public GitHub raw URLs...")
        sample_to_verify = list({upd[3] for upd in stream_updates})[:20]
        verified_ok = 0
        for raw_u in sample_to_verify:
            if verify_raw_url(raw_u, retries=5, delay=2.0):
                verified_ok += 1
            else:
                print(f"  Warning: raw URL check failed for: {raw_u}")
        print(f"Verified {verified_ok}/{len(sample_to_verify)} sampled raw URLs reachable via HTTP 200.")

        if verified_ok == 0 and len(sample_to_verify) > 0:
            raise RuntimeError("Raw GitHub URLs verification failed. Aborting database update for safety.")

        # 8. Save local CSV backup mapping BEFORE database update
        backup_csv_path = os.path.join(repo_dir, args.backup_file)
        print(f"\nCreating local backup mapping at: {backup_csv_path}")
        with open(backup_csv_path, mode="w", newline="", encoding="utf-8") as bf:
            writer = csv.writer(bf)
            writer.writerow(["stream_id", "stream_display_name", "old_stream_icon", "new_stream_icon", "migrated_at"])
            now_iso = datetime.datetime.now().isoformat()
            for upd in stream_updates:
                writer.writerow([upd[0], upd[1], upd[2], upd[3], now_iso])
        print(f"Backup saved ({len(stream_updates)} rows).")

        # 9. Update MySQL database
        print("\nUpdating MySQL streams table...")
        rows_updated = 0
        update_failures = 0

        # Execute updates in parameterized batches
        update_sql = "UPDATE streams SET stream_icon = %s WHERE id = %s;"
        batch_size = 200
        for i in range(0, len(stream_updates), batch_size):
            batch = [(upd[3], upd[0]) for upd in stream_updates[i:i + batch_size]]
            try:
                with db_conn.cursor() as cur:
                    cur.executemany(update_sql, batch)
                db_conn.commit()
                rows_updated += len(batch)
                print(f"  Updated {rows_updated}/{len(stream_updates)} rows...")
            except Exception as e:
                db_conn.rollback()
                print(f"  Error updating batch {i}-{i+len(batch)}: {e}")
                update_failures += len(batch)

        # 10. Post-update verification from database
        print("\nVerifying database updates...")
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT id, stream_display_name, stream_icon
                FROM streams
                WHERE type IN (1, 3, 4)
                  AND stream_icon LIKE %s
                LIMIT 5;
            """, (f"{raw_url_prefix}%",))
            verified_sample = cur.fetchall()

        print(f"Sample of verified database rows pointing to GitHub:")
        for r in verified_sample:
            print(f"  ID {r['id']:<5d} | {r['stream_display_name']:<25} | {r['stream_icon']}")

        # 11. Final Summary Report
        _print_summary(total_matched, already_migrated_count, len(downloaded_data),
                       len(failed_downloads), len(url_to_filename), rows_updated,
                       update_failures, failed_streams)

    finally:
        db_conn.close()


def _print_summary(total_matched, already_migrated, dl_success, dl_failed,
                   pushed_count, db_updated, db_failures, failed_streams):
    print("\n==================================================", flush=True)
    print("                 FINAL REPORT                     ", flush=True)
    print("==================================================", flush=True)
    print(f"Streams matched: {total_matched}", flush=True)
    print(f"Already migrated: {already_migrated}", flush=True)
    print(f"Downloads successful: {dl_success}", flush=True)
    print(f"Downloads failed: {dl_failed}", flush=True)
    print(f"GitHub files pushed: {pushed_count}", flush=True)
    print(f"Database rows updated: {db_updated}", flush=True)
    print(f"Database update failures: {db_failures}", flush=True)
    print("==================================================", flush=True)

    if failed_streams:
        print(f"\nFailed Streams ({len(failed_streams)} rows):", flush=True)
        for stream_id, name, url, err in failed_streams[:30]:
            safe_name = (name or "").encode("ascii", "replace").decode("ascii")
            safe_err = str(err).encode("ascii", "replace").decode("ascii")
            print(f"  Stream ID {stream_id:5d} | {safe_name[:20]:<20} | Reason: {safe_err} | URL: {url}", flush=True)
        if len(failed_streams) > 30:
            print(f"  ... and {len(failed_streams) - 30} more failed streams.", flush=True)


if __name__ == "__main__":
    main()
