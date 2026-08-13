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
    """初始化数据库: 设置路径、建目录、建表 + 迁移 + 索引(WAL)"""
    global _db_path
    _db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)
    conn = _connect()
    try:
        _create_tables(conn)   # 建表(仅当不存在)
        _migrate(conn)         # 旧库补列 / 重建去重约束
        _create_indexes(conn)  # 索引(依赖迁移后的列)
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


# items 表 DDL(items 建表 / 旧库重建共用,保证结构一致)
# 注意: dedup_key 无列级 UNIQUE —— 跨任务引用记录(同一 dedup_key)需共存多行,
#       原记录唯一性由部分唯一索引 idx_items_dedup_orig(is_ref=0)保证(见 _create_indexes)。
_ITEMS_DDL = """
    CREATE TABLE IF NOT EXISTS items (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        dedup_key  TEXT,
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
        is_ref     INTEGER DEFAULT 0,
        created_at TEXT
    )
"""


def _create_tables(conn: sqlite3.Connection):
    """建表: tasks + items + source_stats + collector_cache(与选型书 4.1 一致)

    tasks 额外列(D16/D18):
      - collector_spec: 随请求传入的源规格 JSON(module 引用或 code 内联),
        为空表示走配置内置源(旧行为)
      - traceback: 采集器实例化/crawl 异常时的完整堆栈文本
      - collector_log: crawl 调用期间采集器 stderr 输出
    """
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
            callback_url TEXT,
            archived    INTEGER DEFAULT 0,
            collector_spec TEXT,
            traceback   TEXT,
            collector_log TEXT
        )
    """)
    # 内联代码缓存(D19): hash=SHA256(code),记录「该代码已通过安全检查」,
    # 同 hash 复用;编译对象不持久化(pickle code 复杂),进程内另有内存缓存。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collector_cache (
            hash       TEXT PRIMARY KEY,
            code       TEXT NOT NULL,
            created_at TEXT
        )
    """)
    conn.execute(_ITEMS_DDL)
    # source_stats 预留(D15 源级健康状态): worker 后续按 source 更新统计
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_stats (
            source          TEXT PRIMARY KEY,
            last_run_at     TEXT,
            ok_count        INTEGER DEFAULT 0,
            fail_count      INTEGER DEFAULT 0,
            last_duration_s REAL,
            last_error      TEXT
        )
    """)


def _migrate(conn: sqlite3.Connection):
    """旧库迁移: 补 items.is_ref / tasks.archived;去掉 dedup_key 列级 UNIQUE(重建表)。

    迁移前旧表 dedup_key 是 UNIQUE,无法容纳跨任务引用行(同一 dedup_key 多行),
    因此需重建 items 表为新结构(无列级 UNIQUE),原记录唯一性改由部分唯一索引保证。
    """
    # 1) items.is_ref 列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "is_ref" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN is_ref INTEGER DEFAULT 0")
    # 2) 旧表 dedup_key 列级 UNIQUE → 重建为无 UNIQUE 结构
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()
    table_sql = (row["sql"] or "") if row else ""
    if "dedup_key" in table_sql and "UNIQUE" in table_sql.upper():
        logger.info("检测到旧版 items 表(dedup_key UNIQUE),重建为支持跨任务引用的结构")
        _rebuild_items_table(conn)
    # 3) tasks.archived 列
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "archived" not in tcols:
        conn.execute("ALTER TABLE tasks ADD COLUMN archived INTEGER DEFAULT 0")
    # 4) D16/D18 新增列: collector_spec / traceback / collector_log
    for col in ("collector_spec", "traceback", "collector_log"):
        if col not in tcols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
    # 5) 防御脏数据: is_ref=0 的同 dedup_key 多行(异常库)→ 保留最小 id,其余转引用
    _dedupe_legacy_keys(conn)


def _dedupe_legacy_keys(conn: sqlite3.Connection):
    """防御性清理: 若存在 is_ref=0 的同 dedup_key 多行(脏库/手工写库),保留最小 id,
    其余置 is_ref=1 —— 保证部分唯一索引 idx_items_dedup_orig 可创建,启动不崩溃。"""
    rows = conn.execute(
        "SELECT dedup_key FROM items WHERE is_ref = 0 AND dedup_key IS NOT NULL "
        "GROUP BY dedup_key HAVING COUNT(*) > 1"
    ).fetchall()
    for r in rows:
        dup = conn.execute(
            "SELECT id FROM items WHERE dedup_key = ? AND is_ref = 0 ORDER BY id",
            (r["dedup_key"],),
        ).fetchall()
        for row in dup[1:]:
            conn.execute("UPDATE items SET is_ref = 1 WHERE id = ?", (row["id"],))
    if rows:
        logger.warning("检测到 %d 组重复 dedup_key(脏数据),已转为引用记录", len(rows))


def _rebuild_items_table(conn: sqlite3.Connection):
    """items 表 9 步重建: 新建无 UNIQUE 表 → 复制数据 → 换名(旧索引随 DROP 一并删除)"""
    ddl = _ITEMS_DDL.replace("CREATE TABLE IF NOT EXISTS items", "CREATE TABLE items_new")
    conn.execute(ddl)
    conn.execute("""
        INSERT INTO items_new
            (id, task_id, dedup_key, title, url, summary, source, domain, sector,
             type, date_str, raw_data, consumed, is_ref, created_at)
        SELECT id, task_id, dedup_key, title, url, summary, source, domain, sector,
               type, date_str, raw_data, consumed, is_ref, created_at
        FROM items
    """)
    conn.execute("DROP TABLE items")
    conn.execute("ALTER TABLE items_new RENAME TO items")


def _create_indexes(conn: sqlite3.Connection):
    """常规索引 + 部分唯一索引(dedup_key 唯一性只约束原记录 is_ref=0,引用行不受限)"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_task ON items(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_dedup ON items(dedup_key)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedup_orig "
                 "ON items(dedup_key) WHERE is_ref = 0")
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
                callback_url: str | None = None, task_id: str | None = None,
                collector_spec: str | None = None) -> str:
    """创建任务(status=pending),返回 task_id

    collector_spec: 源规格 JSON 字符串(module 引用 / code 内联),
    None 表示走配置内置源(旧行为)。
    """
    task_id = task_id or make_task_id()
    import json
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, source, params, domain, status, created_at, callback_url, "
            "collector_spec) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, source, json.dumps(params or {}, ensure_ascii=False),
             domain, STATUS_PENDING, _now(), callback_url, collector_spec),
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
                retries: object = _UNSET, traceback: object = _UNSET,
                collector_log: object = _UNSET):
    """按需更新任务字段;error/finished_at/traceback 等传 None 表示显式清空,不传表示不更新"""
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
    if traceback is not _UNSET:
        sets.append("traceback = ?")
        args.append(traceback)
    if collector_log is not _UNSET:
        sets.append("collector_log = ?")
        args.append(collector_log)
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

