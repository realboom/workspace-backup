"""
Shared helpers for the workspace backup/restore notebooks (workspace_backup.py, workspace_restore.py).

Exports user and shared workspace content (notebooks, files, dashboards) to a UC volume so it
survives losing the workspace, and imports it back.

Layout of one backup:
  <volume>/<UTC timestamp>/
    _manifest.json                    every object: path, type, language, archive, entry, size, sha256
    <user>.zip                        one zip per user home folder (/Users/<user>), folder layout inside
    Shared.zip                        one zip per other root (e.g. /Shared)

  * Notebooks are exported in SOURCE format (.py/.sql/.scala/.r) — readable and diffable; cell
    outputs are not kept. Files are exported as-is.
  * Each zip is built on local disk and copied to the volume in one write. _manifest.json is
    written LAST, so a backup folder without it is an incomplete run and restore refuses it.
  * Not covered: folder/notebook permissions, Git folder history (code lives in Git), and
    objects outside the workspace tree (jobs, Genie spaces, queries) — those come from DABs.

A plain Python module (not a notebook) so both notebooks share it and it can be tested off-platform.
"""

from __future__ import annotations

import base64
import datetime as dt
import fnmatch
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

from databricks.sdk.service.workspace import ExportFormat, ImportFormat, Language, ObjectType

