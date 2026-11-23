#!/usr/bin/env python3
"""MediaAutoArchive MVP: safe, content-addressed local media importer for macOS."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

VERSION = "0.1.1"
APP_DIR = Path(os.environ.get("MEDIA_ARCHIVE_HOME", str(Path.home() / "Library/Application Support/MediaAutoArchive"))).expanduser()
SETTINGS = APP_DIR / "settings.json"
DB_PATH = APP_DIR / "history.sqlite3"
MARKER_NAME = ".media-archive-target.json"
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".dng", ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".rw2", ".orf", ".pef", ".srw", ".raw", ".jxl", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".webm", ".wmv", ".insv"}
IGNORE_DIRS = {".trashes", ".spotlight-v100", ".fseventsd", ".temporaryitems", ".documentrevisions-v100", ".appledouble", "__macosx", "@eadir", "system volume information"}
CHUNK = 4 * 1024 * 1024
log = logging.getLogger("media-archive")


class ImportErrorSafe(Exception):
    pass


def setup_log():
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(APP_DIR / "activity.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(stdout)


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_settings():
    if not SETTINGS.is_file():
        raise ImportErrorSafe("尚未设置保存位置。请先运行安装或使用 configure 命令。")
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not data.get("target_path") or not data.get("target_id"):
            raise ValueError("missing fields")
        return data
    except (ValueError, OSError) as e:
        raise ImportErrorSafe(f"配置文件无效：{e}") from e


def choose_folder():
    if sys.platform != "darwin":
        raise ImportErrorSafe("图形文件夹选择仅支持 macOS；可以通过 --target 指定路径。")
    proc = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "请选择图片、视频的归档根目录")'], capture_output=True, text=True)
    if proc.returncode:
        raise ImportErrorSafe("已取消选择目录，配置未改变。")
    return proc.stdout.strip()


def mountpoint_for(path: Path):
    p = path.resolve(strict=True)
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return p


def volume_plist(mount: Path):
    if sys.platform != "darwin":
        return {}
    try:
        p = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(mount)], capture_output=True, timeout=15)
        if p.returncode:
            return {}
        return plistlib.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def configure(target: str | None):
    path = Path(target or choose_folder()).expanduser()
    if not path.is_dir():
        raise ImportErrorSafe(f"保存目录不存在：{path}（请先创建并挂载目标磁盘）")
    if path.is_symlink():
        raise ImportErrorSafe("保存目录不能是符号链接，请选择实际目录。")
    path = path.resolve(strict=True)
    if path == Path("/") or path == Path("/Volumes"):
        raise ImportErrorSafe("不能选择磁盘根目录或 /Volumes 作为归档目录。")
    # /Volumes/X may exist as a regular folder when a target drive is missing.
    if str(path).startswith("/Volumes/"):
        mount = mountpoint_for(path)
        if mount == Path("/"):
            raise ImportErrorSafe("目标外接磁盘尚未正确挂载，不能配置为普通本地文件夹。")
    if not os.access(path, os.W_OK):
        raise ImportErrorSafe("保存位置无写入权限。")
    marker_path = path / MARKER_NAME
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            target_id = str(uuid.UUID(marker["target_id"]))
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ImportErrorSafe(f"归档标记损坏，请人工检查 {marker_path}：{e}") from e
    else:
        target_id = str(uuid.uuid4())
        atomic_json(marker_path, {"schema": 1, "target_id": target_id, "tool": "MediaAutoArchive"})
    existing = {}
    if SETTINGS.exists():
        existing = read_settings()
    data = {
        "version": 1,
        "target_path": str(path),
        "target_id": target_id,
        "source_mode": existing.get("source_mode", "removable"),
        "allowed_volume_names": existing.get("allowed_volume_names", []),
    }
    atomic_json(SETTINGS, data)
    log.info("归档保存位置已设置：%s", path)
    log.info("防误复制：默认自动识别可移动介质；其他外接设备请用 allow-source 手动加入白名单。")


def checked_target(cfg: dict):
    path = Path(cfg["target_path"])
    # Before any mkdir/copy: absolute location and sentinel must exist.
    if not path.is_dir() or path.is_symlink():
        raise ImportErrorSafe("归档目录不存在或已变成符号链接。请检查目标磁盘是否连接。")
    actual = path.resolve(strict=True)
    if str(path).startswith("/Volumes/") and mountpoint_for(path) == Path("/"):
        raise ImportErrorSafe("目标外接硬盘未挂载，已停止导入，防止写入本机同名路径。")
    marker_path = path / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError) as e:
        raise ImportErrorSafe("找不到有效的归档身份标记，目标盘可能已经更换；已停止导入。") from e
    if marker.get("target_id") != cfg["target_id"]:
        raise ImportErrorSafe("目标文件夹身份与配置不一致，已停止导入，避免写入错误磁盘。")
    if not os.access(path, os.W_OK):
        raise ImportErrorSafe("归档目标不可写。")
    return actual


def sha256_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for data in iter(lambda: src.read(CHUNK), b""):
            h.update(data)
    return h.hexdigest()


def db_connect():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS files (
      target_id TEXT NOT NULL,
      sha256 TEXT NOT NULL,
      relative_path TEXT NOT NULL,
      size INTEGER NOT NULL,
      mtime_ns INTEGER NOT NULL,
      PRIMARY KEY (target_id, sha256)
    );
    CREATE TABLE IF NOT EXISTS indexed_targets (
      target_id TEXT PRIMARY KEY, indexed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      completed_at TEXT NOT NULL,
      source TEXT NOT NULL,
      imported INTEGER NOT NULL,
      skipped INTEGER NOT NULL,
      failed INTEGER NOT NULL,
      copied_bytes INTEGER NOT NULL
    );
    """)
    return con


