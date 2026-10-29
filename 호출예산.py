"""실제 요청 직전 예약하는 재시작 안전 누적 예산. 실패한 예약은 환불하지 않는다."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


class BudgetExceeded(Exception):
    """상한 또는 장부 오류: 기사 단위 대안으로 처리하면 안 되는 치명적 오류."""


_SCHEMA = "CREATE TABLE budget (id INTEGER PRIMARY KEY CHECK(id=1), cap INTEGER NOT NULL CHECK(cap>=0), used INTEGER NOT NULL CHECK(used>=0 AND used<=cap))"
_APP_ID = 1313625927


class RequestBudget:
    def __init__(self, path, limit=65):
        if type(limit) is not int or not 0 <= limit <= 9223372036854775807:
            raise BudgetExceeded("요청 예산 상한은 0 이상의 정수여야 합니다.")
        self.path = Path(path).resolve()
        self.limit = limit
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # 기존 빈 파일도 손상 장부다. 독점 생성으로 덮어쓰기를 막는다.
                with self.path.open("xb"):
                    pass
                fresh = True
            except FileExistsError:
                fresh = False
            with self._connection() as conn:
                if fresh:
                    conn.execute(_SCHEMA)
                    conn.execute("INSERT INTO budget VALUES (1, ?, 0)", (limit,))
                    conn.execute(f"PRAGMA application_id={_APP_ID}")
                    conn.execute("PRAGMA user_version=1")
                self._read(conn)
        except (OSError, sqlite3.Error, ValueError):
            raise BudgetExceeded("요청 예산 장부를 열거나 초기화할 수 없습니다.") from None

    @contextmanager
    def _connection(self):
        try:
            # mode=rw: 실행 중 삭제된 장부를 새 DB로 만들지 않는다.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=30)) as conn:
                conn.execute("PRAGMA synchronous=FULL")
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    yield conn
        except (OSError, sqlite3.Error, ValueError):
            raise BudgetExceeded("요청 예산 장부를 읽거나 기록할 수 없습니다.") from None

    def _read(self, conn):
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if (schema != [("table", "budget", _SCHEMA)]
                or conn.execute("PRAGMA application_id").fetchone() != (_APP_ID,)
                or conn.execute("PRAGMA user_version").fetchone() != (1,)
                or conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]):
            raise BudgetExceeded("요청 예산 장부 형식 또는 무결성이 올바르지 않습니다.")
        rows = conn.execute("SELECT id, cap, used FROM budget").fetchall()
        if len(rows) != 1:
            raise BudgetExceeded("요청 예산 장부의 누적값이 올바르지 않습니다.")
        ident, cap, used = rows[0]
        if (ident != 1 or type(cap) is not int or type(used) is not int
                or cap != self.limit or not 0 <= used <= cap):
            raise BudgetExceeded("요청 예산 장부 상한 또는 누적값이 설정과 다릅니다.")
        return {"limit": cap, "used": used, "remaining": cap - used}

    def snapshot(self):
        with self._connection() as conn:
            return self._read(conn)

    def reserve(self):
        with self._connection() as conn:
            state = self._read(conn)
            if state["remaining"] <= 0:
                raise BudgetExceeded("실제 요청 예산이 소진되었습니다.")
            conn.execute("UPDATE budget SET used=used+1 WHERE id=1")
            state["used"] += 1
            state["remaining"] -= 1
        # 커밋이 끝난 뒤에만 HTTP 요청을 허용한다.
        return state
