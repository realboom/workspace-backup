# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %pip install databricks-sdk --upgrade -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
# Parameters (set these widgets by hand, or pass them as job parameters)
dbutils.widgets.text("roots", "/Users,/Shared")              # comma-separated workspace folders to back up
dbutils.widgets.text("excludes", "*/.bundle,*/.bundle/*")    # comma-separated glob patterns to skip
dbutils.widgets.dropdown("include_git_folders", "false", ["false", "true"])
dbutils.widgets.dropdown("include_service_principals", "false", ["false", "true"])  # SP homes: /Users/<applicationId>
dbutils.widgets.text("volume_path", "")        # required: UC volume, e.g. /Volumes/<catalog>/<schema>/<volume>
dbutils.widgets.text("retention_days", "35")   # delete backups older than N days (0 = keep all)

# COMMAND ----------

# DBTITLE 1,Setup
# Workspace backup: export user and shared notebooks/files to the backup volume, one zip per user.
#
#   <volume_path>/<UTC timestamp>/<user>.zip, Shared.zip, _manifest.json
#
# Notebooks are exported in source format; files as-is. Bundle deploy folders (.bundle) are
# skipped (recreated by `bundle deploy`), and so are Git folders unless include_git_folders=true
# (their code lives in Git; turn it on to also catch uncommitted work). Service principal home
# folders (/Users/<applicationId UUID>) are skipped unless include_service_principals=true.
# Restore folders (restore_<UTC timestamp>, e.g. restore_20261105T013831Z) are always skipped.
#
# Runs AS the job's identity, which must be a WORKSPACE ADMIN (to read every user's folder) and
# hold WRITE VOLUME on the backup volume. The run fails at the end if any object failed to export.
#
# RETENTION: backups older than retention_days are deleted, but ONLY when this run had no failed
# objects — a notebook that failed today may only exist in an older backup.
from pathlib import Path

from databricks.sdk import WorkspaceClient

import retention
import workspace_lib as lib

def csv(name: str) -> list[str]:
    return [v.strip() for v in dbutils.widgets.get(name).split(",") if v.strip()]

ROOTS       = csv("roots") or lib.DEFAULT_ROOTS
EXCLUDES    = csv("excludes")
INCLUDE_GIT = dbutils.widgets.get("include_git_folders") == "true"
INCLUDE_SPS = dbutils.widgets.get("include_service_principals") == "true"
VOLUME_PATH = dbutils.widgets.get("volume_path").strip()
RETENTION   = retention.parse_retention(dbutils.widgets.get("retention_days"))

if not VOLUME_PATH.startswith("/Volumes/"):
    raise ValueError(f"volume_path must be a UC volume path (got '{VOLUME_PATH}').")

w = WorkspaceClient()
print(f"backing up {ROOTS} to {VOLUME_PATH} as {w.current_user.me().user_name}")

# COMMAND ----------

# DBTITLE 1,Run backup
manifest = lib.backup_workspace(w, volume_path=VOLUME_PATH, roots=ROOTS, excludes=EXCLUDES,
                                include_git_folders=INCLUDE_GIT, include_service_principals=INCLUDE_SPS)

# COMMAND ----------

# DBTITLE 1,Summary and retention
if manifest["archives"]:
    display(spark.createDataFrame([(a["archive"], a["objects"], a["zip_bytes"]) for a in manifest["archives"]],
                                  "archive string, objects int, zip_bytes long"))
t = manifest["totals"]
print(f"done: {t['exported']} object(s) in {t['archives']} zip(s), {t['failed']} failed, "
      f"{t['skipped']} skipped -> {manifest['folder']}")
# skipped breakdown: entries look like "<path> (<reason>)"; service principal homes are counted, not listed
from collections import defaultdict
by_reason = defaultdict(list)
for s in manifest.get("skipped", []):
    path, _, reason = s.rpartition(" (")
    reason = reason.rstrip(")").split(":")[0].strip() or "unknown"
    by_reason[reason].append(path or s)
for reason, paths in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
    print(f"skipped [{reason}]: {len(paths)}")
    if reason != "service principal home":
        for p in sorted(paths):
            print("   " + p)
failed = [o for o in manifest["objects"] if o["status"] != "ok"]
if failed:
    print(f"retention: skipped because {len(failed)} object(s) failed — older backups are kept")
else:
    pruned = retention.prune(Path(VOLUME_PATH), RETENTION, keep=manifest["timestamp_utc"])
    print(f"retention {RETENTION} day(s): deleted {len(pruned)} old backup folder(s)"
          + ("" if RETENTION else " (pruning off)"))
    for f in pruned:
        print("   deleted " + f)
if failed:
    raise RuntimeError(f"{len(failed)} object(s) failed to export, e.g. "
                       + "; ".join(f"{o['path']}: {o.get('error', '')[:150]}" for o in failed[:5]))