def media_type(path: Path):
    ext = path.suffix.lower()
    return "图片" if ext in PHOTO_EXT else ("视频" if ext in VIDEO_EXT else None)


def walk_media(root: Path):
    def onerror(err):
        raise ImportErrorSafe(f"扫描目录失败：{err}")
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and d.lower() not in IGNORE_DIRS and not (Path(base) / d).is_symlink()]
        for name in sorted(names):
            if name.startswith("."):
                continue
            p = Path(base) / name
            if not p.is_symlink() and p.is_file() and media_type(p):
                yield p


def valid_date(s):
    if not isinstance(s, str):
        return None
    m = re.search(r"(?<!\d)(\d{4})[-:](\d{2})[-:](\d{2})(?!\d)", s)
    if not m:
        return None
    try:
        d = dt.date(*map(int, m.groups()))
        if dt.date(1980, 1, 1) <= d <= dt.date.today() + dt.timedelta(days=2):
            return d.isoformat()
    except ValueError:
        pass
    return None


def video_day(path: Path):
    # ExifTool handles QuickTime metadata more consistently, but is optional.
    exiftool = shutil.which("exiftool")
    if exiftool:
        try:
            r = subprocess.run([exiftool, "-json", "-api", "QuickTimeUTC=1", "-MediaCreateDate", "-CreateDate", "-TrackCreateDate", "-DateTimeOriginal", str(path)], capture_output=True, text=True, timeout=25)
            if r.returncode == 0:
                info = json.loads(r.stdout)[0]
                for key in ("MediaCreateDate", "CreateDate", "TrackCreateDate", "DateTimeOriginal"):
                    day = valid_date(info.get(key))
                    if day:
                        return day, "exiftool"
        except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
            pass
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["/usr/bin/mdls", "-raw", "-name", "kMDItemContentCreationDate", str(path)], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                day = valid_date(r.stdout)
                if day:
                    return day, "mdls"
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        day = dt.date.fromtimestamp(path.stat().st_mtime)
        if dt.date(1980, 1, 1) <= day <= dt.date.today() + dt.timedelta(days=2):
            return day.isoformat(), "file-mtime"
    except (OSError, ValueError, OverflowError):
        pass
    return "日期待确认", "unknown"


