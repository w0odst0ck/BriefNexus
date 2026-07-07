"""
SQLite + FTS5 存储引擎

核心能力:
  1. 标准数据入库（增/去重/更新）
  2. FTS5 全文索引（标题/标准号/发布机构/范围）
  3. 精确搜索（按字段过滤）
  4. 统计数据（按分类/状态分组）

用法:
  db = StandardDB()
  db.save_items(items)        # 批量入库
  results = db.search("LED")  # 全文检索
  results = db.filter(status="现行")  # 精确过滤
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from ..crawler.utils import logger

CST = timezone(timedelta(hours=8))

# 默认数据库路径（相对于项目根）
_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "standards.db"
)


class StandardDB:
    """标准数据库操作"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or _DEFAULT_DB
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── 连接与建表 ──────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """获取连接（lazy + 复用）"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _init_db(self):
        """建表 + FTS5 索引 + 触发器"""
        conn = self._connect()
        conn.executescript("""
            -- 主表
            CREATE TABLE IF NOT EXISTS standards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key   TEXT UNIQUE NOT NULL,
                title       TEXT NOT NULL,
                standard_no TEXT,
                publisher   TEXT,
                publish_date TEXT,
                status      TEXT,
                category    TEXT,
                url         TEXT,
                source      TEXT,
                ics_code    TEXT,
                scopes      TEXT,
                summary     TEXT,
                collected_at TEXT,
                raw_data    TEXT,       /* JSON 原始数据 */
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
            );

            -- FTS5 全文索引
            CREATE VIRTUAL TABLE IF NOT EXISTS standards_fts USING fts5(
                title,
                standard_no,
                publisher,
                scopes,
                summary,
                content='standards',
                content_rowid='id',
                tokenize='unicode61'
            );

            -- 插入触发器
            CREATE TRIGGER IF NOT EXISTS trg_standards_ai
            AFTER INSERT ON standards BEGIN
                INSERT INTO standards_fts(rowid, title, standard_no, publisher, scopes, summary)
                VALUES (new.id, new.title, new.standard_no, new.publisher, new.scopes, new.summary);
            END;

            -- 删除触发器
            CREATE TRIGGER IF NOT EXISTS trg_standards_ad
            AFTER DELETE ON standards BEGIN
                INSERT INTO standards_fts(standards_fts, rowid, title, standard_no, publisher, scopes, summary)
                VALUES ('delete', old.id, old.title, old.standard_no, old.publisher, old.scopes, old.summary);
            END;

            -- 更新触发器（先删后插）
            CREATE TRIGGER IF NOT EXISTS trg_standards_au
            AFTER UPDATE ON standards BEGIN
                INSERT INTO standards_fts(standards_fts, rowid, title, standard_no, publisher, scopes, summary)
                VALUES ('delete', old.id, old.title, old.standard_no, old.publisher, old.scopes, old.summary);
                INSERT INTO standards_fts(rowid, title, standard_no, publisher, scopes, summary)
                VALUES (new.id, new.title, new.standard_no, new.publisher, new.scopes, new.summary);
            END;

            -- 索引（加速精确查询）
            CREATE INDEX IF NOT EXISTS idx_standards_status ON standards(status);
            CREATE INDEX IF NOT EXISTS idx_standards_category ON standards(category);
            CREATE INDEX IF NOT EXISTS idx_standards_source ON standards(source);
            CREATE INDEX IF NOT EXISTS idx_standards_publish_date ON standards(publish_date);
            CREATE INDEX IF NOT EXISTS idx_standards_ics ON standards(ics_code);

            -- ICS 分类树
            CREATE TABLE IF NOT EXISTS ics_tree (
                code        TEXT PRIMARY KEY,
                parent_code TEXT,
                level       INTEGER NOT NULL,
                name        TEXT NOT NULL
            );

            -- 标准 ↔ ICS 关联（多对多，无 FK 约束以兼容未预定义的 ICS 代码）
            CREATE TABLE IF NOT EXISTS standard_ics (
                standard_id INTEGER NOT NULL REFERENCES standards(id) ON DELETE CASCADE,
                ics_code    TEXT NOT NULL,
                PRIMARY KEY (standard_id, ics_code)
            );
            CREATE INDEX IF NOT EXISTS idx_standard_ics_code ON standard_ics(ics_code);
        """)
        conn.commit()

        # 初始化 ICS 树数据
        self._init_ics_tree()

    def _init_ics_tree(self):
        """填充 ICS 分类树数据"""
        try:
            from .ics_tree import ICS_TREE
            conn = self._connect()
            existing = conn.execute("SELECT COUNT(*) FROM ics_tree").fetchone()[0]
            if existing > 0:
                return
            data = [(c, info["parent"], info["level"], info["name"])
                    for c, info in ICS_TREE.items()]
            conn.executemany(
                "INSERT OR IGNORE INTO ics_tree (code, parent_code, level, name) VALUES (?, ?, ?, ?)",
                data
            )
            conn.commit()
        except ImportError:
            pass  # 没有树数据也能用

    def _conn_execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._connect().execute(sql, params)

    def _conn_executemany(self, sql: str, params_list: list) -> sqlite3.Cursor:
        return self._connect().executemany(sql, params_list)

    # ── 入库 ────────────────────────────────────────────

    def save_item(self, item: dict) -> bool:
        """单条入库，返回 True=新增 False=已存在更新"""
        raw_data = {k: v for k, v in item.items() if k.startswith("_")}
        other_data = {k: v for k, v in item.items()
                      if not k.startswith("_") and k in (
                          "title", "standard_no", "publisher", "publish_date",
                          "status", "category", "url", "source", "ics_code",
                          "scope", "summary", "dedup_key", "collected_at"
                      )}
        now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

        sql = """
            INSERT INTO standards
                (dedup_key, title, standard_no, publisher, publish_date,
                 status, category, url, source, ics_code, scopes, summary,
                 collected_at, raw_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedup_key) DO UPDATE SET
                title=excluded.title,
                standard_no=excluded.standard_no,
                publisher=excluded.publisher,
                publish_date=excluded.publish_date,
                status=excluded.status,
                category=excluded.category,
                url=excluded.url,
                source=excluded.source,
                ics_code=excluded.ics_code,
                scopes=excluded.scopes,
                summary=excluded.summary,
                collected_at=excluded.collected_at,
                raw_data=excluded.raw_data,
                updated_at=excluded.updated_at
        """
        params = (
            item.get("dedup_key", ""),
            item.get("title", ""),
            item.get("standard_no", ""),
            item.get("publisher", ""),
            item.get("publish_date", ""),
            item.get("status", ""),
            item.get("category", ""),
            item.get("url", ""),
            item.get("source", ""),
            item.get("ics_code", ""),
            item.get("scopes", ""),
            item.get("summary", ""),
            item.get("collected_at", ""),
            json.dumps(raw_data, ensure_ascii=False) if raw_data else "",
            now,
            now,
        )

        cursor = self._conn_execute(sql, params)
        self._connect().commit()

        # ICS 关联
        ics_code = item.get("ics_code", "") or ""
        if ics_code:
            std_id = cursor.lastrowid
            if std_id is None:
                # UPSERT 场景：查回
                row = self._conn_execute(
                    "SELECT id FROM standards WHERE dedup_key = ?",
                    (item.get("dedup_key", ""),)
                ).fetchone()
                std_id = row["id"] if row else None
            if std_id:
                self._link_ics(std_id, ics_code)

        return cursor.rowcount > 0

    def _link_ics(self, standard_id: int, ics_code: str):
        """建立标准 ↔ ICS 关联"""
        conn = self._connect()
        # ICS 代码可能含多个，用逗号/分号分隔
        codes = [c.strip() for c in ics_code.replace(",", ";").split(";") if c.strip()]
        for code in codes:
            conn.execute(
                "INSERT OR IGNORE INTO standard_ics (standard_id, ics_code) VALUES (?, ?)",
                (standard_id, code)
            )
            # 自动创建父链关联
            self._link_parent_chain(standard_id, code, conn)
        conn.commit()

    def _link_parent_chain(self, standard_id: int, ics_code: str, conn=None):
        """递归建立父级 ICS 关联（标准挂在节点及其所有父节点上）"""
        if conn is None:
            conn = self._connect()
        # 查父节点
        row = conn.execute(
            "SELECT parent_code FROM ics_tree WHERE code = ?",
            (ics_code,)
        ).fetchone()
        if row and row["parent_code"]:
            parent = row["parent_code"]
            conn.execute(
                "INSERT OR IGNORE INTO standard_ics (standard_id, ics_code) VALUES (?, ?)",
                (standard_id, parent)
            )
            self._link_parent_chain(standard_id, parent, conn)

    def save_items(self, items: List[dict]) -> int:
        """批量入库，返回新增+更新总数"""
        for item in items:
            self.save_item(item)
        return len(items)

    # ── 检索：FTS5 全文搜索 ──────────────────────────────

    def search(self, query: str, limit: int = 50, offset: int = 0,
               status: str = None, category: str = None,
               source: str = None) -> List[Dict[str, Any]]:
        """
        全文搜索（FTS5 + LIKE 双引擎）

        FTS5 对纯英文/数字精准，但中英混合分词不准。
        双轨策略：FTS5 精确搜索 + LIKE 中文回退。

        Args:
            query: 搜索关键词
            limit: 返回条数
            offset: 偏移
            status: 过滤状态
            category: 过滤分类
            source: 过滤数据源

        Returns:
            [{id, title, standard_no, status, ...}]
        """
        if not query or not query.strip():
            return self.filter(limit=limit, offset=offset,
                               status=status, category=category, source=source)

        # 先尝试 FTS5
        results = self._search_fts(query, limit=limit, offset=offset,
                                   status=status, category=category,
                                   source=source)

        # FTS5 命中足够 -> 直接返回
        fts_count = self._search_fts_count(query, status=status,
                                            category=category, source=source)
        if fts_count >= min(limit, 3):
            return results

        # FTS5 不足 -> LIKE 回退（覆盖中文）
        results_like = self._search_like(query, limit=limit, offset=offset,
                                         status=status, category=category,
                                         source=source)

        # 合并 + 去重（按 dedup_key）
        seen = set()
        merged = []
        for r in results + results_like:
            dk = r.get("dedup_key", "")
            if dk and dk not in seen:
                seen.add(dk)
                merged.append(r)
            elif not dk:
                merged.append(r)

        return merged[:limit]

    def _search_fts(self, query: str, limit: int = 50, offset: int = 0,
                    status: str = None, category: str = None,
                    source: str = None) -> List[Dict[str, Any]]:
        """FTS5 子搜索"""
        safe_query = self._sanitize_fts_query(query)

        conditions = ["standards_fts MATCH ?"]
        params = [safe_query]

        if status:
            conditions.append("s.status = ?")
            params.append(status)
        if category:
            conditions.append("s.category = ?")
            params.append(category)
        if source:
            conditions.append("s.source = ?")
            params.append(source)

        sql = f"""
            SELECT s.*
            FROM standards_fts f
            JOIN standards s ON s.id = f.rowid
            WHERE {' AND '.join(conditions)}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = self._conn_execute(sql, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def _search_fts_count(self, query: str, status: str = None,
                           category: str = None,
                           source: str = None) -> int:
        """FTS5 命中数"""
        safe_query = self._sanitize_fts_query(query)

        conditions = ["standards_fts MATCH ?"]
        params = [safe_query]

        if status:
            conditions.append("s.status = ?")
            params.append(status)
        if category:
            conditions.append("s.category = ?")
            params.append(category)
        if source:
            conditions.append("s.source = ?")
            params.append(source)

        sql = f"""
            SELECT COUNT(*) as cnt
            FROM standards_fts f
            JOIN standards s ON s.id = f.rowid
            WHERE {' AND '.join(conditions)}
        """
        cursor = self._conn_execute(sql, tuple(params))
        row = cursor.fetchone()
        return row["cnt"] if row else 0

    def _search_like(self, query: str, limit: int = 50, offset: int = 0,
                     status: str = None, category: str = None,
                     source: str = None) -> List[Dict[str, Any]]:
        """LIKE 模糊搜索（覆盖中文）"""
        conditions = []
        params = []

        # 按空格分割，每个词用 LIKE
        terms = query.strip().split()
        like_clauses = []
        for term in terms:
            like_clauses.append(
                "(s.title LIKE ? OR s.standard_no LIKE ? OR s.publisher LIKE ? OR s.scopes LIKE ? OR s.summary LIKE ?)"
            )
            like_term = f"%{term}%"
            for _ in range(5):
                params.append(like_term)
        conditions.append("(" + " AND ".join(like_clauses) + ")")

        if status:
            conditions.append("s.status = ?")
            params.append(status)
        if category:
            conditions.append("s.category = ?")
            params.append(category)
        if source:
            conditions.append("s.source = ?")
            params.append(source)

        sql = f"""
            SELECT s.*
            FROM standards s
            WHERE {' AND '.join(conditions)}
            ORDER BY s.publish_date DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = self._conn_execute(sql, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def search_count(self, query: str, status: str = None,
                     category: str = None, source: str = None) -> int:
        """搜索命中总数"""
        # LIKE 结果更准确（覆盖中文），直接返回 LIKE 计数
        terms = query.strip().split()
        conditions = []
        params = []
        for term in terms:
            like_clauses = []
            for _f in ["s.title", "s.standard_no", "s.publisher", "s.scopes", "s.summary"]:
                like_clauses.append(f"{_f} LIKE ?")
                params.append(f"%{term}%")
            conditions.append("(" + " OR ".join(like_clauses) + ")")
        where_extra = " AND ".join(conditions) if conditions else "1=1"

        extra = []
        if status:
            extra.append("s.status = ?")
            params.append(status)
        if category:
            extra.append("s.category = ?")
            params.append(category)
        if source:
            extra.append("s.source = ?")
            params.append(source)
        if extra:
            where_extra += " AND " + " AND ".join(extra)

        cursor = self._conn_execute(
            f"SELECT COUNT(*) as cnt FROM standards s WHERE {where_extra}",
            tuple(params)
        )
        row = cursor.fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """FTS5 查询转义"""
        # 标准号搜索：精确匹配
        if re.match(r'^[A-Z0-9/.\-—–\s]+$', query):
            return f'"{query}"'
        # 普通文本
        # 将中文字符分别用 OR 连接（FTS5 每个中文字是独立 token）
        # 但为了避免性能问题，直接用原始文本
        return query

    # ── 检索：精确过滤 ──────────────────────────────────

    def filter(self, status: str = None, category: str = None,
               source: str = None, standard_no: str = None,
               publisher: str = None, date_from: str = None,
               date_to: str = None, limit: int = 50,
               offset: int = 0) -> List[Dict[str, Any]]:
        """精确过滤查询"""
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if standard_no:
            conditions.append("standard_no = ?")
            params.append(standard_no)
        if publisher:
            conditions.append("publisher LIKE ?")
            params.append(f"%{publisher}%")
        if date_from:
            conditions.append("publish_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("publish_date <= ?")
            params.append(date_to)

        where = " AND ".join(conditions) if conditions else "1=1"

        sql = f"""
            SELECT * FROM standards
            WHERE {where}
            ORDER BY publish_date DESC, id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = self._conn_execute(sql, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def filter_count(self, status: str = None, category: str = None,
                     source: str = None, standard_no: str = None,
                     publisher: str = None, date_from: str = None,
                     date_to: str = None) -> int:
        """过滤命中总数"""
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if standard_no:
            conditions.append("standard_no = ?")
            params.append(standard_no)
        if publisher:
            conditions.append("publisher LIKE ?")
            params.append(f"%{publisher}%")
        if date_from:
            conditions.append("publish_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("publish_date <= ?")
            params.append(date_to)

        where = " AND ".join(conditions) if conditions else "1=1"

        cursor = self._conn_execute(
            f"SELECT COUNT(*) as cnt FROM standards WHERE {where}",
            tuple(params)
        )
        row = cursor.fetchone()
        return row["cnt"] if row else 0

    # ── 统计 ────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """数据库统计信息"""
        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM standards").fetchone()[0]

        by_status = {}
        for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM standards WHERE status != '' GROUP BY status ORDER BY cnt DESC"
        ):
            by_status[row["status"]] = row["cnt"]

        by_category = {}
        for row in conn.execute(
                "SELECT category, COUNT(*) as cnt FROM standards WHERE category != '' GROUP BY category ORDER BY cnt DESC"
        ):
            by_category[row["category"]] = row["cnt"]

        by_source = {}
        for row in conn.execute(
                "SELECT source, COUNT(*) as cnt FROM standards WHERE source != '' GROUP BY source ORDER BY cnt DESC"
        ):
            by_source[row["source"]] = row["cnt"]

        last_update = conn.execute(
            "SELECT MAX(updated_at) FROM standards"
        ).fetchone()[0] or "N/A"

        return {
            "total": total,
            "by_status": by_status,
            "by_category": by_category,
            "by_source": by_source,
            "last_update": last_update,
            "db_path": self.db_path,
        }

    # ── ICS 分类树 ────────────────────────────────────

    def tree_children(self, code: str = "") -> List[Dict[str, Any]]:
        """获取某节点的子节点（含每个子节点下的标准计数）"""
        conn = self._connect()
        if not code:
            rows = conn.execute(
                """SELECT t.*, 
                    (SELECT COUNT(*) FROM standard_ics si WHERE si.ics_code = t.code) as std_count
                   FROM ics_tree t
                   WHERE t.level = 1
                   ORDER BY t.code"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.*, 
                    (SELECT COUNT(*) FROM standard_ics si WHERE si.ics_code = t.code) as std_count
                   FROM ics_tree t
                   WHERE t.parent_code = ?
                   ORDER BY t.code""",
                (code,)
            ).fetchall()
        return [dict(r) for r in rows]

    def tree_get_node(self, code: str) -> Optional[Dict[str, Any]]:
        """获取节点信息（含名称/路径/标准计数）"""
        conn = self._connect()
        row = conn.execute(
            """SELECT t.*,
                (SELECT COUNT(*) FROM standard_ics si WHERE si.ics_code = t.code) as std_count
               FROM ics_tree t WHERE t.code = ?""",
            (code,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["path"] = self.tree_get_path(code)
        return result

    def tree_get_path(self, code: str) -> str:
        """获取可读路径"""
        try:
            from .ics_tree import get_display_path
            return get_display_path(code)
        except ImportError:
            parts = []
            conn = self._connect()
            while code:
                row = conn.execute(
                    "SELECT name, parent_code FROM ics_tree WHERE code = ?",
                    (code,)
                ).fetchone()
                if row:
                    parts.insert(0, row["name"])
                    code = row["parent_code"]
                else:
                    break
            return " / ".join(parts)

    def tree_standards(self, code: str, limit: int = 50,
                       offset: int = 0) -> List[Dict[str, Any]]:
        """获取节点下所有关联标准"""
        cursor = self._conn_execute(
            """SELECT s.* FROM standards s
               JOIN standard_ics si ON si.standard_id = s.id
               WHERE si.ics_code = ?
               ORDER BY s.publish_date DESC
               LIMIT ? OFFSET ?""",
            (code, limit, offset)
        )
        return [dict(r) for r in cursor.fetchall()]

    def tree_standard_count(self, code: str) -> int:
        """节点下标准计数"""
        row = self._conn_execute(
            "SELECT COUNT(*) as cnt FROM standard_ics WHERE ics_code = ?",
            (code,)
        ).fetchone()
        return row["cnt"] if row else 0

    def get_latest(self, limit: int = 10) -> List[Dict[str, Any]]:
        """最新入库的标准"""
        cursor = self._conn_execute(
            "SELECT * FROM standards ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_by_dedup_key(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        """按去重键精确查找"""
        cursor = self._conn_execute(
            "SELECT * FROM standards WHERE dedup_key = ?", (dedup_key,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_by_standard_no(self, standard_no: str) -> Optional[Dict[str, Any]]:
        """按标准号精确查找"""
        cursor = self._conn_execute(
            "SELECT * FROM standards WHERE standard_no = ?", (standard_no,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_note(self, standard_id: int, note: str):
        """更新标准备注/笔记

        可用于存储手动补充的全文摘要、本地PDF路径等。
        """
        self._conn_execute(
            "UPDATE standards SET scopes = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (note, standard_id)
        )
        self._connect().commit()

    def update_local_path(self, standard_id: int, local_path: str):
        """记录本地PDF/文件路径"""
        import json
        row = self._conn_execute(
            "SELECT raw_data FROM standards WHERE id = ?", (standard_id,)
        ).fetchone()
        if row and row["raw_data"]:
            try:
                raw = json.loads(row["raw_data"])
            except:
                raw = {}
        else:
            raw = {}
        raw["local_path"] = local_path
        self._conn_execute(
            "UPDATE standards SET raw_data = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (json.dumps(raw, ensure_ascii=False), standard_id)
        )
        self._connect().commit()

    def export_to_json(self, output_path: str = None) -> str:
        """将整个数据库导出为 JSON 文件"""
        if output_path is None:
            output_path = os.path.join(
                os.path.dirname(self.db_path),
                "standards_export.json"
            )
        cursor = self._conn_execute("SELECT * FROM standards ORDER BY publish_date DESC")
        items = [dict(row) for row in cursor.fetchall()]
        from .exporter import export_json
        export_json(items, output_path)
        return output_path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


import re  # noqa: E402 (used by _sanitize_fts_query)
