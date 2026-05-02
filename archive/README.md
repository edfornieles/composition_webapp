# Archived files (not used by the running app)

These items were moved here during a **repository cleanup**. Nothing in this folder is imported by Django, Celery, or the URL configuration.

| Location | Contents |
| ---------- | ---------- |
| `root-artifacts/` | Old temp audio/video exports, `image_list.txt`, `test.py` (Flask harness), SQL dump, zip, empty `db.sqlite3`, backup `views.py3425` |
| `legacy-code/` | `views_copy.py`, `settings_copy.py`, misplaced `views_copy_misplaced_in_templates.py` |
| `legacy-templates/` | Duplicate/unused admin templates, unused `admin_dashboard.html`, broken `categories/` templates |

**Security:** If `rabbi.sql` or similar dumps contain real credentials, delete them after you no longer need a backup.

You may delete this entire `archive/` directory once you have confirmed you do not need any of the files.

## Cleanup notes (April 2026)

- Removed root-level macOS AppleDouble artifacts (`._*`) that were not part of the app.
- Repository hygiene policy is documented in `README.md` under **Repository hygiene**.