def index_target(con, target: Path, cfg: dict):
    row = con.execute("SELECT 1 FROM indexed_targets WHERE target_id=?", (cfg["target_id"],)).fetchone()
    if row:
        return
    count = 0
    log.info("第一次使用此保存位置：正在建立现有文件的去重索引（大型归档可能需要较长时间）...")
    for category in ("图片", "视频"):
        root = target / category
        if root.is_symlink():
            raise ImportErrorSafe(f"已有归档目录是符号链接，拒绝建立索引：{root}")
        if not root.is_dir():
            continue
        for path in walk_media(root):
            if media_type(path) != category:
                continue
            try:
                digest = sha256_file(path)
                st = path.stat()
                rel = str(path.relative_to(target))
                con.execute("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?)", (cfg["target_id"], digest, rel, st.st_size, st.st_mtime_ns))
                count += 1
            except OSError as e:
                raise ImportErrorSafe(f"索引现有文件失败：{path}: {e}") from e
    con.execute("INSERT OR REPLACE INTO indexed_targets VALUES (?, ?)", (cfg["target_id"], dt.datetime.now().isoformat(timespec="seconds")))
    con.commit()
    log.info("现有文件索引完成：%s 个文件", count)


def known_copy(con, cfg, target, digest):
    row = con.execute("SELECT relative_path, size, mtime_ns FROM files WHERE target_id=? AND sha256=?", (cfg["target_id"], digest)).fetchone()
    if not row:
        return None
    rel, size, mtime_ns = row
    # Reject forged/out-of-tree paths even when DB was manually modified.
    dst = (target / rel).resolve()
    if not dst.is_relative_to(target) or not dst.is_file():
        con.execute("DELETE FROM files WHERE target_id=? AND sha256=?", (cfg["target_id"], digest))
        return None
    try:
        st = dst.stat()
        # Never trust file size + mtime alone: bytes may change while metadata
        # remains unchanged (e.g. a restored/corrupted backup).
        if st.st_size == size and sha256_file(dst) == digest:
            if st.st_mtime_ns != mtime_ns:
                con.execute("UPDATE files SET mtime_ns=? WHERE target_id=? AND sha256=?", (st.st_mtime_ns, cfg["target_id"], digest))
            return dst
    except OSError:
        pass
    con.execute("DELETE FROM files WHERE target_id=? AND sha256=?", (cfg["target_id"], digest))
    return None


def record_file(con, cfg, target, digest, path):
    st = path.stat()
    con.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)", (cfg["target_id"], digest, str(path.relative_to(target)), st.st_size, st.st_mtime_ns))
    con.commit()


def nonconflicting_path(directory: Path, name: str, digest: str):
    original = directory / name
    if not original.exists() and not original.is_symlink():
        return original
    src = Path(name)
    stem, extension = src.stem, src.suffix
    for counter in range(10000):
        extra = "" if counter == 0 else f"_{counter}"
        candidate = directory / f"{stem}__{digest[:10]}{extra}{extension}"
        # If a previous process copied the file but crashed before recording
        # the database row, return that existing candidate for deduplication.
        if ((not candidate.exists() and not candidate.is_symlink())
                or (not candidate.is_symlink() and candidate.is_file() and sha256_file(candidate) == digest)):
            return candidate
    raise ImportErrorSafe(f"无法生成不冲突的目标文件名：{name}")


def cleanup_partials(target: Path):
    """Remove only our hidden unfinished copies after acquiring the task lock."""
    cleaned = 0
    for category in ("图片", "视频"):
        folder = target / category
        if not folder.is_dir() or folder.is_symlink():
            continue
        def onerror(err):
            raise ImportErrorSafe(f"清理未完成文件失败：{err}")
        for base, dirs, names in os.walk(folder, topdown=True, followlinks=False, onerror=onerror):
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
            if Path(base).stat().st_dev != target.stat().st_dev:
                dirs.clear()
                continue
            for name in names:
                if not name.startswith(".media-archive-partial-"):
                    continue
                p = Path(base) / name
                if p.is_file() and not p.is_symlink():
                    try:
                        p.unlink()
                        cleaned += 1
                    except OSError as e:
                        raise ImportErrorSafe(f"清理上次中断文件失败 {p}: {e}") from e
    if cleaned:
        log.info("已清理 %d 个上次异常中断的隐藏临时文件", cleaned)