DEFAULT_ROOTS = ["/Users", "/Shared"]
DEFAULT_EXCLUDES = ["*/.bundle", "*/.bundle/*"]   # bundle deploy folders: recreated by `bundle deploy`
EXPORTABLE = {ObjectType.NOTEBOOK, ObjectType.FILE, ObjectType.DASHBOARD}
# Types the SDK's ObjectType enum doesn't know yet (object_type comes back None); exported with ExportFormat.AUTO.
EXPORTABLE_RAW = {"ALERT", "DESIGNER_FILE"}
LANG_EXT = {"PYTHON": "py", "SQL": "sql", "SCALA": "scala", "R": "r"}
MANIFEST = "_manifest.json"
# Service principal home folders are /Users/<applicationId> (a UUID); human users are /Users/<email>.
# Matching the folder name (not the live SP list) also catches folders left by deleted SPs.
SP_HOME = re.compile(r"^/Users/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Folders created by restore_workspace (mode=timestamped): restore_<backup UTC timestamp>, e.g.
# restore_20261104T215256Z. Always skipped so restored copies are never backed up again.
RESTORE_FOLDER = re.compile(r"^restore_\d{8}T\d{6}Z$")


def log(msg: str) -> None:
    print(f"[workspace-backup] {msg}", flush=True)


def _name(v) -> str:
    return getattr(v, "value", v) if v is not None else ""


def _raw_type(w, o) -> str:
    """Object type name, even when the SDK enum doesn't know it (object_type None, e.g. ALERT)."""
    if o.object_type is not None:
        return _name(o.object_type)
    try:
        return w.api_client.do("GET", "/api/2.0/workspace/get-status",
                               query={"path": o.path}).get("object_type", "")
    except Exception:
        return ""


def excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def is_service_principal_home(path: str) -> bool:
    return bool(SP_HOME.match(path.rstrip("/")))


def is_restore_folder(path: str) -> bool:
    return bool(RESTORE_FOLDER.match(PurePosixPath(path).name))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
def walk(w, root: str, excludes: list[str], include_git_folders: bool,
         include_service_principals: bool = False) -> tuple[list, list[str]]:
    """Breadth-first listing so excluded folders (and Git folders) are pruned, not just filtered."""
    objects, skipped, queue = [], [], [root]
    while queue:
        folder = queue.pop(0)
        try:
            children = list(w.workspace.list(folder))
        except Exception as e:  # e.g. a folder deleted mid-walk
            skipped.append(f"{folder} (list failed: {e})")
            continue
        for o in children:
            if excluded(o.path, excludes):
                skipped.append(f"{o.path} (excluded)")
            elif not include_service_principals and is_service_principal_home(o.path):
                skipped.append(f"{o.path} (service principal home)")
            elif o.object_type == ObjectType.DIRECTORY and is_restore_folder(o.path):
                skipped.append(f"{o.path} (restore folder)")
            elif o.object_type == ObjectType.DIRECTORY:
                queue.append(o.path)
            elif o.object_type == ObjectType.REPO:
                if include_git_folders:
                    queue.append(o.path)
                else:
                    skipped.append(f"{o.path} (Git folder — code lives in Git)")
            elif o.object_type in EXPORTABLE:
                objects.append(o)
            else:
                raw = _raw_type(w, o)
                if raw in EXPORTABLE_RAW:
                    o.object_type = raw   # plain string; _name() and export_bytes() (AUTO) handle it
                    objects.append(o)
                else:
                    skipped.append(f"{o.path} ({raw} not exportable)")
    return objects, skipped


def group_of(path: str) -> str:
    """Workspace folder each zip covers: /Users/<user> for home folders, else the top-level folder."""
    parts = PurePosixPath(path).parts  # ('/', 'Users', 'jane@x.org', ...)
    return f"/Users/{parts[2]}" if parts[1] == "Users" and len(parts) > 3 else f"/{parts[1]}"


def archive_name(group: str) -> str:
    """/Users/jane@x.org -> jane@x.org.zip; /Shared -> Shared.zip (emails contain '@', so no collisions)."""
    return f"{PurePosixPath(group).name}.zip"


def entry_name(o, group: str) -> str:
    """Path inside the zip, relative to the group folder. Notebooks get their source extension."""
    rel = PurePosixPath(o.path).relative_to(group)
    if o.object_type == ObjectType.NOTEBOOK:
        rel = rel.with_name(f"{rel.name}.{LANG_EXT.get(_name(o.language), 'txt')}")
    return str(rel)


def export_bytes(w, o) -> bytes:
    if o.object_type == ObjectType.FILE:
        # Workspace files are read from the /Workspace mount: the Export API caps responses at 50 MiB.
        return Path("/Workspace" + o.path).read_bytes()
    fmt = ExportFormat.SOURCE if o.object_type == ObjectType.NOTEBOOK else ExportFormat.AUTO
    return base64.b64decode(w.workspace.export(o.path, format=fmt).content or b"")


def backup_workspace(w, *, volume_path: str, roots: list[str], excludes: list[str],
                     include_git_folders: bool, include_service_principals: bool = False,
                     workers: int = 8) -> dict:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_root = Path(volume_path) / stamp

    objects, skipped = [], []
    for root in roots:
        found, skip = walk(w, root, excludes, include_git_folders, include_service_principals)
        log(f"{root}: {len(found)} object(s) to export, {len(skip)} skipped")
        objects += found
        skipped += skip

    groups: dict[str, list] = defaultdict(list)
    for o in objects:
        groups[group_of(o.path)].append(o)

    entries, archives = [], []
    workdir = Path(tempfile.mkdtemp(prefix="ws-backup-"))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for group, objs in sorted(groups.items()):
                archive = archive_name(group)
                local_zip = workdir / archive
                # exports run in parallel; only this thread writes the zip (zipfile isn't thread-safe)
                futures = {pool.submit(export_bytes, w, o): o for o in objs}
                ok_in_group = 0
                with zipfile.ZipFile(local_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for fut in as_completed(futures):
                        o = futures[fut]
                        entry = {"path": o.path, "object_type": _name(o.object_type),
                                 "language": _name(o.language) or None, "archive": archive,
                                 "entry": entry_name(o, group), "modified_at": getattr(o, "modified_at", None)}
                        try:
                            data = fut.result()
                            zf.writestr(entry["entry"], data)
                            entry.update(status="ok", size_bytes=len(data), sha256=sha256(data))
                            ok_in_group += 1
                        except Exception as e:
                            entry.update(status="failed", error=str(e)[:500])
                        entries.append(entry)
                if ok_in_group:
                    dest_root.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(local_zip, dest_root / archive)   # one write per user
                    archives.append({"archive": archive, "workspace_folder": group, "objects": ok_in_group,
                                     "zip_bytes": local_zip.stat().st_size, "sha256": file_sha256(local_zip)})
                local_zip.unlink()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [e for e in entries if e["status"] != "ok"]
    totals = {"archives": len(archives), "exported": len(entries) - len(failed), "failed": len(failed),
              "skipped": len(skipped), "source_bytes": sum(e.get("size_bytes", 0) for e in entries),
              "zip_bytes": sum(a["zip_bytes"] for a in archives)}
    manifest = {"timestamp_utc": stamp, "roots": roots, "excludes": excludes,
                "include_git_folders": include_git_folders,
                "include_service_principals": include_service_principals, "run_as": w.current_user.me().user_name,
                "totals": totals, "archives": archives, "objects": entries, "skipped": skipped}
    dest_root.mkdir(parents=True, exist_ok=True)
    (dest_root / MANIFEST).write_text(json.dumps(manifest, indent=2, default=str))  # LAST = backup complete
    log(f"OK  {totals['exported']} object(s) in {totals['archives']} zip(s) "
        f"({totals['zip_bytes']:,} bytes zipped from {totals['source_bytes']:,}), "
        f"{totals['failed']} failed, {totals['skipped']} skipped -> {dest_root}")
    manifest["folder"] = str(dest_root)
    return manifest


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
RESTORE_MODES = ("timestamped", "original")


def restore_workspace(w, *, backup_folder: str, source_prefix: str, target_root: str,
                      overwrite: bool, mode: str = "timestamped") -> dict:
    """Import objects from one backup. source_prefix is required: a path limits it (e.g. /Users/jane@x.org
    or a subfolder); ALL restores everything in the backup. Blank is refused so nobody restores
    the whole workspace by accident.

    Where objects go:
      * mode=timestamped (default): back under their owner's folder, in restore_<backup timestamp>/,
        e.g. /Users/jane@x.org/proj/nb -> /Users/jane@x.org/restore_20261104T215256Z/proj/nb.
        Nothing existing is overwritten; the user copies back what they need.
      * mode=original: the exact original paths (workspace rebuild after a disaster).
      * target_root (overrides mode): rewrite the source_prefix to this path."""
    if mode not in RESTORE_MODES:
        raise ValueError(f"mode must be one of {RESTORE_MODES} (got '{mode}')")
    source_prefix = source_prefix.strip()
    if source_prefix.upper() == "ALL":
        source_prefix = "/"
    elif not source_prefix.startswith("/") or source_prefix.rstrip("/") == "":
        raise ValueError("source_prefix is required: a workspace path such as /Users/<email> or /Shared, "
                         f"or ALL to restore everything in the backup (got '{source_prefix}').")
    folder = Path(backup_folder)
    if not (folder / MANIFEST).exists():
        raise RuntimeError(f"{folder} has no {MANIFEST}: it is an incomplete backup, or not a backup folder.")
    manifest = json.loads((folder / MANIFEST).read_text())
    prefix = source_prefix.rstrip("/") or "/"
    chosen = [e for e in manifest["objects"] if e["status"] == "ok"
              and (prefix == "/" or e["path"] == prefix or e["path"].startswith(prefix + "/"))]
    if not chosen:
        raise RuntimeError(f"No objects under {prefix} in backup {manifest['timestamp_utc']}.")
    log(f"{len(chosen)} object(s) under {prefix} in backup {manifest['timestamp_utc']}")

    restore_folder = f"restore_{manifest['timestamp_utc']}"

    def target_path(src: str) -> str:
        if target_root:
            return target_root.rstrip("/") + (src[len(prefix):] if prefix != "/" else src)
        if mode == "original":
            return src
        group = group_of(src)  # /Users/<user> or /Shared
        return f"{group}/{restore_folder}{src[len(group):]}"

    zip_sha = {a["archive"]: a["sha256"] for a in manifest["archives"]}
    by_archive: dict[str, list] = defaultdict(list)
    for e in chosen:
        by_archive[e["archive"]].append(e)

    results, made = [], set()
    for archive, items in sorted(by_archive.items()):
        zpath = folder / archive
        if file_sha256(zpath) != zip_sha.get(archive):
            results += [{"path": e["path"], "status": "failed", "error": f"{archive} checksum mismatch"} for e in items]
            continue
        with zipfile.ZipFile(zpath) as zf:
            for e in items:
                dest = target_path(e["path"])
                result = {"path": e["path"], "restored_to": dest}
                try:
                    data = zf.read(e["entry"])
                    if sha256(data) != e["sha256"]:
                        raise RuntimeError("checksum mismatch")
                    parent = str(PurePosixPath(dest).parent)
                    if parent not in made:
                        w.workspace.mkdirs(parent)
                        made.add(parent)
                    kwargs = {"format": ImportFormat.AUTO}
                    if e["object_type"] == "NOTEBOOK":
                        kwargs = {"format": ImportFormat.SOURCE, "language": Language(e["language"])}
                    w.workspace.import_(dest, content=base64.b64encode(data).decode(), overwrite=overwrite, **kwargs)
                    result["status"] = "ok"
                except Exception as ex:
                    result.update(status="failed", error=str(ex)[:500])
                results.append(result)
    ok = sum(1 for r in results if r["status"] == "ok")
    log(f"restored {ok} of {len(results)} object(s)")
    return {"backup": manifest["timestamp_utc"], "results": results, "restored": ok,
            "failed": [r for r in results if r["status"] != "ok"]}
