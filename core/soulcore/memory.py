"""
memory.py —— 分层记忆库（借鉴 LSTM 门控思想，但不训练网络）

  输入门 write(): 每条记忆带 importance ∈ [0,1]；低于 input_floor 的直接不存
  遗忘门 weight(): w = importance · exp(-Δt/τ_kind) · (1 + β·ln(1+被检索次数))
                   maintain() 把 w < cold_threshold 的标记为 cold（不删，只是不参与常规检索）
  输出门 search(): 相关度(BM25) × w × 层级折扣，先搜当前分支，不够再逐级回退到父分支 / core / 全局
                   被检索到的记忆刷新 last_access（复习强化）
  细胞状态 consolidate(): 某分支冷掉的原始对话太多时，用 LLM 压成一条摘要（长 τ），原文标记 merged 并保留指针

存储：SQLite（items 表 + FTS5 索引）为准，Markdown 文件为人类可读镜像。
路径即目录树：chat/qq/group/<群号>、chat/bilibili/dm/<uid>、project/<项目名>、core/people/...、core/self
"""
from __future__ import annotations
import math, os, re, sqlite3, threading, time, json
from dataclasses import dataclass
from typing import Iterable, Optional

from .textutil import index_text, fts_query, jaccard, token_weights, overlap, tokens
from .embedding import cosines, pack
from .extractor import is_question

INDEX_VERSION = 2   # v2：索引里加入概念 token（见 concepts.py）
SCHEMA_VERSION = 2  # v2：trust / pinned / 时间轴 / 墓碑

# 记忆来源的可信度。检索时按当前场景限制能召回哪些级别（见 search 的 allowed_trusts）。
#   owner    ：主人本人（本地 DSH、主人的 QQ 私聊）
#   known    ：白名单私聊、群里的熟人
#   stranger ：群里的陌生人
#   public   ：公开平台（B站评论区等）
TRUST_LEVELS = ("owner", "known", "stranger", "public")
TRUST_RANK = {"public": 0, "stranger": 1, "known": 2, "owner": 3}


def stricter(a: str, b: str) -> str:
    """两个来源合并时取更不可信的那个——合并不会让内容变得更可信。"""
    return a if TRUST_RANK.get(a, 0) <= TRUST_RANK.get(b, 0) else b