def copy_verified(src: Path, dst: Path, digest: str, size: int):
    # Temporary files never look like media. Never overwrite existing destination paths.
    tmp = dst.parent / f".media-archive-partial-{uuid.uuid4().hex}"
    try:
        calculated = hashlib.sha256()
        with src.open("rb") as inp, tmp.open("xb") as out:
            for b in iter(lambda: inp.read(CHUNK), b""):
                out.write(b)
                calculated.update(b)
            out.flush()
            os.fsync(out.fileno())
        if calculated.hexdigest() != digest or tmp.stat().st_size != size:
            raise ImportErrorSafe("复制过程中源文件发生变化，已拒绝写入不完整内容")
        try:
            shutil.copystat(src, tmp)
        except OSError as e:
            # exFAT and some camera cards may not support all POSIX attributes.
            log.warning("文件系统不支持完整保留时间/权限属性，内容继续安全复制：%s", e)
        # APFS supports hard links for atomic publication without overwrite.
        # exFAT often does not: create the final path using O_EXCL, never
        # os.rename() which may silently overwrite an existing file.
        if dst.exists() or dst.is_symlink():
            raise FileExistsError(str(dst))
        try:
            os.link(tmp, dst)
        except OSError as e:
            if isinstance(e, FileExistsError):
                raise
            opened = False
            try:
                with os.fdopen(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                    opened = True
                    with tmp.open("rb") as inp:
                        shutil.copyfileobj(inp, out, CHUNK)
                    out.flush()
                    os.fsync(out.fileno())
                if sha256_file(dst) != digest:
                    raise ImportErrorSafe("回退复制验证失败")
                try:
                    shutil.copystat(tmp, dst)
                except OSError as meta_error:
                    log.warning("文件系统无法完整保留元数据：%s", meta_error)
            except Exception:
                if opened:
                    dst.unlink(missing_ok=True)
                raise
    finally:
        tmp.unlink(missing_ok=True)


def ensure_destination_folder(target: Path, *parts: str):
    """Create only real directories on the configured filesystem."""
    folder = target
    for part in parts:
        folder = folder / part
        if folder.is_symlink():
            raise ImportErrorSafe(f"目标目录是符号链接，已拒绝写入：{folder}")
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or not folder.is_dir() or folder.stat().st_dev != target.stat().st_dev:
            raise ImportErrorSafe(f"目标子目录不在指定磁盘：{folder}")
    return folder


def import_one(con, cfg, target, src):
    # The destination might have been unplugged since the import began.
    checked_target(cfg)
    initial = src.stat()
    if not src.is_file() or src.is_symlink():
        return "skipped", 0
    digest = sha256_file(src)
    latest = src.stat()
    if (initial.st_size, initial.st_mtime_ns) != (latest.st_size, latest.st_mtime_ns):
        raise ImportErrorSafe("扫描时源文件被修改，已跳过")
    if known_copy(con, cfg, target, digest):
        return "skipped", 0
    category = media_type(src)
    if category == "图片":
        folder = ensure_destination_folder(target, "图片")
    else:
        day, origin = video_day(src)
        folder = ensure_destination_folder(target, "视频", day)
        if origin in ("file-mtime", "unknown"):
            log.info("视频拍摄日期缺失：%s，使用 %s", src.name, origin)
    # Avoid directory swaps to symlinks / separate mounts.
    if not folder.resolve().is_relative_to(target) or folder.stat().st_dev != target.stat().st_dev:
        raise ImportErrorSafe("目标子目录已改变或位于其他磁盘，已拒绝复制")
    preferred = folder / src.name
    if preferred.is_file() and sha256_file(preferred) == digest:
        record_file(con, cfg, target, digest, preferred)
        return "skipped", 0
    dst = nonconflicting_path(folder, src.name, digest)
    if dst != preferred and dst.is_file() and sha256_file(dst) == digest:
        record_file(con, cfg, target, digest, dst)
        return "skipped", 0
    checked_target(cfg)
    if shutil.disk_usage(target).free < initial.st_size + 32 * 1024 * 1024:
        raise ImportErrorSafe("目标磁盘剩余空间不足（保留 32 MiB 安全余量）")
    copy_verified(src, dst, digest, initial.st_size)
    if dst.stat().st_size != initial.st_size or sha256_file(dst) != digest:
        # No success record; avoid treating a damaged file as a completed import.
        dst.unlink(missing_ok=True)
        raise ImportErrorSafe("目标文件 SHA-256 校验失败，已清理损坏副本")
    record_file(con, cfg, target, digest, dst)
    log.info("导入：%s → %s", src, dst)
    return "imported", initial.st_size


def import_source(con, cfg, target, source: Path):
    source = source.resolve(strict=True)
    if not source.is_dir() or source == Path("/"):
        raise ImportErrorSafe("来源必须是有效的目录。")
    if source == target or source in target.parents or target in source.parents or source.stat().st_dev == target.stat().st_dev:
        raise ImportErrorSafe("来源和保存位置不可为同一个磁盘或互为子目录，防止递归误复制。")
    summary = {"imported": 0, "skipped": 0, "failed": 0, "bytes": 0}
    log.info("开始导入来源：%s", source)
    for path in walk_media(source):
        try:
            result, size = import_one(con, cfg, target, path)
            summary[result] += 1
            summary["bytes"] += size
        except (OSError, ImportErrorSafe, sqlite3.Error) as e:
            summary["failed"] += 1
            log.error("导入失败：%s：%s", path, e)
    con.execute("INSERT INTO runs(completed_at,source,imported,skipped,failed,copied_bytes) VALUES (?,?,?,?,?,?)",
                (dt.datetime.now().isoformat(timespec="seconds"), str(source), summary["imported"], summary["skipped"], summary["failed"], summary["bytes"]))
    con.commit()
    log.info("导入完成 %s：新增 %d，重复 %d，失败 %d，复制 %.1f MiB", source.name, summary["imported"], summary["skipped"], summary["failed"], summary["bytes"] / 1048576)
    return summary


def allowed_source(p: Path, cfg: dict, target: Path):
    if p == target or p == mountpoint_for(target):
        return False
    if not p.is_dir() or p.is_symlink() or not os.path.ismount(p):
        return False
    if p.stat().st_dev == target.stat().st_dev:
        return False
    if p.name in cfg.get("allowed_volume_names", []):
        return True
    info = volume_plist(p)
    # diskutil reports some devices with a true boolean, others as 'Removable'.
    removable = info.get("RemovableMedia")
    return cfg.get("source_mode") == "removable" and (removable is True or str(removable).lower() in {"removable", "yes", "true"})


def auto_sources(cfg, target):
    root = Path("/Volumes")
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir()) if allowed_source(p, cfg, target)]


