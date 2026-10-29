"""설정 로더 — config.yaml(운영) + audience.yaml(편집 기준) + .env(비밀값)."""
from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def load_settings(root: Path = ROOT) -> dict:
    """설정을 읽어 {"cfg": 운영 설정, "audience": 편집 기준, "root": 경로} 로 돌려준다.

    .env 는 환경변수로만 올린다. 값을 반환하거나 출력하지 않는다.
    """
    load_dotenv(root / ".env", override=False)
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    audience = yaml.safe_load((root / "audience.yaml").read_text(encoding="utf-8"))
    return {"cfg": cfg, "audience": audience, "root": root}
