# Workspace Backup & Restore (notebooks, no bundle)

These notebooks copy every user's home folder (`/Users/<email>`) and `/Shared` into a Unity Catalog volume, and can put any of it back. Notebooks are saved in source format, so they're readable and diffable; files and dashboards are saved as-is. Each backup is a timestamped folder:

```
/Volumes/<catalog>/<schema>/<volume>/<UTC timestamp>/
    _manifest.json        every object backed up: path, type, size, checksum
    <email>.zip           one zip per user home folder
    Shared.zip            /Shared
```

## What is and isn't backed up

| Backed up | Not backed up |
| --- | --- |
| Notebooks (source code, no cell outputs) | Folder and notebook permissions (ACLs) |
| Workspace files | Genie / Databricks One chat history |
| AI/BI dashboards stored in the workspace tree | Jobs, SQL queries, Genie spaces, alerts |
| `/Users/<email>` and `/Shared` | Unity Catalog grants and ownership |
| | Git folders (skipped by default; their code is in Git) |
| | Service principal home folders (skipped by default) |
| | PATs and Git credentials |

This is a safety net for workspace *content*. It doesn't replace identity, permission, or job configuration.

## Files

| File | What it is |
| --- | --- |
| `workspace_backup.py` | Notebook: runs a backup |
| `workspace_restore.py` | Notebook: restores from one backup |
| `workspace_lib.py` | Python module used by both notebooks (must be a workspace **file**, not a notebook) |
| `retention.py` | Python module: deletes old backups (must be a workspace **file**) |

All four must sit in the **same workspace folder**, because the notebooks `import workspace_lib` and `import retention` from their own folder.

## One-time setup

### 1. Create the backup volume
Run in a SQL editor or notebook. Change the names to fit your conventions:

```sql
CREATE CATALOG IF NOT EXISTS backup;
CREATE SCHEMA  IF NOT EXISTS backup.default;
CREATE VOLUME  IF NOT EXISTS backup.default.userfiles;
```

Grant `READ VOLUME` and `WRITE VOLUME` on the volume to whoever runs the backup. Lock it down to admins: the zips contain everyone's notebooks.

### 2. Upload the four files into one folder

**Option A: Git folder** (easiest)
In **Workspace**, go to the folder you want, then **Create → Git folder**, and paste `https://github.com/realboom/workspace-backup`. All four files arrive as the right types. Run the notebooks straight from the Git folder.

**Option B: Databricks CLI**

```bash
git clone https://github.com/realboom/workspace-backup.git
databricks workspace import-dir ./workspace-backup /Workspace/Shared/admin/workspace-backup --profile <profile>
```

**Option C: Workspace UI upload**
In **Workspace**, create a folder (for example `/Workspace/Shared/admin/workspace-backup`), then **⋮ → Import** each of the four `.py` files.

After uploading, check the icons: `workspace_backup` and `workspace_restore` should be **notebooks**, and `workspace_lib.py` and `retention.py` should be **files**. If either module came in as a notebook, delete it and re-import it, or create it with **Create → File** and paste the contents.

### 3. Permissions for whoever runs it
- **Workspace admin** in each workspace being backed up, to read every user's folder.
- `READ VOLUME` and `WRITE VOLUME` on the backup volume.

## Running a backup

1. Open `workspace_backup` and attach **serverless** compute.
2. Set the widgets at the top:

| Widget | Value | Notes |
| --- | --- | --- |
| `volume_path` | `/Volumes/backup/default/userfiles` | **Required.** Blank fails on purpose. |
| `roots` | `/Users,/Shared` | Folders to back up |
| `excludes` | `*/.bundle,*/.bundle/*` | Glob patterns to skip |
| `include_git_folders` | `false` | Set `true` to also catch uncommitted work in Git folders |
| `include_service_principals` | `false` | SP home folders `/Users/<applicationId>` |
| `retention_days` | `35` | Deletes backups older than N days. **0 = keep everything.** |

3. Click **Run all**.
4. Check the last cell's output: `done: N object(s) in N zip(s), 0 failed, ...`
   - If any object fails, the run errors out, lists the failures, and does **not** delete old backups.
   - A backup folder without `_manifest.json` didn't finish. The restore notebook refuses to use it.

One run backs up the workspace the notebook runs in. For several workspaces, upload the files to each one and run each separately. They can all write to the same volume if it's attached to every workspace.

### Optional: run it on a schedule (no bundle)
**Jobs & Pipelines → Create → Job**:
- Task type **Notebook**, path = your `workspace_backup` notebook, compute = **Serverless**.
- Add the widget values above as **job parameters**, with the same names.
- **Run as** a workspace-admin service principal that has `WRITE VOLUME`.
- Add a schedule, for example daily at 2:00 AM, and a failure notification.

## Restoring

Open `workspace_restore`, attach serverless, set the widgets, and **Run all**.

| Widget | Value |
| --- | --- |
| `backup_folder` | **Required.** One backup, for example `/Volumes/backup/default/userfiles/20261119T230000Z`. Pick the newest one taken before the change. |
| `source_prefix` | **Required.** `/Users/<email>`, `/Users/<email>/<subfolder>`, `/Shared`, or `ALL` |
| `mode` | `timestamped` (default) restores a side copy at `/Users/<email>/restore_<timestamp>/...`. `original` restores to the exact original paths. |
| `target_root` | Optional. Restores under this path instead. Overrides `mode`. |
| `overwrite` | `false` (default) never replaces anything. `true` lets the backup copy replace existing objects. |

Every zip and object is checksum-verified before import. Results are listed in a table at the end.

### Common recipes
- **One user wants their work back:** `source_prefix=/Users/<email>`, `mode=timestamped`. They'll find it in `/Users/<email>/restore_<timestamp>/` and copy back what they need.
- **One folder:** `source_prefix=/Users/<email>/<folder>`.
- **Rebuild a whole workspace:** `source_prefix=ALL`, `mode=original`. Users must already exist.

## During an email / domain migration

Example: users are renamed from `@old-domain.com` to `@example.com`.

1. **Run a fresh backup on migration day**, after users stop working and **before** the identity provider domain change starts.
   - Set `retention_days = 0` so no older backups are deleted during the change window.
   - Confirm `0 failed` and write down the backup folder name (the timestamp).
2. **Restoring a user after the rename:** the backup is filed under the **old** address (`/Users/jane.doe@old-domain.com`). If their home folder is now `/Users/jane.doe@example.com`, restore into the new home with `target_root`:
   - `source_prefix = /Users/jane.doe@old-domain.com`
   - `target_root  = /Users/jane.doe@example.com/restore_pre_migration`
   - `overwrite    = false`

   Don't use `mode=original` for renamed users. It would recreate the old `@old-domain.com` path.
3. Remember what this doesn't cover: **chat history, permissions, jobs, and UC grants** depend on the identity migration itself, not on this backup.