@contextlib.contextmanager
def exclusive_lock():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    f = (APP_DIR / "scan.lock").open("a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ImportErrorSafe("另一项导入任务正在运行；本次触发已合并，请稍后查看日志。") from e
        yield
    finally:
        f.close()


def notify(s):
    if sys.platform != "darwin":
        return
    # shell-free osascript; Chinese/quotes escaped through JSON strings.
    safe = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    try:
        subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{safe}" with title "媒体自动归档"'], timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_scan(explicit: str | None = None):
    with exclusive_lock():
        cfg = read_settings()
        target = checked_target(cfg)
        cleanup_partials(target)
        with db_connect() as con:
            index_target(con, target, cfg)
            processed = set()
            summaries = []
            source_errors = 0
            if explicit:
                sources = [Path(explicit)]
                # Manually specified source is deliberately not restricted to removable media.
                for source in sources:
                    summaries.append(import_source(con, cfg, target, source))
            else:
                # A mount can happen while another media card is copying. Rescan
                # sources between passes, without repeating already handled volumes.
                for attempt in range(3):
                    new_sources = [x for x in auto_sources(cfg, target) if str(x) not in processed]
                    for source in new_sources:
                        processed.add(str(source))
                        try:
                            summaries.append(import_source(con, cfg, target, source))
                        except (OSError, ImportErrorSafe) as e:
                            source_errors += 1
                            log.error("来源 %s 处理失败：%s", source, e)
                    if attempt < 2:
                        time.sleep(2)
            if summaries:
                added = sum(x["imported"] for x in summaries)
                skipped = sum(x["skipped"] for x in summaries)
                failed = sum(x["failed"] for x in summaries)
                notify(f"新增 {added} 个，跳过重复 {skipped} 个，失败 {failed} 个")
                if failed or source_errors:
                    raise ImportErrorSafe(f"本轮有 {failed} 个文件和 {source_errors} 个来源处理失败，请查看 activity.log")
            elif source_errors:
                raise ImportErrorSafe(f"本轮有 {source_errors} 个来源处理失败，请查看 activity.log")
            else:
                log.info("未发现符合条件的新媒体设备。USB 固定盘可以通过 allow-source 加入白名单。")


def add_source(name: str, remove=False):
    cfg = read_settings()
    if not name or "/" in name or name in (".", ".."):
        raise ImportErrorSafe("请输入磁盘名称，不要包含路径（如 CAMERA_SD）。")
    names = set(cfg.get("allowed_volume_names", []))
    if remove:
        names.discard(name)
    else:
        names.add(name)
    cfg["allowed_volume_names"] = sorted(names)
    atomic_json(SETTINGS, cfg)
    log.info("自动导入来源白名单：%s", ", ".join(cfg["allowed_volume_names"]) or "（空）")


def show_status():
    cfg = read_settings()
    print(f"MediaAutoArchive v{VERSION}")
    print("保存位置：", cfg["target_path"])
    try:
        checked_target(cfg)
        print("目标状态：可用，身份校验通过")
    except ImportErrorSafe as e:
        print("目标状态：不可用 -", e)
    print("自动来源：可移动介质 + 白名单")
    print("白名单：", ", ".join(cfg["allowed_volume_names"]) or "（空）")
    print("日志文件：", APP_DIR / "activity.log")
    if DB_PATH.is_file():
        with db_connect() as con:
            print("当前目标已索引文件：", con.execute("SELECT COUNT(*) FROM files WHERE target_id=?", (cfg["target_id"],)).fetchone()[0])
            for row in con.execute("SELECT completed_at,source,imported,skipped,failed FROM runs ORDER BY run_id DESC LIMIT 5"):
                print("导入记录：", row)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mac SD / USB 图片和视频自动归档")
    sub = parser.add_subparsers(dest="command", required=True)
    conf = sub.add_parser("configure", help="修改保存位置")
    conf.add_argument("--target", help="指定已存在的保存目录；不指定则弹出选取窗口")
    scan = sub.add_parser("scan", help="运行一次导入")
    scan.add_argument("--source", help="手动指定来源目录（可以是已挂载的磁盘）")
    sub.add_parser("status", help="显示设置和最近记录")
    add = sub.add_parser("allow-source", help="将固定式 USB 磁盘加入自动导入白名单")
    add.add_argument("name", help="磁盘名，例如 CAMERA_SD（不是路径）")
    rem = sub.add_parser("deny-source", help="从来源白名单删除磁盘")
    rem.add_argument("name")
    args = parser.parse_args(argv)
    setup_log()
    try:
        if args.command == "configure":
            configure(args.target)
        elif args.command == "scan":
            run_scan(args.source)
        elif args.command == "status":
            show_status()
        elif args.command == "allow-source":
            add_source(args.name)
        elif args.command == "deny-source":
            add_source(args.name, remove=True)
    except (ImportErrorSafe, OSError, sqlite3.Error) as e:
        log.error("任务停止：%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())