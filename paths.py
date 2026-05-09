"""
資料路徑解析（cloud-aware）
─────────────────────────
責任：讓所有 JSON 資料檔的路徑可以透過 DATA_DIR 環境變數覆寫。

本機開發  : DATA_DIR 未設 → 用 Path(__file__).parent（跟原行為一致）
Cloud Run : DATA_DIR=/data → 對應到 GCS bucket mount 點，重啟資料不會消失
"""
import os
from pathlib import Path

# 預設用「本檔所在目錄」=專案根目錄
_DEFAULT_DATA_DIR = Path(__file__).parent

# 環境變數覆寫（給 Cloud Run / Docker 用）
_data_dir_env = os.environ.get("DATA_DIR")
DATA_DIR = Path(_data_dir_env) if _data_dir_env else _DEFAULT_DATA_DIR

# 確保 DATA_DIR 存在（Cloud Run mount 時應該已存在；本機不會有問題）
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_path(*parts: str) -> Path:
    """組成 DATA_DIR 下的子路徑，並確保父目錄存在"""
    p = DATA_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
