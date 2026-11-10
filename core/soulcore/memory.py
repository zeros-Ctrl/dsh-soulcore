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

from .textutil import index_text, fts_query, jaccard, token_weights, overlap
from .embedding import cosines, pack
from .extractor import is_question

INDEX_VERSION = 2   # v2：索引里加入概念 token（见 concepts.py）

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
  parent_id INTEGER
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
"""


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
        if self.meta_get("index_version", 1) < INDEX_VERSION:
            self.reindex()

    # ================= 输入门：写入 =================
    def write(self, path: str, kind: str, content: str, importance: float, *, role: str = "",
              speaker: str = "", tags: Iterable[str] = (), user_id: str = "", dedup: bool = True,
              ts: float | None = None, mirror: bool = True) -> Optional[int]:
        content = (content or "").strip()
        if not content or importance < self.input_floor:
            return None
        now = ts if ts is not None else self.clock()
        tau = float(self.tau.get(kind, 72))
        if kind == "turn" and "/group/" in path:
            tau *= self.group_turn_tau_scale
        tag_s = " ".join(sorted(set(t.strip("#") for t in tags if t)))
        with self.lock:
            if dedup and kind != "turn":
                dup = self._find_duplicate(path, kind, content)
                if dup is not None:
                    self.db.execute(
                        "UPDATE items SET importance=MAX(importance,?), last_access=?, status='hot',"
                        " content=CASE WHEN length(?)>length(content) THEN ? ELSE content END WHERE id=?",
                        (importance, now, content, content, dup))
                    self.db.commit()
                    return dup
            cur = self.db.execute(
                "INSERT INTO items(path,kind,role,speaker,content,importance,tau_h,created,last_access,tags,user_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (path, kind, role, speaker, content, float(importance), tau, now, now, tag_s, user_id))
            iid = cur.lastrowid
            self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                            (iid, index_text(content + " " + tag_s + " " + speaker)))
            self.db.commit()
        if self.embedder is not None and (kind != "turn" or self.embed_turns):
            self._embed_ids([(iid, content + " " + tag_s)])
        if mirror:
            self._mirror(path, kind, content, iid, importance, speaker, role, now)
        return iid

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
            "WHERE items_fts MATCH ? AND items.path=? AND items.kind=? AND items.status!='merged' "
            "ORDER BY bm25(items_fts) LIMIT 5", (q, path, kind)).fetchall()
        for r in rows:
            if jaccard(r["content"], content) >= thr:
                return r["id"]
        return None

    # ================= 遗忘门：权重 =================
    def weight(self, it, now: float | None = None) -> float:
        now = now if now is not None else self.clock()
        dt_h = max(0.0, (now - it["last_access"]) / 3600.0)
        return it["importance"] * math.exp(-dt_h / it["tau_h"]) * (1 + self.beta * math.log1p(it["access_count"]))

    # ================= 输出门：分层检索 =================
    def search(self, query: str, path: str, *, k: int = 8, extra_paths: Iterable[str] = (),
               include_turns: bool = True, min_score: float = 0.02, touch: bool = True,
               include_cold: bool = False, public: bool = False, allow_projects: bool = True) -> list[Item]:
        """先搜当前分支子树，结果不够就逐级回退：父分支 → ... → core → 全局（全局打折最狠）

        隐私规则（回退到别的分支时生效）：
          - 原始聊天记录(turn)不出本会话，跨分支只召回提炼过的条目（事实/决策/摘要…）
          - 别人的私聊、私信、人物档案永远不可见
          - allow_projects=False 时项目记忆不可见（QQ/B站场景默认如此）
          - public=True（B站评论区等公开场景）时，其他聊天分支的内容一律不可见
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
        scopes: list[tuple[str, float]] = [(path, 1.0)] + [(p, 0.9) for p in extra_paths if p != path]
        must = len(scopes)
        disc = 0.7
        for i in range(len(parts) - 1, 0, -1):
            scopes.append(("/".join(parts[:i]), disc))
            disc *= 0.7
        scopes.append(("core", 0.8))
        scopes.append(("", 0.35))
        now = self.clock()
        found: dict[int, Item] = {}
        status_sql = "items.status IN ('hot','cold')" if include_cold else "items.status='hot'"
        kind_sql = "" if include_turns else " AND items.kind!='turn'"
        # 隐私隔离：别人的私聊/私信、别人的人物档案，在回退检索时不可见
        allowed = [path, *extra_paths]
        hidden = ["chat/%/private/%", "chat/%/dm/%", "core/people/%"]
        if not allow_projects:
            hidden.append("project/%")
        if public:
            hidden.append("chat/%")
        hidden_sql = " OR ".join("items.path LIKE '%s'" % h for h in hidden)
        priv_sql = (" AND (items.path IN (%s) OR (items.kind!='turn' AND NOT (%s)))"
                    % (",".join("?" * len(allowed)), hidden_sql))
        with self.lock:
            for si, (scope, d) in enumerate(scopes):
                if scope in allowed:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}"
                    wargs: tuple = (scope, scope + "/%")
                elif scope:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}{priv_sql}"
                    wargs = (scope, scope + "/%", *allowed)
                else:
                    where = f"{status_sql}{kind_sql}{priv_sql}"
                    wargs = tuple(allowed)
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
                self.db.executemany(
                    "UPDATE items SET last_access=?, access_count=access_count+1, status='hot' WHERE id=?",
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
        sql = "SELECT * FROM items WHERE (path=? OR path LIKE ?) AND status='hot'"
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

    # ================= 遗忘门：维护 =================
    def maintain(self) -> dict:
        now = self.clock()
        cooled = 0
        with self.lock:
            rows = self.db.execute("SELECT * FROM items WHERE status='hot'").fetchall()
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
        sid = self.write(path, "summary", summary, importance, dedup=False)
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
            q = "SELECT id FROM items WHERE path=? AND kind IN (%s) AND status NOT IN ('merged','deleted') AND created < ?" \
                % ",".join("?" * len(kinds))
            gone = [r["id"] for r in self.db.execute(q, (path, *kinds, now - 5)) if r["id"] not in seen]
            if gone:
                self.db.executemany("UPDATE items SET status='deleted' WHERE id=?", [(i,) for i in gone])
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
    def _mirror(self, path, kind, content, iid, imp, speaker, role, ts) -> None:
        d = os.path.join(self.md_root, *path.split("/"))
        os.makedirs(d, exist_ok=True)
        tm = time.localtime(ts)
        if kind == "turn":
            fn = os.path.join(d, time.strftime("%Y-%m", tm) + ".md")
            who = speaker or role
            line = f"- `{time.strftime('%m-%d %H:%M', tm)}` **{who}**: {content.replace(chr(10), ' ')}\n"
        else:
            fn = os.path.join(d, {"decision": "decisions.md", "todo": "todo.md",
                                  "summary": "summaries.md"}.get(kind, "notes.md"))
            line = f"- [{kind}] {content.replace(chr(10), ' ')}  <!-- id={iid} imp={imp:.2f} {time.strftime('%Y-%m-%d', tm)} -->\n"
        with open(fn, "a", encoding="utf-8") as f:
            f.write(line)

    def close(self):
        with self.lock:
            self.db.close()


def _item(r) -> Item:
    keys = Item.__dataclass_fields__.keys()
    return Item(**{k: r[k] for k in r.keys() if k in keys})