# add_item 返回码: True 本次新增入库 / 'ref' 跨任务引用(复制入库) / False 同任务内重复(跳过)
ADDED = True
REFERRED = "ref"
DUPLICATED = False


def add_item(task_id: str, item: dict):
    """插入一条采集结果,支持跨任务去重引用(D11)。

    去重语义:
      - dedup_key 全局首次出现 → 正常入库,返回 True
      - dedup_key 已被**其他任务**采集 → 复制引用入库(is_ref=1,完整字段),
        返回 'ref' —— 新任务同样能拉到自己的结果(全局去重只防重复采集,不阻断多任务消费)
      - dedup_key 在本任务内重复 → 跳过,返回 False
      - 无 dedup_key → 直接入库(不参与去重),返回 True

    并发安全: 原记录唯一性由部分唯一索引 idx_items_dedup_orig(is_ref=0)保证,
    INSERT OR IGNORE 在并发竞态下静默退化为引用/跳过路径,不会产生重复原记录。

    item 需包含: dedup_key/title/url/summary/source/domain/sector/type/date_str/raw_data(JSON 字符串)
    """
    conn = _connect()
    try:
        dedup_key = item.get("dedup_key")
        if not dedup_key:
            # 无去重键 → 直接入库
            _insert_item(conn, task_id, item, is_ref=0)
            conn.commit()
            return ADDED
        # 1) 尝试作为原记录插入(部分唯一索引冲突时被 IGNORE,rowcount=0)
        cur = _insert_item(conn, task_id, item, is_ref=0, or_ignore=True)
        conn.commit()
        if cur.rowcount > 0:
            return ADDED
        # 2) 已被采集过 → 判断归属
        row = conn.execute(
            "SELECT task_id FROM items WHERE dedup_key = ? AND is_ref = 0 "
            "ORDER BY id LIMIT 1", (dedup_key,),
        ).fetchone()
        if row is None:
            return DUPLICATED  # 极端竞态,保守跳过
        if row["task_id"] == task_id:
            return DUPLICATED  # 同任务内重复
        # 3) 跨任务 → 完整复制引用记录(is_ref=1,不受唯一索引约束)
        #    任务内去重: 同任务同 dedup_key 已有引用行则跳过,避免重复引用
        dup_ref = conn.execute(
            "SELECT 1 FROM items WHERE task_id = ? AND dedup_key = ? AND is_ref = 1 LIMIT 1",
            (task_id, dedup_key),
        ).fetchone()
        if dup_ref:
            return DUPLICATED
        cur = _insert_item(conn, task_id, item, is_ref=1, or_ignore=True)
        conn.commit()
        return REFERRED if cur.rowcount > 0 else DUPLICATED
    finally:
        conn.close()


