"""
平台数据库 — SQLite(WAL 模式)+ 表结构 + CRUD

连接串固定格式: sqlite:///<db_path>?journal_mode=WAL&busy_timeout=5000
  - WAL: API 线程与 worker 线程并发读写不互锁
  - busy_timeout: 并发写时等待而非立即报 database is locked

每次操作使用独立连接(本地 SQLite 开销极小),天然避免跨线程连接复用问题。
"""
import hashlib
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("platform.db")

# ---------- 状态与错误分类常量 ----------
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

ERROR_RATE_LIMITED = "rate_limited"   # 限流 / 反爬
ERROR_NETWORK = "network"             # 超时 / 断连
ERROR_SOURCE_EMPTY = "source_empty"   # 源无数据
ERROR_INTERNAL = "internal"           # 代码异常

_db_path: str = None

# update_task 哨兵: 区分「不更新该字段」与「显式置 NULL/0」
_UNSET = object()


def init_db(db_path: str):
    """初始化数据库: 设置路径、建目录、建表(WAL)"""
    global _db_path
    _db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)
    conn = _connect()
    try:
        _create_tables(conn)
        conn.commit()
    finally:
        conn.close()


def reset_db():
    """清空全局路径(测试隔离用)"""
    global _db_path
    _db_path = None


def make_conn_str(db_path: str) -> str:
    """生成标准连接串: sqlite:///<abs>?journal_mode=WAL&busy_timeout=5000"""
    return f"sqlite:///{os.path.abspath(db_path)}?journal_mode=WAL&busy_timeout=5000"


def _parse_conn_str(conn_str: str) -> str:
    """从连接串解析出数据库文件路径"""
    if not conn_str.startswith("sqlite:///"):
        raise ValueError(f"仅支持 sqlite 连接串: {conn_str}")
    rest = conn_str[len("sqlite:///"):]
    if "?" in rest:
        rest = rest.split("?", 1)[0]
    return rest


def _connect() -> sqlite3.Connection:
    """新建连接并应用 WAL / busy_timeout / 外键约束"""
    if _db_path is None:
        raise RuntimeError("数据库未初始化,请先调用 init_db()")
    conn = sqlite3.connect(_db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _create_tables(conn: sqlite3.Connection):
    """建表: tasks + items(与选型书 4.1 一致)"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          TEXT PRIMARY KEY,
            source      TEXT NOT NULL,
            params      TEXT,
            domain      TEXT,
            status      TEXT DEFAULT 'pending',
            created_at  TEXT,
            finished_at TEXT,
            error       TEXT,
            items_count INTEGER DEFAULT 0,
            retries     INTEGER DEFAULT 0,
            callback_url TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            dedup_key  TEXT UNIQUE,
            title      TEXT NOT NULL,
            url        TEXT,
            summary    TEXT,
            source     TEXT,
            domain     TEXT,
            sector     TEXT,
            type       TEXT DEFAULT 'news',
            date_str   TEXT,
            raw_data   TEXT,
            consumed   INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_task ON items(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")


# ---------- 工具函数 ----------

CST = timezone(timedelta(hours=8))  # 与 intel.core.base 同用东八区


def _now() -> str:
    """本地时间字符串(与 SQLite datetime 一致)"""
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def make_task_id() -> str:
    """生成任务 ID: t_<毫秒时间戳>_<随机hex>"""
    return f"t_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def make_dedup_key(source: str, title: str) -> str:
    """跨任务全局去重键: (source, title) 的 MD5"""
    raw = f"{source}|{title}".encode()
    return hashlib.md5(raw).hexdigest()


# ---------- tasks CRUD ----------

def create_task(source: str, params: dict | None = None, domain: str | None = None,
                callback_url: str | None = None, task_id: str | None = None) -> str:
    """创建任务(status=pending),返回 task_id"""
    task_id = task_id or make_task_id()
    import json
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, source, params, domain, status, created_at, callback_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, source, json.dumps(params or {}, ensure_ascii=False),
             domain, STATUS_PENDING, _now(), callback_url),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def get_task(task_id: str) -> dict:
    """按 ID 查任务,不存在返回 None"""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_task(task_id: str, status: str | None = None, error: object = _UNSET,
                finished_at: object = _UNSET, items_count: object = _UNSET,
                retries: object = _UNSET):
    """按需更新任务字段;error/finished_at 等传 None 表示显式清空,不传表示不更新"""
    sets, args = [], []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if error is not _UNSET:
        sets.append("error = ?")
        args.append(error)
    if finished_at is not _UNSET:
        sets.append("finished_at = ?")
        args.append(finished_at)
    if items_count is not _UNSET:
        sets.append("items_count = ?")
        args.append(items_count)
    if retries is not _UNSET:
        sets.append("retries = ?")
        args.append(retries)
    if not sets:
        return
    args.append(task_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", args)
        conn.commit()
    finally:
        conn.close()


def claim_task(task_id: str) -> bool:
    """原子领取任务: 仅当仍为 pending 时置为 running(防多 worker 双领)"""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
            (STATUS_RUNNING, task_id, STATUS_PENDING),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_pending_tasks() -> list:
    """查询所有 pending 任务(worker 轮询用)"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC", (STATUS_PENDING,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- items CRUD ----------

def add_item(task_id: str, item: dict) -> bool:
    """插入一条采集结果;dedup_key 已存在则跳过(全局去重)。

    item 需包含: dedup_key/title/url/summary/source/domain/sector/type/date_str/raw_data(JSON 字符串)
    Returns:
        True 入库成功;False 命中去重被跳过
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items "
            "(task_id, dedup_key, title, url, summary, source, domain, sector, type, date_str, raw_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, item.get("dedup_key"), item.get("title"), item.get("url"),
             item.get("summary") or "", item.get("source") or "", item.get("domain") or "",
             item.get("sector") or "", item.get("type") or "news", item.get("date_str") or "",
             item.get("raw_data") or "{}", _now()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_items(task_id: str, offset: int = 0, limit: int = 50,
              include_consumed: bool = False) -> list:
    """分页拉取任务结果(默认只拉未消费条目,按 id 升序稳定分页)"""
    conn = _connect()
    try:
        sql = "SELECT * FROM items WHERE task_id = ?"
        args = [task_id]
        if not include_consumed:
            sql += " AND consumed = 0"
        sql += " ORDER BY id ASC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_items(task_id: str, unconsumed_only: bool = True) -> int:
    """统计任务结果数(unconsumed_only=True 时只统计未消费)"""
    conn = _connect()
    try:
        if unconsumed_only:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE task_id = ? AND consumed = 0", (task_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row["c"]
    finally:
        conn.close()


def mark_consumed(task_id: str, item_ids: list):
    """标记条目已消费(consume=1 拉取后调用)"""
    if not item_ids:
        return
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(
            f"UPDATE items SET consumed = 1 WHERE task_id = ? AND id IN ({placeholders})",
            [task_id] + [int(i) for i in item_ids],
        )
        conn.commit()
    finally:
        conn.close()


def clear_items(task_id: str):
    """清空任务结果(手动 retry 重跑时旧结果作废)"""
    conn = _connect()
    try:
        conn.execute("DELETE FROM items WHERE task_id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def ping() -> bool:
    """健康检查: 数据库可读写"""
    try:
        conn = _connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True
    except Exception:
        return False