DEFAULT_TAU_HOURS = {
    "turn": 72,            # 原始对话：3 天
    "fact": 24 * 60,       # 事实：2 个月
    "preference": 24 * 180,
    "decision": 24 * 365,  # 项目决策：1 年
    "todo": 24 * 30,
    "person": 24 * 180,    # 对某个人的印象
    "self": 24 * 365,      # 她对自己的认知
    "summary": 24 * 120,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  role TEXT DEFAULT '',
  speaker TEXT DEFAULT '',
  content TEXT NOT NULL,
  importance REAL NOT NULL,
  tau_h REAL NOT NULL,
  created REAL NOT NULL,
  last_access REAL NOT NULL,
  access_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'hot',
  tags TEXT DEFAULT '',
  user_id TEXT DEFAULT '',
  parent_id INTEGER,
  trust TEXT DEFAULT 'owner',
  pinned INTEGER DEFAULT 0,
  valid_from REAL,
  invalid_at REAL,
  superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS ix_items_path ON items(path, kind, status);
CREATE INDEX IF NOT EXISTS ix_items_created ON items(created);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(tok);
CREATE TABLE IF NOT EXISTS relations(
  platform TEXT, uid TEXT, name TEXT, affinity REAL DEFAULT 0.5,
  interactions INTEGER DEFAULT 0, last_seen REAL, PRIMARY KEY(platform, uid)
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, model TEXT, v BLOB);
CREATE TABLE IF NOT EXISTS tombstones(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fp TEXT NOT NULL,
  content TEXT NOT NULL,
  created REAL NOT NULL
);
"""

# 老库升级：CREATE TABLE IF NOT EXISTS 不会给已有的表加列，这里补 ALTER TABLE
_MIGRATIONS = {
    "trust": "TEXT DEFAULT 'owner'",
    "pinned": "INTEGER DEFAULT 0",
    "valid_from": "REAL",
    "invalid_at": "REAL",
    "superseded_by": "INTEGER",
}

# 老条目没有 trust：按路径回填。保守起见，群聊/评论区一律降级。
_TRUST_BACKFILL = [
    ("chat/qq/group/%", "stranger"),
    ("chat/bilibili/comment/%", "public"),
    ("chat/qq/private/%", "known"),
    ("chat/bilibili/dm/%", "known"),
]


@dataclass
class Item:
    id: int
    path: str
    kind: str
    role: str
    speaker: str
    content: str
    importance: float
    tau_h: float
    created: float
    last_access: float
    access_count: int
    status: str
    tags: str
    user_id: str
    parent_id: Optional[int]
    trust: str = "owner"
    pinned: int = 0
    valid_from: Optional[float] = None
    invalid_at: Optional[float] = None
    superseded_by: Optional[int] = None
    score: float = 0.0
    weight_now: float = 0.0


class MemoryStore:
    def __init__(self, data_dir: str, cfg: dict | None = None, clock=time.time, embedder=None):
        cfg = cfg or {}
        self.embedder = embedder            # 可选：CachedEmbedder（见 embedding.py）
        self.embed_turns = cfg.get("embed_turns", False)
        self.sem_floor = cfg.get("sem_floor", 0.45)   # 余弦相似度低于它视为不相关
        self.dir = data_dir
        self.md_root = os.path.join(data_dir, "memory")
        os.makedirs(self.md_root, exist_ok=True)
        self.clock = clock
        self.tau = {**DEFAULT_TAU_HOURS, **cfg.get("tau_hours", {})}
        self.beta = cfg.get("beta", 0.3)
        self.input_floor = cfg.get("input_floor", 0.05)
        self.cold_threshold = cfg.get("cold_threshold", 0.04)
        self.group_turn_tau_scale = cfg.get("group_turn_tau_scale", 0.5)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(data_dir, "memory.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._migrate()
        if self.meta_get("index_version", 1) < INDEX_VERSION:
            self.reindex()

    # ================= 迁移 =================
    def _migrate(self) -> None:
        """给老库补列并回填 trust。新库此时已是 v2，不做事。"""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        added = [name for name in _MIGRATIONS if name not in cols]
        for name in added:
            self.db.execute(f"ALTER TABLE items ADD COLUMN {name} {_MIGRATIONS[name]}")
        if "trust" in added:
            # 老条目全部是 owner 的话，升级后群聊投毒仍然畅通，所以按路径降级
            for pat, level in _TRUST_BACKFILL:
                self.db.execute("UPDATE items SET trust=? WHERE path LIKE ?", (level, pat))
        self.db.execute("UPDATE items SET trust='owner' WHERE trust IS NULL OR trust=''")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_items_trust ON items(trust)")
        if added:
            self.db.commit()
        if self.meta_get("schema_version", 1) < SCHEMA_VERSION:
            self.meta_set("schema_version", SCHEMA_VERSION)

    # ================= 输入门：写入 =================
    def write(self, path: str, kind: str, content: str, importance: float, *, role: str = "",
              speaker: str = "", tags: Iterable[str] = (), user_id: str = "", dedup: bool = True,
              ts: float | None = None, mirror: bool = True, trust: str = "owner",
              pinned: bool = False, by_tombstone_ok: bool = False) -> Optional[int]:
        """写一条记忆。

        trust          来源可信度（见 TRUST_LEVELS）。检索时按场景过滤，默认 owner。
        pinned         永久事实（名字/生日/过敏…）：不参与衰减，也不会转冷。
        by_tombstone_ok 用户主动写入（remember 工具）时绕过墓碑；自动提炼不绕过。
        """
        content = (content or "").strip()
        if not content or importance < self.input_floor:
            return None
        now = ts if ts is not None else self.clock()
        trust = trust if trust in TRUST_RANK else "owner"
        tau = float(self.tau.get(kind, 72))
        if kind == "turn" and "/group/" in path:
            tau *= self.group_turn_tau_scale
        tag_s = " ".join(sorted(set(t.strip("#") for t in tags if t)))
        merged = None
        with self.lock:
            if kind != "turn" and not by_tombstone_ok and self._tombstoned(content):
                return None      # 用户删过这条：自动提炼不再把它复活
            if dedup and kind != "turn":
                dup = self._find_duplicate(path, kind, content)
                if dup is not None:
                    merged = self._merge_duplicate(dup, path, kind, content, importance, now, tag_s, trust)
            if merged is None:
                cur = self.db.execute(
                    "INSERT INTO items(path,kind,role,speaker,content,importance,tau_h,created,last_access,"
                    "tags,user_id,trust,pinned,valid_from) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path, kind, role, speaker, content, float(importance), tau, now, now, tag_s, user_id,
                     trust, 1 if pinned else 0, now))
                iid = cur.lastrowid
                self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                                (iid, index_text(content + " " + tag_s + " " + speaker)))
                self.db.commit()
            else:
                iid = merged
        row = None
        if merged is not None:
            row = self.db.execute("SELECT content, importance FROM items WHERE id=?", (iid,)).fetchone()
        if self.embedder is not None and (kind != "turn" or self.embed_turns):
            self._embed_ids([(iid, ((row["content"] if row else content) + " " + tag_s))])
        if mirror:
            if merged is None:
                self._mirror(path, kind, content, iid, importance, speaker, role, now)
            else:
                self._rewrite_mirror_line(path, kind, iid, row["content"], row["importance"], now)
        return iid

    def _merge_duplicate(self, dup: int, path: str, kind: str, content: str, importance: float,
                         now: float, tag_s: str, trust: str) -> int:
        """去重命中：更新数据库**和** Markdown 镜像、全文索引、向量。

        旧实现只改数据库，下次 sync_markdown() 会以 Markdown 为准把它改回旧内容。
        """
        r = self.db.execute("SELECT content, tags, speaker, trust FROM items WHERE id=?", (dup,)).fetchone()
        old = r["content"] if r else ""
        new_content = content if len(content) > len(old) else old
        keep_trust = stricter(r["trust"] if r and r["trust"] else "owner", trust)
        self.db.execute(
            "UPDATE items SET importance=MAX(importance,?), last_access=?, status='hot',"
            " content=?, trust=? WHERE id=?",
            (importance, now, new_content, keep_trust, dup))
        self.db.execute("DELETE FROM items_fts WHERE rowid=?", (dup,))
        self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                        (dup, index_text(new_content + " " + (r["tags"] or tag_s) + " " + (r["speaker"] or ""))))
        self.db.execute("DELETE FROM vecs WHERE id=?", (dup,))
        self.db.commit()
        return dup

    def _embed_ids(self, pairs: list[tuple[int, str]]) -> int:
        if not pairs or self.embedder is None:
            return 0
        vecs = self.embedder.embed([t for _, t in pairs])
        if not vecs:
            return 0
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO vecs(id, model, v) VALUES(?,?,?)",
                                [(i, self.embedder.name, pack(v)) for (i, _), v in zip(pairs, vecs)])
            self.db.commit()
        return len(vecs)

    def backfill_embeddings(self, batch: int = 32, limit: int = 256) -> int:
        """给还没有向量的提炼条目补向量（换模型后也会重算）"""
        if self.embedder is None:
            return 0
        with self.lock:
            rows = self.db.execute(
                "SELECT items.id, items.content, items.tags FROM items LEFT JOIN vecs ON vecs.id=items.id "
                "WHERE (vecs.id IS NULL OR vecs.model!=?) AND items.status!='merged' AND items.status!='deleted' "
                + ("" if self.embed_turns else "AND items.kind!='turn' ") + "LIMIT ?",
                (self.embedder.name, limit)).fetchall()
        n = 0
        for i in range(0, len(rows), batch):
            n += self._embed_ids([(r["id"], r["content"] + " " + (r["tags"] or "")) for r in rows[i:i + batch]])
        return n

    def _find_duplicate(self, path: str, kind: str, content: str, thr: float = 0.75) -> Optional[int]:
        q = fts_query(content)
        if not q:
            return None
        rows = self.db.execute(
            "SELECT items.id, items.content FROM items_fts JOIN items ON items.id=items_fts.rowid "
            "WHERE items_fts MATCH ? AND items.path=? AND items.kind=? "
            "AND items.status NOT IN ('merged','deleted') AND items.invalid_at IS NULL "
            "ORDER BY bm25(items_fts) LIMIT 5", (q, path, kind)).fetchall()
        for r in rows:
            if jaccard(r["content"], content) >= thr:
                return r["id"]
        return None

    # ================= 墓碑：用户删掉的内容不再自动复活 =================
    @staticmethod
    def _fp(content: str) -> str:
        return " ".join(sorted(set(tokens(content))))

    def _tombstoned(self, content: str) -> bool:
        fp = self._fp(content)
        if not fp:
            return False
        sa = set(fp.split())
        for r in self.db.execute("SELECT fp FROM tombstones ORDER BY id DESC LIMIT 1000"):
            sb = set((r["fp"] or "").split())
            if not sb:
                continue
            if len(sa & sb) / len(sa | sb) >= 0.9:
                return True
        return False

    def add_tombstone(self, content: str) -> bool:
        fp = self._fp(content)
        if not fp:
            return False
        if self.db.execute("SELECT 1 FROM tombstones WHERE fp=?", (fp,)).fetchone():
            return False
        self.db.execute("INSERT INTO tombstones(fp, content, created) VALUES(?,?,?)",
                        (fp, content[:500], self.clock()))
        self.db.commit()
        return True

    def clear_tombstones(self) -> int:
        n = self.db.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]
        self.db.execute("DELETE FROM tombstones")
        self.db.commit()
        return n

    # ================= 遗忘门：权重 =================
    def weight(self, it, now: float | None = None) -> float:
        now = now if now is not None else self.clock()
        dt_h = max(0.0, (now - it["last_access"]) / 3600.0)
        boost = 1 + self.beta * math.log1p(it["access_count"])
        if _pinned(it):
            return it["importance"] * boost      # 永久事实不衰减
        return it["importance"] * math.exp(-dt_h / it["tau_h"]) * boost

    def touch(self, ids: Iterable[int], now: float | None = None) -> int:
        """只有"真的用到"才刷新：衰减计时归零、访问次数 +1、从冷存储拉回。"""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        now = now if now is not None else self.clock()
        with self.lock:
            self.db.executemany(
                "UPDATE items SET last_access=?, access_count=access_count+1, status='hot' WHERE id=?",
                [(now, i) for i in ids])
            self.db.commit()
        return len(ids)

    # ================= 输出门：分层检索 =================
    def search(self, query: str, path: str, *, k: int = 8, extra_paths: Iterable[str] = (),
               include_turns: bool = True, min_score: float = 0.02, touch: bool = True,
               include_cold: bool = False, public: bool = False, allow_projects: bool = True,
               allowed_trusts: Iterable[str] | None = None, count: bool = True) -> list[Item]:
        """先搜当前分支子树，结果不够就逐级回退：父分支 → ... → core → 全局（全局打折最狠）

        隐私规则（回退到别的分支时生效）：
          - 原始聊天记录(turn)不出本会话，跨分支只召回提炼过的条目（事实/决策/摘要…）
          - 别人的私聊、私信、人物档案永远不可见
          - allow_projects=False 时项目记忆不可见（QQ/B站场景默认如此）
          - public=True（B站评论区等公开场景）时，其他聊天分支的内容一律不可见
          - allowed_trusts：跨分支回退时只召回这些可信级别的条目。本分支不受限
            （本群/本私聊里的人说的话，本来就属于这个场景）。None 表示不限制（调试用）。
        """
        q = fts_query(query)
        if not q:
            return []
        if include_cold:
            min_score *= 0.25     # 翻冷存储是主动回溯，门槛放低
        qtok = token_weights(query)
        qv = None
        if self.embedder is not None:
            got = self.embedder.embed([query])
            qv = got[0] if got else None
        # 顺序：当前分支 → 指定的额外分支（对方档案、她自己）→ 逐级父分支 → core → 全局
        # 当前分支和额外分支一定都会搜完，之后才允许"凑够 k 条就停"
        parts = path.split("/")
        scopes: list[tuple[str, float]] = [(path, 1.0)] + [(p, 0.9) for p in dict.fromkeys(extra_paths) if p and p != path]
        must = len(scopes)
        disc = 0.7
        for i in range(len(parts) - 1, 0, -1):
            scopes.append(("/".join(parts[:i]), disc))
            disc *= 0.7
        scopes.append(("core", 0.8))
        scopes.append(("", 0.35))
        now = self.clock()
        found: dict[int, Item] = {}
        # pinned 只豁免"转冷"，不豁免用户删除（deleted）和合并（merged）
        status_sql = ("items.status IN ('hot','cold')" if include_cold
                      else "(items.status='hot' OR (COALESCE(items.pinned,0)=1 AND items.status='cold'))")
        kind_sql = "" if include_turns else " AND items.kind!='turn'"
        # 隐私隔离：别人的私聊/私信、别人的人物档案，在回退检索时不可见
        extras = [p for p in dict.fromkeys(extra_paths) if p and p != path]
        hidden = ["chat/%/private/%", "chat/%/dm/%", "core/people/%"]
        if not allow_projects:
            hidden.append("project/%")
        if public:
            hidden.append("chat/%")
        hidden_sql = " OR ".join("items.path LIKE '%s'" % h for h in hidden)
        trusts = [t for t in (allowed_trusts or ()) if t in TRUST_RANK]
        trust_sql = (" AND items.trust IN (%s)" % ",".join("?" * len(trusts))) if trusts else ""
        targs = tuple(trusts)
        # 只有当前分支本身不受可信度限制；额外分支（对方档案、core/self）和回退分支都要过滤，
        # 否则群里陌生人诱导出的 core/self 条目会经由 extra_paths 进入本地 agent 的上下文。
        extra_in = ("items.path IN (%s) OR " % ",".join("?" * len(extras))) if extras else ""
        priv_sql = (" AND (items.path=? OR ((%s(items.kind!='turn' AND NOT (%s)))%s))"
                    % (extra_in, hidden_sql, trust_sql))
        pargs = (path, *extras, *targs)
        with self.lock:
            for si, (scope, d) in enumerate(scopes):
                if scope == path:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}"
                    wargs: tuple = (scope, scope + "/%")
                elif scope in extras:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}{trust_sql}"
                    wargs = (scope, scope + "/%", *targs)
                elif scope:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}{priv_sql}"
                    wargs = (scope, scope + "/%", *pargs)
                else:
                    where = f"{status_sql}{kind_sql}{priv_sql}"
                    wargs = pargs
                cand: dict[int, list] = {}   # id -> [row, 字词相关度, 语义相关度]
                for r in self.db.execute(
                        "SELECT items.* FROM items_fts JOIN items ON items.id=items_fts.rowid "
                        f"WHERE items_fts MATCH ? AND {where} ORDER BY bm25(items_fts) LIMIT 40", (q, *wargs)):
                    if r["id"] not in found:
                        cand[r["id"]] = [r, overlap(qtok, r["content"] + " " + (r["tags"] or "")), 0.0]
                if qv is not None:
                    rows = self.db.execute(
                        f"SELECT items.*, vecs.v AS _v FROM items JOIN vecs ON vecs.id=items.id WHERE {where}",
                        wargs).fetchall()
                    sims = cosines(qv, [r["_v"] for r in rows])
                    top = sorted(((sm, r) for sm, r in zip(sims, rows) if sm >= self.sem_floor),
                                 key=lambda x: -x[0])[:20]
                    for sm, r in top:
                        if r["id"] in found:
                            continue
                        sem = min(1.0, (sm - self.sem_floor) / (0.85 - self.sem_floor))
                        if r["id"] in cand:
                            cand[r["id"]][2] = sem
                        else:
                            cand[r["id"]] = [r, overlap(qtok, r["content"] + " " + (r["tags"] or "")), sem]
                for iid, (r, lex, sem) in cand.items():
                    rel = 1 - (1 - lex) * (1 - sem)      # 字词 / 语义 任一命中即可，两者都命中更高
                    if r["kind"] == "turn" and is_question(r["content"]):
                        rel *= 0.3                       # 以前问过的问题 ≠ 答案
                    w = self.weight(r, now)
                    if include_cold:
                        w = max(w, self.cold_threshold)
                    score = rel * w * d
                    if score < min_score:
                        continue
                    it = _item(r); it.score = score; it.weight_now = w
                    found[iid] = it
                if len(found) >= k and si >= must - 1:
                    break
            ranked = sorted(found.values(), key=lambda x: -x.score)
            # 去重：原话(turn)和从它提炼出的条目几乎一样时只留提炼条目
            res: list[Item] = []
            for it in sorted(ranked, key=lambda x: (x.kind == "turn", -x.score)):
                if any(jaccard(it.content, o.content) > 0.6 for o in res):
                    continue
                res.append(it)
            res = sorted(res, key=lambda x: -x.score)[:k]
            if touch and res:
                if count:
                    self.db.executemany(
                        "UPDATE items SET last_access=?, access_count=access_count+1, status='hot' WHERE id=?",
                        [(now, it.id) for it in res])
                else:
                    # 只刷新"多久没想起"，不增加访问次数——避免"越注入越重要"的正反馈
                    self.db.executemany(
                        "UPDATE items SET last_access=?, status='hot' WHERE id=?",
                        [(now, it.id) for it in res])
                self.db.commit()
        return res

    def reindex(self) -> int:
        """重建全文索引（升级分词/概念词典后调用）"""
        with self.lock:
            rows = self.db.execute("SELECT id, content, tags, speaker FROM items").fetchall()
            self.db.execute("DELETE FROM items_fts")
            self.db.executemany("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                                [(r["id"], index_text(r["content"] + " " + (r["tags"] or "") + " " + (r["speaker"] or "")))
                                 for r in rows])
            self.db.commit()
            self.meta_set("index_version", INDEX_VERSION)
        return len(rows)

    def recent_turns(self, path: str, n: int = 12) -> list[Item]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM items WHERE path=? AND kind='turn' ORDER BY created DESC, id DESC LIMIT ?",
                (path, n)).fetchall()
        return [_item(r) for r in reversed(rows)]

    def by_path(self, path: str, kinds: Iterable[str] | None = None, limit: int = 20) -> list[Item]:
        sql = "SELECT * FROM items WHERE (path=? OR path LIKE ?) AND (status='hot' OR (COALESCE(pinned,0)=1 AND status='cold'))"
        args: list = [path, path + "/%"]
        if kinds:
            kinds = list(kinds)
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += kinds
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY importance DESC, last_access DESC LIMIT ?",
                                   args + [limit]).fetchall()
        return [_item(r) for r in rows]

    def list_projects(self) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT substr(path, 9) AS p FROM items WHERE path LIKE 'project/%'").fetchall()
        return sorted({r["p"].split("/")[0] for r in rows if r["p"]})

    def move_path(self, old: str, new: str) -> int:
        """把某个分支下的条目迁到新路径（人物档案合并用）。返回迁移条数。"""
        if not old or old == new:
            return 0
        with self.lock:
            rows = self.db.execute("SELECT id FROM items WHERE path=? OR path LIKE ?",
                                   (old, old + "/%")).fetchall()
            if not rows:
                return 0
            self.db.execute("UPDATE items SET path=? WHERE path=?", (new, old))
            self.db.execute("UPDATE items SET path=? || substr(path, ?) WHERE path LIKE ?",
                            (new, len(old) + 1, old + "/%"))
            self.db.commit()
            # Markdown 镜像也要跟着搬：否则新目录的 notes.md 里没有这些行，
            # 下次 sync_markdown() 会把迁过来的条目当成"用户删掉了"（标 deleted + 墓碑）
            self._move_mirror(old, new, [r["id"] for r in rows])
        return len(rows)

    def _move_mirror(self, old: str, new: str, ids: list[int]) -> None:
        NL = "\n"
        src_root = os.path.join(self.md_root, *old.split("/"))
        if os.path.isdir(src_root):
            for d, _, files in os.walk(src_root, topdown=False):
                rel = os.path.relpath(d, src_root)
                dst = os.path.join(self.md_root, *new.split("/"), *([] if rel == "." else [rel]))
                os.makedirs(dst, exist_ok=True)
                for fn in files:
                    if fn.endswith(".tmp"):
                        continue
                    sp, tp = os.path.join(d, fn), os.path.join(dst, fn)
                    with open(sp, encoding="utf-8") as f:
                        text = f.read()
                    if os.path.exists(tp):
                        with open(tp, encoding="utf-8") as f:
                            cur = f.read()
                        text = cur + ("" if not cur or cur.endswith(NL) else NL) + text
                    tmp = tp + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(text)
                    os.replace(tmp, tp)
                    os.remove(sp)
                try:
                    os.rmdir(d)
                except OSError:
                    pass
        # 数据库里有、但 Markdown 里没有对应行的条目（比如当初写入时没镜像），补一行，
        # 保证同步时不会被误判为"已删除"
        if not ids:
            return
        rows = self.db.execute(
            "SELECT * FROM items WHERE id IN (%s) AND kind!='turn' AND status NOT IN ('merged','deleted')"
            % ",".join("?" * len(ids)), tuple(ids)).fetchall()
        cache: dict[str, str] = {}
        for r in rows:
            fn = os.path.join(self.md_root, *r["path"].split("/"), self._md_name(r["kind"]))
            if fn not in cache:
                try:
                    with open(fn, encoding="utf-8") as f:
                        cache[fn] = f.read()
                except OSError:
                    cache[fn] = ""
            if re.search(r"<!--\s*id=%d(?!\d)" % r["id"], cache[fn]):
                continue
            self._mirror(r["path"], r["kind"], r["content"], r["id"], r["importance"],
                         r["speaker"], r["role"], r["created"])
            cache[fn] += "<!-- id=%d -->" % r["id"]

    # ================= 遗忘门：维护 =================
    def maintain(self) -> dict:
        now = self.clock()
        cooled = 0
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM items WHERE status='hot' AND COALESCE(pinned,0)=0").fetchall()
            ids = [r["id"] for r in rows if self.weight(r, now) < self.cold_threshold]
            if ids:
                self.db.executemany("UPDATE items SET status='cold' WHERE id=?", [(i,) for i in ids])
                self.db.commit()
                cooled = len(ids)
        return {"cooled": cooled}

    # ================= 细胞状态：合并摘要 =================
    def consolidation_candidates(self, min_count: int = 30) -> list[tuple[str, list[Item]]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT path, COUNT(*) c FROM items WHERE kind='turn' AND status='cold' GROUP BY path HAVING c>=?",
                (min_count,)).fetchall()
            out = []
            for r in rows:
                its = self.db.execute(
                    "SELECT * FROM items WHERE path=? AND kind='turn' AND status='cold' ORDER BY created LIMIT 200",
                    (r["path"],)).fetchall()
                out.append((r["path"], [_item(x) for x in its]))
        return out

    def commit_summary(self, path: str, summary: str, source_ids: list[int], importance: float = 0.6) -> int:
        # 摘要的可信度 = 被摘要原文里最低的那个。否则把群聊摘要变成 owner 会绕过隔离。
        trust = "owner"
        rows = self.db.execute(
            "SELECT trust FROM items WHERE id IN (%s)" % ",".join("?" * len(source_ids)),
            tuple(source_ids)).fetchall() if source_ids else []
        for r in rows:
            trust = stricter(trust, r["trust"] or "owner")
        sid = self.write(path, "summary", summary, importance, dedup=False, trust=trust)
        with self.lock:
            self.db.executemany("UPDATE items SET status='merged', parent_id=? WHERE id=?",
                                [(sid, i) for i in source_ids])
            self.db.commit()
        return sid

    # ================= 关系（好感度） =================
    def relation(self, platform: str, uid: str) -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM relations WHERE platform=? AND uid=?", (platform, uid)).fetchone()
        return dict(r) if r else {"platform": platform, "uid": uid, "name": "", "affinity": 0.5,
                                  "interactions": 0, "last_seen": None}

    def update_relation(self, platform: str, uid: str, name: str, affinity: float) -> None:
        now = self.clock()
        with self.lock:
            self.db.execute(
                "INSERT INTO relations(platform,uid,name,affinity,interactions,last_seen) VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(platform,uid) DO UPDATE SET name=CASE WHEN excluded.name!='' THEN excluded.name ELSE name END,"
                " affinity=excluded.affinity, interactions=interactions+1, last_seen=excluded.last_seen",
                (platform, uid, name, affinity, now))
            self.db.commit()

    # ================= 元数据 =================
    def meta_get(self, k: str, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def meta_set(self, k: str, v) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
            self.db.commit()

    def stats(self) -> dict:
        with self.lock:
            rows = self.db.execute(
                "SELECT split.top AS top, status, COUNT(*) c FROM (SELECT CASE WHEN instr(path,'/')>0 "
                "THEN substr(path,1,instr(path,'/')-1) ELSE path END AS top, status FROM items) split "
                "GROUP BY top, status").fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["top"], {})[r["status"]] = r["c"]
        return out

    # ================= Markdown 回写 =================
    MD_FILES = {"decisions.md": ("decision",), "todo.md": ("todo",), "summaries.md": ("summary",),
                "notes.md": ("fact", "preference", "person", "self")}
    _MD_LINE = re.compile(r"^(\s*[-*]\s+)(?:\[(\w+)\]\s*)?(.*?)\s*(<!--\s*id=(\d+)[^>]*-->)?\s*$")

    def sync_markdown(self, force: bool = False) -> dict:
        """把人在 Markdown 镜像里做的修改同步回数据库：
             改了某一行的文字 → 更新那条记忆（重建索引/向量）
             删掉某一行       → 那条记忆标记为 deleted（不再被检索；数据库里仍保留可恢复）
             新加一行 "- xxx" → 作为一条新记忆写入（重要性 0.85），并在行尾补上 id 注释
           只处理 notes/decisions/todo/summaries.md；按月的对话日志是只追加的流水，不回写。"""
        stats = {"files": 0, "updated": 0, "deleted": 0, "added": 0}
        now = self.clock()
        for d, _, files in os.walk(self.md_root):
            for fn in files:
                if fn not in self.MD_FILES:
                    continue
                full = os.path.join(d, fn)
                rel = os.path.relpath(d, self.md_root).replace(os.sep, "/")
                mt = os.path.getmtime(full)
                key = "md_mtime:" + rel + "/" + fn
                if not force and self.meta_get(key) == mt:
                    continue
                stats["files"] += 1
                self._sync_file(full, rel, self.MD_FILES[fn], now, stats)
                self.meta_set(key, os.path.getmtime(full))
        return stats

    def _sync_file(self, full: str, path: str, kinds: tuple, now: float, stats: dict) -> None:
        with open(full, encoding="utf-8") as f:
            lines = f.read().split("\n")
        seen: set[int] = set()
        changed_file = False
        with self.lock:
            for li, line in enumerate(lines):
                m = self._MD_LINE.match(line)
                if not m or not m.group(3).strip():
                    continue
                prefix, kind, content, _, iid = m.groups()
                content = content.strip()
                kind = kind if kind in DEFAULT_TAU_HOURS and kind != "turn" else kinds[0]
                if iid:
                    iid = int(iid); seen.add(iid)
                    r = self.db.execute("SELECT * FROM items WHERE id=? AND path=?", (iid, path)).fetchone()
                    if r is None:
                        continue
                    if r["content"] != content or r["kind"] != kind or r["status"] == "deleted":
                        self.db.execute("UPDATE items SET content=?, kind=?, status='hot', last_access=? WHERE id=?",
                                        (content, kind, now, iid))
                        self.db.execute("DELETE FROM items_fts WHERE rowid=?", (iid,))
                        self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                                        (iid, index_text(content + " " + (r["tags"] or ""))))
                        self.db.execute("DELETE FROM vecs WHERE id=?", (iid,))
                        stats["updated"] += 1
                else:
                    cur = self.db.execute(
                        "INSERT INTO items(path,kind,role,speaker,content,importance,tau_h,created,last_access,tags)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (path, kind, "", "", content, 0.85, float(self.tau.get(kind, 72)), now, now, "manual"))
                    new_id = cur.lastrowid; seen.add(new_id)
                    self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)", (new_id, index_text(content + " manual")))
                    lines[li] = f"{prefix}[{kind}] {content}  <!-- id={new_id} imp=0.85 {time.strftime('%Y-%m-%d', time.localtime(now))} manual -->"
                    changed_file = True
                    stats["added"] += 1
            q = ("SELECT id, content FROM items WHERE path=? AND kind IN (%s) "
                 "AND status NOT IN ('merged','deleted') AND created < ?" % ",".join("?" * len(kinds)))
            gone = [(r["id"], r["content"]) for r in self.db.execute(q, (path, *kinds, now - 5))
                    if r["id"] not in seen]
            if gone:
                for _iid, content in gone:
                    self.add_tombstone(content)      # 用户删掉的，自动提炼不再复活
                self.db.executemany("UPDATE items SET status='deleted' WHERE id=?",
                                    [(i,) for i, _ in gone])
                stats["deleted"] += len(gone)
            self.db.commit()
        if changed_file:
            tmp = full + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            os.replace(tmp, full)
        if self.embedder is not None:
            self.backfill_embeddings()

    # ================= Markdown 镜像 =================
    MD_NAMES = {"decision": "decisions.md", "todo": "todo.md", "summary": "summaries.md"}

    @classmethod
    def _md_name(cls, kind: str) -> str:
        return cls.MD_NAMES.get(kind, "notes.md")

    def _mirror(self, path, kind, content, iid, imp, speaker, role, ts) -> None:
        d = os.path.join(self.md_root, *path.split("/"))
        os.makedirs(d, exist_ok=True)
        tm = time.localtime(ts)
        if kind == "turn":
            fn = os.path.join(d, time.strftime("%Y-%m", tm) + ".md")
            who = speaker or role
            line = f"- `{time.strftime('%m-%d %H:%M', tm)}` **{who}**: {content.replace(chr(10), ' ')}\n"
        else:
            fn = os.path.join(d, self._md_name(kind))
            line = f"- [{kind}] {content.replace(chr(10), ' ')}  <!-- id={iid} imp={imp:.2f} {time.strftime('%Y-%m-%d', tm)} -->\n"
        with open(fn, "a", encoding="utf-8") as f:
            f.write(line)

    def _rewrite_mirror_line(self, path, kind, iid, content, imp, ts) -> bool:
        """去重合并后，把 Markdown 里对应的那一行改成新内容。

        不回写的话，下次 sync_markdown() 会认为"Markdown 才是最新的"，把数据库改回旧内容。
        """
        if kind == "turn":
            return False
        fn = os.path.join(self.md_root, *path.split("/"), self._md_name(kind))
        if not os.path.exists(fn):
            return False
        try:
            with open(fn, encoding="utf-8") as f:
                lines = f.read().split("\n")
        except OSError:
            return False
        out, changed = [], False
        for line in lines:
            m = self._MD_LINE.match(line)
            if m and m.group(5) and int(m.group(5)) == int(iid):
                prefix = m.group(1)
                out.append(f"{prefix}[{kind}] {content}  "
                           f"<!-- id={iid} imp={imp:.2f} {time.strftime('%Y-%m-%d', time.localtime(ts))} -->")
                changed = True
            else:
                out.append(line)
        if not changed:
            return False
        tmp = fn + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        os.replace(tmp, fn)
        return True

    def close(self):
        with self.lock:
            self.db.close()


def _pinned(it) -> bool:
    try:
        return bool(it["pinned"])
    except (IndexError, KeyError, TypeError):
        return bool(getattr(it, "pinned", 0))


def _item(r) -> Item:
    keys = Item.__dataclass_fields__.keys()
    return Item(**{k: r[k] for k in r.keys() if k in keys})
