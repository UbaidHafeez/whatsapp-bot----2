"""
WhatsApp Bot - Cloud Version with Improved Deduplication
- Runs via GitHub Actions cron (no internal time check needed)
- Tracks per-group send status
- Fixed scan_posts logic (partial sends now resume correctly)
- Sorted images numerically to avoid ordering bugs
- Sends ALL images in a post folder (not just first 2)
- Added delay between sends to avoid rate limiting
"""

import os
import re
import sys
import time
import base64
import requests
import json
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
POSTS_FOLDER = os.path.join(BASE_DIR, "islamic_posts")
GROUPS_FILE  = os.path.join(BASE_DIR, "groups.txt")
DB_FILE      = os.path.join(BASE_DIR, "posts_db.json")

API_TOKEN   = os.environ.get("ULTRAMSG_TOKEN", "pak8408yn0osmffv")
INSTANCE_ID = os.environ.get("ULTRAMSG_INSTANCE", "instance167704")

# Delay (seconds) between sending images to the same group
SEND_DELAY = 5

# Retry settings for failed sends
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds to wait between retries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────

def natural_sort_key(s):
    """Sort strings with embedded numbers naturally (image2 < image10)."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]


def load_groups():
    if not os.path.exists(GROUPS_FILE):
        log.error("❌ groups.txt not found!")
        return []
    with open(GROUPS_FILE, "r", encoding="utf-8") as f:
        groups = [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]
    log.info(f"📱 Loaded {len(groups)} group(s): {groups}")
    return groups


# ── Database ─────────────────────────────────────────────────

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        # Migrate old boolean-format entries
        migrated = False
        for post_name in list(db.keys()):
            if isinstance(db[post_name], bool):
                db[post_name] = {
                    "sent_to":   [],
                    "failed":    [],
                    "timestamp": datetime.now().isoformat(),
                    "completed": db[post_name]
                }
                migrated = True
                log.info(f"🔄 Migrated old entry: {post_name}")
        if migrated:
            save_db(db)
        return db
    except Exception as e:
        log.error(f"❌ Error loading database: {e}")
        return {}


def save_db(db):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=4)
        log.info("💾 Database saved")
    except Exception as e:
        log.error(f"❌ Error saving database: {e}")


def get_post_status(db, post_name):
    if post_name not in db:
        return {"sent_to": [], "failed": [], "timestamp": None, "completed": False}
    s = db[post_name]
    if isinstance(s, bool):
        db[post_name] = {"sent_to": [], "failed": [], "timestamp": None, "completed": s}
        save_db(db)
        return db[post_name]
    return s


def is_group_sent(db, post_name, group):
    return group in get_post_status(db, post_name)["sent_to"]


def _ensure_entry(db, post_name):
    if post_name not in db or isinstance(db[post_name], bool):
        db[post_name] = {
            "sent_to":   [],
            "failed":    [],
            "timestamp": datetime.now().isoformat(),
            "completed": False
        }


def mark_group_sent(db, post_name, group):
    _ensure_entry(db, post_name)
    if group not in db[post_name]["sent_to"]:
        db[post_name]["sent_to"].append(group)
    # Remove from failed list if previously failed
    db[post_name]["failed"] = [g for g in db[post_name]["failed"] if g != group]
    log.info(f"✅ Marked '{post_name}' sent → {group}")


def mark_group_failed(db, post_name, group):
    _ensure_entry(db, post_name)
    if group not in db[post_name]["failed"]:
        db[post_name]["failed"].append(group)
    log.warning(f"❌ Marked '{post_name}' failed → {group}")


def is_post_completed(db, post_name, total_groups):
    return len(get_post_status(db, post_name)["sent_to"]) >= total_groups


def mark_post_completed(db, post_name):
    _ensure_entry(db, post_name)
    db[post_name]["completed"] = True
    log.info(f"🏁 Post '{post_name}' completed (all groups received it)")


# ── Post scanning ─────────────────────────────────────────────

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}


def get_images_in_folder(folder_path):
    files = [
        f for f in os.listdir(folder_path)
        if os.path.splitext(f.lower())[1] in IMAGE_EXTS
    ]
    return sorted(files, key=natural_sort_key)


def scan_posts(db, groups):
    """
    Return posts that still need to be sent to at least one group.
    Partial sends are resumed correctly — only fully completed posts are skipped.
    """
    if not os.path.exists(POSTS_FOLDER):
        log.error("❌ Posts folder not found!")
        return []

    posts = []
    for folder in sorted(os.listdir(POSTS_FOLDER), key=natural_sort_key):
        if folder.startswith("sent_"):
            continue
        folder_path = os.path.join(POSTS_FOLDER, folder)
        if not os.path.isdir(folder_path):
            continue

        status = get_post_status(db, folder)

        # Skip only if fully completed AND sent to every group
        if status["completed"] and len(status["sent_to"]) >= len(groups):
            log.info(f"⏭️  '{folder}' fully completed – skipping")
            continue

        images = get_images_in_folder(folder_path)
        if len(images) >= 2:
            posts.append(folder)
            remaining = [g for g in groups if g not in status["sent_to"]]
            log.info(f"📦 '{folder}' — {len(images)} images, needs sending to: {remaining}")
        else:
            log.warning(f"⚠️  '{folder}' — only {len(images)} image(s), need ≥2 – skipping")

    log.info(f"📬 {len(posts)} post(s) pending")
    return posts


# ── Sending ───────────────────────────────────────────────────

def image_to_base64(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def send_image(group_name, image_path, caption=""):
    if not os.path.exists(image_path):
        log.error(f"❌ File not found: {image_path}")
        return False

    file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
    log.info(f"🖼️  {os.path.basename(image_path)} ({file_size_mb:.2f} MB) → {group_name}")

    if file_size_mb > 5:
        log.error("❌ Image exceeds 5 MB limit")
        return False

    image_b64 = image_to_base64(image_path)
    chat_id   = group_name if "@g.us" in group_name else f"{group_name}@c.us"
    api_url   = f"https://api.ultramsg.com/{INSTANCE_ID}/messages/image"

    payload = {
        "token":   API_TOKEN,
        "to":      chat_id,
        "image":   image_b64,
        "caption": caption
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"🔄 Attempt {attempt}/{MAX_RETRIES}...")
            resp = requests.post(api_url, data=payload, timeout=90)
            log.info(f"📡 HTTP {resp.status_code} | {resp.text[:200]}")
            if resp.status_code == 200:
                result = resp.json()
                if result.get("sent") == "true" or result.get("message") == "ok":
                    log.info(f"✅ Sent to {group_name}")
                    return True
                log.error(f"❌ API rejected: {result}")
            else:
                log.error(f"❌ HTTP error {resp.status_code}")
        except requests.exceptions.Timeout:
            log.error(f"❌ Attempt {attempt} timed out (90s)")
        except Exception as e:
            log.error(f"❌ Attempt {attempt} failed: {e}")

        if attempt < MAX_RETRIES:
            log.info(f"⏳ Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)

    log.error(f"❌ All {MAX_RETRIES} attempts failed for {group_name}")
    return False


def send_all_images(group_name, image_paths):
    """
    Send ALL images in a post to a group, with a delay between each.
    Returns True only if every image was sent successfully.
    """
    for i, image_path in enumerate(image_paths):
        caption = "" # Add caption to first image if desired
        ok = send_image(group_name, image_path, caption=caption)
        if not ok:
            log.error(f"❌ Failed on image {i+1}/{len(image_paths)}: {os.path.basename(image_path)}")
            return False
        # Delay between images to avoid rate limiting (skip after last image)
        if i < len(image_paths) - 1:
            log.info(f"⏳ Waiting {SEND_DELAY}s before next image...")
            time.sleep(SEND_DELAY)
    return True


def rename_folder_sent(post_name):
    old = os.path.join(POSTS_FOLDER, post_name)
    new = os.path.join(POSTS_FOLDER, f"sent_{post_name}")
    try:
        if os.path.exists(old):
            os.rename(old, new)
            log.info(f"📁 Renamed: {post_name} → sent_{post_name}")
    except Exception as e:
        log.error(f"❌ Rename failed: {e}")


# ── Main ──────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("🤖 WhatsApp Bot (GitHub Actions / Cloud)")
    log.info("=" * 60)

    # NOTE: No time check here — GitHub Actions cron handles scheduling.
    # To test locally, just run:  python bot.py

    if not API_TOKEN or not INSTANCE_ID:
        log.error("❌ Missing ULTRAMSG_TOKEN or ULTRAMSG_INSTANCE environment variables")
        sys.exit(1)

    log.info(f"🔑 Token: {API_TOKEN[:10]}… | Instance: {INSTANCE_ID}")

    groups = load_groups()
    if not groups:
        log.error("❌ No groups configured in groups.txt")
        sys.exit(1)

    db    = load_db()
    posts = scan_posts(db, groups)

    if not posts:
        log.info("✅ Nothing to send – all posts already delivered!")
        return

    # Process only the first pending post per run (avoids spamming)
    post_name = posts[0]
    log.info(f"📮 Processing post: {post_name}")

    groups_needing_post = [g for g in groups if not is_group_sent(db, post_name, g)]
    if not groups_needing_post:
        log.info(f"✅ '{post_name}' already sent to all groups")
        mark_post_completed(db, post_name)
        save_db(db)
        rename_folder_sent(post_name)
        return

    log.info(f"📤 Sending to: {groups_needing_post}")

    post_folder = os.path.join(POSTS_FOLDER, post_name)
    image_files = get_images_in_folder(post_folder)
    image_paths = [os.path.join(post_folder, f) for f in image_files]

    log.info(f"🖼️  {len(image_files)} image(s) to send per group: {image_files}")

    successful, failed = [], []

    for group in groups_needing_post:
        log.info("-" * 40)
        ok = send_all_images(group, image_paths)

        if ok:
            successful.append(group)
            mark_group_sent(db, post_name, group)
        else:
            failed.append(group)
            mark_group_failed(db, post_name, group)
            log.warning(f"⚠️  Failed for {group}")

        save_db(db)  # Save after each group to avoid data loss on crash

    # ── Summary ──────────────────────────────────────────────
    log.info("=" * 60)
    log.info("📊 SUMMARY")
    if successful:
        log.info(f"✅ Succeeded ({len(successful)}): {successful}")
    if failed:
        log.info(f"❌ Failed    ({len(failed)}): {failed}")

    if is_post_completed(db, post_name, len(groups)):
        mark_post_completed(db, post_name)
        save_db(db)
        rename_folder_sent(post_name)
        log.info(f"🏁 '{post_name}' fully delivered and archived")
    else:
        log.info(f"⚠️  '{post_name}' partially sent – will retry failed groups next run")

    log.info("=" * 60)


if __name__ == "__main__":
    main()
