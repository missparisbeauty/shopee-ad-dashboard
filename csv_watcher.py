"""
CSV 資料夾自動上傳監看
─────────────────────────
責任：定期掃描 watch/ 下的子資料夾，把新 CSV 自動解析、寫入 store、
     成功 → 移到 processed/，失敗 → 移到 failed/

資料夾結構（自動建立）：
    watch/
    ├── S001/                    ← 子資料夾名稱 = shop
    │   ├── 2026-04-25.csv      ← 新檔
    │   ├── processed/           ← 處理成功
    │   └── failed/              ← 處理失敗
    └── 自家3C旗艦店/

對外公開函式：
    start(loop_interval=5)      # 啟動背景 task（asyncio）
    stop()
    scan_once() → ScanResult    # 立即掃一次
    status() → dict
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import ad_report_parser
import ad_data_store

from paths import DATA_DIR
ROOT = DATA_DIR
WATCH_DIR = ROOT / "watch"  # 主要監看目錄（永遠存在）
STATE_FILE = ROOT / "watcher_state.json"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_RECENT = 30                     # 紀錄最近處理的 N 筆
ACCEPTED_EXTS = (".csv", ".tsv", ".txt")
SAFE_SHOP_RE = re.compile(r"[\x00-\x1f\x7f/\\<>\"'`\r\n\t]")  # 與 server._safe_label 同步

_lock = Lock()
_state: dict[str, Any] = {
    "enabled": False,
    "watch_dir": str(WATCH_DIR),
    "extra_watch_dirs": [],   # 額外監看目錄（如 GDrive 同步路徑）
    "scan_interval": 5,
    "last_scan_at": None,
    "scan_count": 0,
    "recent": [],
}
_task: asyncio.Task | None = None


# ─────────────────────────── State 持久化 ───────────────────────────

def _load_state() -> None:
    global _state
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _state.update({k: v for k, v in saved.items() if k != "enabled"})
            # enabled 不從檔案讀，避免重啟後狀態混亂；要明確 start
        except (json.JSONDecodeError, OSError):
            pass


def _save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _add_recent(entry: dict) -> None:
    with _lock:
        _state["recent"].insert(0, entry)
        _state["recent"] = _state["recent"][:MAX_RECENT]


# ─────────────────────────── 安全：路徑/shop 名檢查 ───────────────────────────

def _safe_shop_name(name: str) -> str | None:
    """子資料夾名 → shop label。不通過則回 None。"""
    n = name.strip()
    if not n or len(n) > 64:
        return None
    if SAFE_SHOP_RE.search(n):
        return None
    if n.startswith(".") or n in ("processed", "failed"):
        return None
    return n


def _ensure_dirs() -> None:
    WATCH_DIR.mkdir(exist_ok=True)


def _all_watch_roots() -> list[Path]:
    """主目錄 + 所有額外目錄（過濾不存在的）"""
    roots = [WATCH_DIR]
    for d in _state.get("extra_watch_dirs", []):
        try:
            p = Path(d)
            if p.exists() and p.is_dir():
                roots.append(p)
        except (OSError, ValueError):
            continue
    return roots


def add_watch_dir(path: str) -> dict:
    """新增一個監看目錄。回傳新狀態。"""
    p = Path(path).expanduser()
    if not p.exists() or not p.is_dir():
        raise ValueError(f"路徑不存在或不是資料夾：{path}")
    p_resolved = str(p.resolve())
    with _lock:
        existing = _state.get("extra_watch_dirs", [])
        if p_resolved not in existing and p_resolved != str(WATCH_DIR.resolve()):
            existing.append(p_resolved)
            _state["extra_watch_dirs"] = existing
            _save_state()
    return status()


def remove_watch_dir(path: str) -> dict:
    p_resolved = str(Path(path).expanduser().resolve())
    with _lock:
        existing = _state.get("extra_watch_dirs", [])
        _state["extra_watch_dirs"] = [d for d in existing if d != p_resolved]
        _save_state()
    return status()


# ─────────────────────────── 處理單一檔案 ───────────────────────────

@dataclass
class FileResult:
    file: str
    shop: str
    status: str           # "ok" / "skip" / "fail"
    upload_id: str | None = None
    rows: int = 0
    error: str | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _process_file(filepath: Path, shop: str) -> FileResult:
    rel = filepath.relative_to(WATCH_DIR).as_posix()
    try:
        size = filepath.stat().st_size
        if size == 0:
            return FileResult(file=rel, shop=shop, status="fail", error="空檔案")
        if size > MAX_FILE_SIZE:
            return FileResult(file=rel, shop=shop, status="fail",
                              error=f"檔案 {size//1024//1024}MB 超過 50MB 上限")

        raw = filepath.read_bytes()
        result = ad_report_parser.parse_csv(raw)
        if not result.rows:
            return FileResult(file=rel, shop=shop, status="fail",
                              error=f"解碼成功（{result.encoding}）但沒有可用資料列")

        upload_id = f"UP-{int(time.time() * 1000)}"
        ad_data_store.save_upload(upload_id, shop, filepath.name, result)
        return FileResult(
            file=rel, shop=shop, status="ok",
            upload_id=upload_id, rows=result.summary.get("row_count", 0),
        )
    except ValueError as e:
        return FileResult(file=rel, shop=shop, status="fail", error=str(e))
    except Exception as e:
        return FileResult(file=rel, shop=shop, status="fail",
                          error=f"{type(e).__name__}: {e}")


def _move_after_process(filepath: Path, shop_dir: Path, success: bool) -> Path | None:
    """成功移到 processed/，失敗移到 failed/。回傳新路徑（None = 移動失敗）"""
    target_dir = shop_dir / ("processed" if success else "failed")
    target_dir.mkdir(exist_ok=True)
    # 加 timestamp 避免同名覆蓋
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    new_name = f"{filepath.stem}.{stamp}{filepath.suffix}"
    target = target_dir / new_name
    try:
        filepath.rename(target)
        return target
    except OSError:
        return None


# ─────────────────────────── 主掃描邏輯 ───────────────────────────

@dataclass
class ScanResult:
    scanned: int
    processed: int
    succeeded: int
    failed: int
    skipped: int
    files: list[FileResult] = field(default_factory=list)


def _process_root(root: Path, out: ScanResult) -> None:
    """處理單一監看根目錄下所有子資料夾的新檔"""
    for shop_dir in sorted(root.iterdir()):
        if not shop_dir.is_dir():
            continue
        shop = _safe_shop_name(shop_dir.name)
        if not shop:
            continue
        for f in sorted(shop_dir.iterdir()):
            if not f.is_file():
                continue
            if f.suffix.lower() not in ACCEPTED_EXTS:
                continue
            out.scanned += 1
            res = _process_file_in_root(f, shop, root)
            out.processed += 1
            if res.status == "ok":
                out.succeeded += 1
            elif res.status == "fail":
                out.failed += 1
            else:
                out.skipped += 1
            new_path = _move_after_process(f, shop_dir, success=(res.status == "ok"))
            if new_path:
                try:
                    res.file = new_path.relative_to(root).as_posix()
                except ValueError:
                    res.file = str(new_path)
            out.files.append(res)
            _add_recent(asdict(res))


def _process_file_in_root(filepath: Path, shop: str, root: Path) -> FileResult:
    """跟 _process_file 邏輯一樣，但 file path 顯示成相對 root"""
    try:
        rel = filepath.relative_to(root).as_posix()
    except ValueError:
        rel = str(filepath)
    try:
        size = filepath.stat().st_size
        if size == 0:
            return FileResult(file=rel, shop=shop, status="fail", error="空檔案")
        if size > MAX_FILE_SIZE:
            return FileResult(file=rel, shop=shop, status="fail",
                              error=f"檔案 {size//1024//1024}MB 超過 50MB 上限")

        raw = filepath.read_bytes()
        result = ad_report_parser.parse_csv(raw)
        if not result.rows:
            return FileResult(file=rel, shop=shop, status="fail",
                              error=f"解碼成功（{result.encoding}）但沒有可用資料列")

        # 偵測重複（同 shop + 同檔名 + 同期間）→ 移到 failed/ 不重複進 store
        dup = ad_data_store.find_duplicate_upload(
            shop=shop, filename=filepath.name,
            period_start=result.summary.get("report_period_start"),
            period_end=result.summary.get("report_period_end"),
        )
        if dup:
            return FileResult(
                file=rel, shop=shop, status="fail",
                error=f"重複上傳：{dup['id']} 已於 {dup['uploaded_at'][:19]} 處理過此期間資料"
            )

        upload_id = f"UP-{int(time.time() * 1000)}"
        ad_data_store.save_upload(upload_id, shop, filepath.name, result)
        return FileResult(
            file=rel, shop=shop, status="ok",
            upload_id=upload_id, rows=result.summary.get("row_count", 0),
        )
    except ValueError as e:
        return FileResult(file=rel, shop=shop, status="fail", error=str(e))
    except Exception as e:
        return FileResult(file=rel, shop=shop, status="fail",
                          error=f"{type(e).__name__}: {e}")


def scan_once() -> ScanResult:
    """掃所有 watch roots（主 + extra），處理所有未處理過的 CSV"""
    _ensure_dirs()
    out = ScanResult(scanned=0, processed=0, succeeded=0, failed=0, skipped=0)
    for root in _all_watch_roots():
        _process_root(root, out)
    with _lock:
        _state["last_scan_at"] = datetime.now(timezone.utc).isoformat()
        _state["scan_count"] += 1
        _save_state()
    return out


# ─────────────────────────── 背景 task 控制 ───────────────────────────

async def _loop():
    while _state.get("enabled"):
        try:
            scan_once()
        except Exception:
            pass
        # 用 sleep 而非 wait_for，方便外部 stop() 時 task cancel
        await asyncio.sleep(_state.get("scan_interval", 5))


def start(scan_interval: int = 5) -> dict:
    """啟動背景監看 task。重複呼叫安全（idempotent）"""
    global _task
    _ensure_dirs()
    with _lock:
        _state["enabled"] = True
        _state["scan_interval"] = max(1, min(scan_interval, 3600))
        _save_state()
    if _task and not _task.done():
        return status()
    try:
        loop = asyncio.get_running_loop()
        _task = loop.create_task(_loop())
    except RuntimeError:
        # 不在 async loop 內，呼叫端要在 server startup 時呼叫
        pass
    return status()


def stop() -> dict:
    global _task
    with _lock:
        _state["enabled"] = False
        _save_state()
    if _task and not _task.done():
        _task.cancel()
    return status()


def status() -> dict:
    with _lock:
        # 列出每個 root 下的子資料夾
        roots_status = []
        for root in _all_watch_roots():
            shops_in_root = []
            try:
                for d in sorted(root.iterdir()):
                    if not d.is_dir():
                        continue
                    pending = sum(1 for f in d.iterdir()
                                  if f.is_file() and f.suffix.lower() in ACCEPTED_EXTS)
                    processed = (d / "processed").exists() and sum(
                        1 for f in (d / "processed").iterdir() if f.is_file()) or 0
                    failed = (d / "failed").exists() and sum(
                        1 for f in (d / "failed").iterdir() if f.is_file()) or 0
                    shop = _safe_shop_name(d.name)
                    shops_in_root.append({
                        "name": d.name, "shop": shop,
                        "pending": pending, "processed": processed, "failed": failed,
                        "valid": shop is not None,
                    })
            except (OSError, PermissionError):
                pass
            roots_status.append({
                "path": str(root),
                "is_main": root == WATCH_DIR,
                "exists": root.exists(),
                "shops": shops_in_root,
            })
        # 為了向後相容，仍回傳合併的 shops list
        all_shops = []
        for r in roots_status:
            all_shops.extend(r["shops"])
        return {
            "enabled": _state["enabled"],
            "watch_dir": _state["watch_dir"],
            "extra_watch_dirs": _state.get("extra_watch_dirs", []),
            "roots": roots_status,
            "scan_interval": _state["scan_interval"],
            "last_scan_at": _state["last_scan_at"],
            "scan_count": _state["scan_count"],
            "task_alive": bool(_task and not _task.done()),
            "shops": all_shops,
            "recent": _state["recent"][:10],
        }


# 載入持久化狀態
_load_state()
