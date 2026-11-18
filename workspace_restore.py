# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# DBTITLE 1,How to use this restore
# MAGIC %md
# MAGIC # Workspace Restore
# MAGIC
# MAGIC Restores notebooks, files and dashboards from **one** workspace backup in the `userfiles` volume. You pick which backup (the date) with `backup_folder` and which user or folder with `source_prefix`. Both are required.
# MAGIC
# MAGIC #### Parameters
# MAGIC
# MAGIC | Parameter | What it does | Example |
# MAGIC | --- | --- | --- |
# MAGIC | `backup_folder` | **Required. Which backup (date).** One timestamped folder in the volume; the name is the backup's UTC start time. No default on purpose, so a restore never picks a backup silently. | `/Volumes/backup/default/userfiles/20261118T200000Z` |
# MAGIC | `source_prefix` | **Required. Which user or folder.** Only objects at or under this path are restored, so only that user's zip is opened. Enter `ALL` to restore everything in the backup (every user + `/Shared`). Blank is refused, so a restore never covers the whole workspace by accident. | `/Users/jane.doe@example.com` or `/Users/jane.doe@example.com/projectX` or `/Shared` or `ALL` |
# MAGIC | `mode` | **Where it goes** (ignored when `target_root` is set). `timestamped` (default) = a side copy under the owner's folder: `/Users/<user>/restore_<backup timestamp>/...`. `original` = the exact original paths (rebuilding after a disaster; users must exist first). | `timestamped` |
# MAGIC | `target_root` | Optional. Restore under this path instead. The `source_prefix` part of each path is replaced by `target_root` (if `source_prefix` is `ALL`, the full original path is placed under it). | `/Users/me@example.com/recovered` |
# MAGIC | `overwrite` | `false` (default) = objects that already exist fail and are listed in the results table; nothing is replaced. `true` = the backup copy replaces existing objects. | `false` |
# MAGIC
# MAGIC #### Finding the backup to use
# MAGIC * Each folder under `/Volumes/<catalog>/<schema>/<volume>/` is one backup, named `YYYYMMDDTHHMMSSZ` (UTC). Pick the newest one on or before the date you need.
# MAGIC * A folder without `_manifest.json` is an incomplete run; the restore refuses it.
# MAGIC * Inside, there is one zip per user (`<email>.zip`) plus `Shared.zip`. You never pick the zip directly: `source_prefix` selects it.
# MAGIC
# MAGIC #### Common recipes
# MAGIC * **One user wants yesterday's copy of their work:** `backup_folder` = that day's folder, `source_prefix` = `/Users/<email>`, `mode` = `timestamped`. They find it in `/Users/<email>/restore_<timestamp>/` and copy back what they need.
# MAGIC * **One folder only:** `source_prefix` = `/Users/<email>/<folder>`.
# MAGIC * **Rebuild the whole workspace:** `source_prefix` = `ALL`, `mode` = `original`, `overwrite` as needed.
# MAGIC
# MAGIC #### Notes
# MAGIC * Every zip and object is checksum-verified before import. Permissions and Git folders are not restored.
# MAGIC * `restore_<timestamp>` folders are skipped by the workspace backup, so restored copies are never backed up again.
# MAGIC * Only the last two cells do the import. The cells before them just install the SDK, set up the parameters and check `backup_folder`.

# COMMAND ----------

# DBTITLE 1,Install SDK
# MAGIC %pip install databricks-sdk --upgrade -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Cell 4
# Parameters (set these widgets by hand; see the first cell). backup_folder and source_prefix have no default on purpose.
dbutils.widgets.text("backup_folder", "")   # one backup: /Volumes/<catalog>/<schema>/<volume>/<timestamp>
dbutils.widgets.text("source_prefix", "")   # required: a path, e.g. /Users/jane.doe@example.com, or ALL for everything
dbutils.widgets.dropdown("mode", "timestamped", ["timestamped", "original"])
dbutils.widgets.text("target_root", "")     # optional: restore under this path instead (overrides mode)
dbutils.widgets.dropdown("overwrite", "false", ["false", "true"])

# COMMAND ----------

# Workspace restore: import notebooks/files from one backup back into the workspace.
#
# Where it restores to:
#   * mode=timestamped (default): under each owner's folder in restore_<backup timestamp>/, e.g.
#       /Users/<user>/restore_20261104T215256Z/...  and  /Shared/restore_20261104T215256Z/...
#     Nothing existing is overwritten; the user copies back what they need.
#   * mode=original: the exact original paths — for rebuilding a workspace after a disaster
#     (users must exist again first; use overwrite=true if the backup copy should win).
#   * target_root: restore under any other path instead (overrides mode).
# source_prefix is required: /Users/<user>, /Users/<user>/<subfolder>, /Shared, or ALL for everything.
#
# Every zip and every object is checksum-verified before import. A backup folder without
# _manifest.json is an incomplete run and is refused. Permissions are not restored.
from databricks.sdk import WorkspaceClient

import workspace_lib as lib

BACKUP_FOLDER = dbutils.widgets.get("backup_folder").strip().rstrip("/")
if not BACKUP_FOLDER.startswith("/Volumes/"):
    raise ValueError("backup_folder is required and must be a /Volumes/... folder of one backup.")

SOURCE_PREFIX = dbutils.widgets.get("source_prefix").strip()
if SOURCE_PREFIX.upper() != "ALL" and (not SOURCE_PREFIX.startswith("/") or SOURCE_PREFIX.rstrip("/") == ""):
    raise ValueError("source_prefix is required: a workspace path such as /Users/<email> or /Shared, "
                     "or ALL to restore everything in the backup.")

w = WorkspaceClient()

# COMMAND ----------

result = lib.restore_workspace(w, backup_folder=BACKUP_FOLDER,
                               source_prefix=SOURCE_PREFIX,
                               target_root=dbutils.widgets.get("target_root").strip(),
                               overwrite=dbutils.widgets.get("overwrite") == "true",
                               mode=dbutils.widgets.get("mode"))

# COMMAND ----------

display(spark.createDataFrame([(r["path"], r.get("restored_to", ""), r["status"], r.get("error", ""))
                               for r in result["results"]],
                              "path string, restored_to string, status string, error string"))
print(f"restored {result['restored']} of {len(result['results'])} object(s) from backup {result['backup']}")
if result["failed"]:
    raise RuntimeError(f"{len(result['failed'])} object(s) failed to restore (see table above). "
                       "If they already exist (e.g. this backup was restored before), rerun with overwrite=true.")
