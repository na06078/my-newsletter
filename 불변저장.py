"""완성된 바이트를 기존 대상을 교체하지 않고 한 번만 게시한다."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_bytes_once(path: Path, data: bytes) -> None:
    """같은 파일시스템의 완성본만 게시한다. 경로/내용 정책은 호출자가 검증한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".불변저장-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # replace는 기존 자료를 지우므로 사용하지 않는다. 경쟁 시 한 작성자만 성공한다.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
