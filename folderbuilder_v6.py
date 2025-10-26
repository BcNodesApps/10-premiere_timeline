r"""
folderbuilder_v6.py — robust refactor

Improvements:
1) Central config (config.ini next to script/EXE).
2) PyInstaller-friendly resource_path() for bundled data.
3) No destructive global deletes; explicit overwrites only with --force.
4) Temp build dir; move final output at the end.
5) Copy shared assets (never move) like timelinevids.exe.
6) Safer CSV discovery & age check; or pass --csv.
7) CSV header validation via configurable field names.
8) Absolute paths everywhere; explicit cwd when launching child EXE.
9) Verbose logging + --dry-run.
10) Guard rails for overwrites (--force or config).

Typical runs:
  folderbuilder_v6.exe --dry-run
  folderbuilder_v6.exe --csv "C:\\Users\\johnb\\Downloads\\AppSheet.ViewData.2025-10-26.csv" --force
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def is_frozen() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def script_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    if is_frozen():
        meipass = Path(getattr(sys, "_MEIPASS"))
        p = meipass / relative
        if p.exists():
            return p
    return script_dir() / relative


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("folderbuilder")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)
    return logger


def load_config(cfg_path: Path) -> Dict[str, Dict[str, str]]:
    data: Dict[str, Dict[str, str]] = {}
    if not cfg_path.exists():
        return data
    section = None
    for raw in cfg_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if "=" in line and section:
            k, v = line.split("=", 1)
            data[section][k.strip()] = v.strip()
    return data


def most_recent_csv(search_dir: Path, pattern_prefix: str = "appsheet", max_age_days: int = 14) -> Optional[Path]:
    candidates: List[Tuple[float, Path]] = []
    if not search_dir.exists():
        return None
    for p in search_dir.glob(f"{pattern_prefix}*"):
        if p.is_file() and p.suffix.lower() == ".csv":
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    newest = candidates[0][1]
    age_days = (dt.datetime.now() - dt.datetime.fromtimestamp(candidates[0][0])).days
    if age_days > max_age_days:
        return None
    return newest


def read_csv_rows(csv_path: Path, field_map: Dict[str, str], logger: logging.Logger) -> List[Dict[str, str]]:
    required = ["project", "chapter", "story", "sequence"]
    for r in required:
        if r not in field_map:
            raise ValueError(f"Missing field_map key: {r}")
    rows: List[Dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        logger.info(f"CSV headers: {headers}")
        missing = [field_map[r] for r in required if field_map[r] not in headers]
        if missing:
            raise ValueError(f"CSV missing expected columns: {missing}")
        for row in reader:
            norm = {
                "project": (row.get(field_map["project"]) or "").strip(),
                "chapter": (row.get(field_map["chapter"]) or "").strip(),
                "story": (row.get(field_map["story"]) or "").strip(),
                "sequence": (row.get(field_map["sequence"]) or "").strip(),
            }
            if any(v for v in norm.values()):
                rows.append(norm)
    if not rows:
        raise ValueError("CSV contains no data rows.")
    return rows


def safe_copytree(src: Path, dst: Path, logger: logging.Logger):
    if dst.exists():
        raise FileExistsError(f"Destination already exists: {dst}")
    logger.info(f"Copying template tree -> {dst}")
    shutil.copytree(src, dst)


def ensure_dirs(paths: List[Path], logger: logging.Logger):
    for p in paths:
        if not p.exists():
            logger.info(f"Creating directory: {p}")
            p.mkdir(parents=True, exist_ok=True)


def _find_template_root(extract_dir: Path, logger: logging.Logger, expected: str = "video folder template") -> Optional[Path]:
    exp = expected.lower()
    for p in extract_dir.iterdir():
        if p.is_dir() and p.name.lower().startswith(exp):
            return p
    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(children) == 1:
        nested = children[0]
        for p in nested.iterdir():
            if p.is_dir() and p.name.lower().startswith(exp):
                return p
    for p in extract_dir.rglob("*"):
        if p.is_dir() and p.name.lower().startswith(exp):
            return p
    return None


def sanitize_name(name: str) -> str:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in (" ", "-", "_", ".", "+")).strip()
    return safe or "project"


def build_project(args, logger: logging.Logger) -> None:
    cfg = load_config(script_dir() / "config.ini")
    cfg_paths = cfg.get("paths", {})
    cfg_csv = cfg.get("csv", {})
    cfg_safety = cfg.get("safety", {})
    base_path = Path(args.base_path) if args.base_path else Path(cfg_paths.get("BASE_PATH", script_dir()))
    base_path = base_path.resolve()
    template_zip = Path(args.template_zip) if args.template_zip else Path(cfg_paths.get("TEMPLATE_ZIP", "folderstructure.zip"))
    if not template_zip.is_absolute():
        rp = resource_path(str(template_zip))
        template_zip = rp if rp.exists() else (script_dir() / template_zip).resolve()
    csv_file = Path(args.csv).resolve() if args.csv else most_recent_csv(
        Path(cfg_paths.get("CSV_DIR", Path.home() / "Downloads")),
        pattern_prefix=cfg_csv.get("PATTERN_PREFIX", "appsheet"),
        max_age_days=int(cfg_csv.get("MAX_AGE_DAYS", "14")),
    )
    if not csv_file:
        raise FileNotFoundError("No recent CSV found.")
    field_map = {
        "project": cfg_csv.get("FIELD_PROJECT", "project"),
        "chapter": cfg_csv.get("FIELD_CHAPTER", "chapter"),
        "story": cfg_csv.get("FIELD_STORY", "story"),
        "sequence": cfg_csv.get("FIELD_SEQUENCE", "sequence"),
    }
    logger.info("--- RESOLVED PATHS ---")
    logger.info(f"BASE_PATH   : {base_path}")
    logger.info(f"TEMPLATE_ZIP: {template_zip}")
    logger.info(f"CSV_FILE    : {csv_file}")
    if not template_zip.exists():
        raise FileNotFoundError(f"Template ZIP not found: {template_zip}")
    if not csv_file.exists():
        raise FileNotFoundError(f"CSV not found: {csv_file}")

    if not args.dry_run:
        processed_dir = csv_file.parent / "processed"
        processed_dir.mkdir(exist_ok=True)
        dest_csv = processed_dir / csv_file.name
        try:
            shutil.move(str(csv_file), str(dest_csv))
            logger.info(f"Moved processed CSV to: {dest_csv}")
            csv_file = dest_csv
        except Exception as e:
            logger.warning(f"Could not move CSV: {e}")

    rows = read_csv_rows(csv_file, field_map, logger)
    project_name = rows[0]["project"] or "project"
    safe_project = sanitize_name(project_name)
    logger.info(f'Project (from CSV): "{project_name}" -> folder "{safe_project}"')
    build_root = Path(tempfile.mkdtemp(prefix="build_", dir=str(base_path)))
    extract_dir = build_root / "template_extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template_zip, "r") as zf:
        zf.extractall(extract_dir)
    template_root = _find_template_root(extract_dir, logger, expected=cfg_paths.get("TEMPLATE_ROOT_NAME", "video folder template"))
    if not template_root:
        raise FileNotFoundError('Could not locate "video folder template" inside ZIP.')
    dest_in_build = build_root / safe_project
    safe_copytree(template_root, dest_in_build, logger)
    src_root = dest_in_build / "2. sourcefiles"
    ensure_dirs([src_root / x for x in ["b-roll", "music", "culling", "other"]], logger)

    seen_keys = set()
    chapters_txt: List[str] = []
    for r in rows:
        chap, story, seq_raw = r["chapter"], r["story"], r["sequence"]
        try:
            seq_int = int(float(seq_raw))
        except ValueError:
            seq_int = 0
        key = f"{seq_int:02d}|{(chap or '').lower()}|{(story or '').lower()}"
        if key in seen_keys or (not chap and not story):
            continue
        seen_keys.add(key)
        name_parts = [f"{seq_int:02d}"]
        if chap:
            name_parts.append(chap)
        if story:
            name_parts.append(story)
        folder_name = " - ".join(name_parts)
        target = src_root / folder_name
        if not args.dry_run:
            target.mkdir(parents=True, exist_ok=True)
        logger.info(f"Chapter folder: {target}")
        chapters_line = chap if chap and not story else f"{chap} - {story}" if chap and story else ""
        if chapters_line:
            chapters_txt.append(chapters_line)

    chapters_file = dest_in_build / "1. project files and presets" / "chapters.txt"
    if not args.dry_run:
        chapters_file.parent.mkdir(parents=True, exist_ok=True)
        chapters_file.write_text("\n".join(chapters_txt) + "\n", encoding="utf-8")
    timeline_src = template_root / "1. project files and presets" / "timelinevids.exe"
    timeline_dst = dest_in_build / "1. project files and presets" / "timelinevids.exe"
    if timeline_src.exists():
        if not args.dry_run:
            timeline_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(timeline_src, timeline_dst)
        logger.info(f"Ensured timeline tool: {timeline_dst}")
    final_dest = base_path / safe_project
    if final_dest.exists():
        allow = args.force or cfg_safety.get("AllowOverwrite", "no").lower() in ("yes", "true", "1")
        if not allow:
            raise FileExistsError(f"Final destination exists: {final_dest}.")
        if not args.dry_run:
            shutil.rmtree(final_dest)
    if not args.dry_run:
        shutil.move(str(dest_in_build), str(final_dest))
    if not args.no_launch:
        exe = final_dest / "1. project files and presets" / "timelinevids.exe"
        if exe.exists() and not args.dry_run:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
    if not args.dry_run:
        shutil.rmtree(extract_dir, ignore_errors=True)
        shutil.rmtree(build_root, ignore_errors=True)
    logger.info("Done.")


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description="Build a Premiere-ready folder structure from CSV.")
    p.add_argument("--base-path")
    p.add_argument("--template-zip")
    p.add_argument("--csv")
    p.add_argument("--no-launch", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    logs_dir = script_dir()
    log_path = logs_dir / "folderbuilder.log"
    logger = setup_logger(log_path)
    logger.info("folderbuilder_v6 starting...")
    try:
        args = parse_args(argv)
        build_project(args, logger)
        return 0
    except Exception as e:
        logger.exception(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
