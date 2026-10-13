from __future__ import annotations

import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from werkzeug.datastructures import FileStorage

from .config import Limits


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
IGNORED_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__"}


def _safe_relative(name: str) -> Path:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"Unsafe archive path: {name}")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts:
        raise ValueError("Empty archive member path")
    return Path(*parts)


def _check_budget(count: int, total: int, limits: Limits) -> None:
    if count > limits.max_files:
        raise ValueError("Archive contains too many files")
    if total > limits.max_extracted_mb * 1024 * 1024:
        raise ValueError("Archive expands beyond the configured size limit")


def extract_archive(archive: Path, destination: Path, limits: Limits) -> None:
    count = total = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                target = destination / _safe_relative(info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                count += 1
                total += info.file_size
                _check_budget(count, total, limits)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as bundle:
            for info in bundle:
                if info.issym() or info.islnk() or info.isdev():
                    raise ValueError(f"Unsupported archive member: {info.name}")
                target = destination / _safe_relative(info.name)
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not info.isfile():
                    continue
                count += 1
                total += info.size
                _check_budget(count, total, limits)
                target.parent.mkdir(parents=True, exist_ok=True)
                src = bundle.extractfile(info)
                if src is None:
                    continue
                with src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return
    raise ValueError(f"Unsupported or invalid archive: {archive.name}")


def save_uploads(files: list[FileStorage], root: Path, limits: Limits) -> None:
    root.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    for index, upload in enumerate(files, start=1):
        original = Path(upload.filename or f"upload-{index}").name
        name = original or f"upload-{index}"
        if name in used_names:
            stem, suffix = Path(name).stem, Path(name).suffix
            name = f"{stem}-{index}{suffix}"
        used_names.add(name)
        target = root / name
        upload.save(target)
        if target.stat().st_size > limits.max_upload_mb * 1024 * 1024:
            raise ValueError(f"Upload exceeds configured size limit: {original}")
        lower = name.lower()
        if lower.endswith(ARCHIVE_SUFFIXES):
            extraction_dir = root / f"{Path(name).name}.contents"
            extraction_dir.mkdir()
            extract_archive(target, extraction_dir, limits)
            target.unlink()


def list_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts)
    )