def _insert_item(conn: sqlite3.Connection, task_id: str, item: dict,
                 is_ref: int, or_ignore: bool = False) -> sqlite3.Cursor:
    """插入单条 items 记录(新增与引用共用,is_ref 区分)"""
    sql = ("INSERT OR IGNORE INTO items " if or_ignore else "INSERT INTO items ") + (
        "(task_id, dedup_key, title, url, summary, source, domain, sector, "
        "type, date_str, raw_data, consumed, is_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)"
    )
    return conn.execute(
        sql,
        (task_id, item.get("dedup_key"), item.get("title"), item.get("url"),
         item.get("summary") or "", item.get("source") or "", item.get("domain") or "",
         item.get("sector") or "", item.get("type") or "news", item.get("date_str") or "",
         item.get("raw_data") or "{}", is_ref, _now()),
    )


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


# ---------- 数据清理(D13) ----------

def cleanup(consumed_ttl_days: int = 90, task_archive_days: int = 30) -> dict:
    """每日数据清理: 防公共平台数据无限膨胀。

    - 删除 consumed=1 且 created_at 早于 now-consumed_ttl_days 的 items(已消费过期)
    - 归档终态任务(done/failed/cancelled)且 finished_at 早于 now-task_archive_days:
      删除其全部 items 并置 archived=1(归档任务不再出现在业务查询里)

    Returns:
        {"items_deleted": int, "tasks_archived": int}
    """
    now = datetime.now(CST)
    consumed_cutoff = (now - timedelta(days=consumed_ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    archive_cutoff = (now - timedelta(days=task_archive_days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        # 1) 过期已消费 items
        cur = conn.execute(
            "DELETE FROM items WHERE consumed = 1 AND created_at < ?", (consumed_cutoff,)
        )
        items_deleted = cur.rowcount
        # 2) 归档任务 + 删除其 items(先取 id 再删,避免游标迭代中修改表)
        rows = conn.execute(
            "SELECT id FROM tasks WHERE archived = 0 "
            "AND status IN (?, ?, ?) AND finished_at IS NOT NULL AND finished_at < ?",
            (*TERMINAL_STATUSES, archive_cutoff),
        ).fetchall()
        tasks_archived = 0
        for r in rows:
            cur = conn.execute("DELETE FROM items WHERE task_id = ?", (r["id"],))
            items_deleted += cur.rowcount
            conn.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (r["id"],))
            tasks_archived += 1
        conn.commit()
        return {"items_deleted": items_deleted, "tasks_archived": tasks_archived}
    finally:
        conn.close()


# ---------- 内联代码缓存(D19) ----------

def upsert_collector_cache(code_hash: str, code: str):
    """记录某 code 已通过安全检查(同 hash 复用;存在则刷新 code/时间) """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO collector_cache (hash, code, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(hash) DO UPDATE SET code = excluded.code, created_at = excluded.created_at",
            (code_hash, code, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_collector_cache(code_hash: str) -> dict | None:
    """按 hash 查缓存记录(存在说明该 code 已通过安全检查) """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM collector_cache WHERE hash = ?", (code_hash,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------- 活跃源统计(D21 /sources 动态发现) ----------

def get_source_activity(days: int = 30) -> list:
    """最近 days 天有任务记录的源聚合统计(活跃源发现 + 静态源 last_used 补全)

    Returns:
        [{source, last_used, success_count, collector_spec(样例)}]
    """
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        # last_used=MAX(created_at) 精确;collector_spec 仅样例展示(JSON 字典序最大值,
        # 不保证是最近任务——非关键字段,消费方按 source 维度使用,可接受)
        rows = conn.execute(
            "SELECT source, MAX(created_at) AS last_used, "
            "SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS success_count, "
            "MAX(collector_spec) AS collector_spec "
            "FROM tasks WHERE created_at >= ? GROUP BY source",
            (STATUS_DONE, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
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
