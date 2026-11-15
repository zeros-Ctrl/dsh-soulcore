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
import logging, math, os, re, sqlite3, threading, time, json, shlex
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .textutil import index_text, fts_query, jaccard, token_weights, overlap, tokens
from .embedding import cosines, pack
from .extractor import is_question
from .conflicts import find_superseded, core_text

log = logging.getLogger("soulcore")

INDEX_VERSION = 2   # v2：索引里加入概念 token（见 concepts.py）
SCHEMA_VERSION = 7  # v7：项目身份、来源关联与提炼覆盖范围


def _verification_passed(command: str, output: str) -> bool:
    """Recognize a single test execution and a positive successful summary.

    Searching for test names, collecting tests, shell pipelines and prose alone
    do not establish verification. Unrecognized runners remain ordinary evidence.
    """
    if not command or re.search(r"[\r\n;&|<>`$]", command):
        return False
    try:
        parts = [part.strip('\"\'') for part in shlex.split(command, posix=False)]
    except ValueError:
        return False
    if not parts:
        return False
    executable = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    args = parts[1:]
    forbidden = {"--help", "-h", "--version", "--collect-only", "--co", "--fixtures",
                 "--fixtures-per-test", "--markers", "--trace-config", "--list-tests", "--show-only"}
    if any(arg.split("=", 1)[0].lower() in forbidden or arg == "-N" for arg in args):
        return False
    if re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", executable):
        while args and (args[0] in ("-u", "-B") or executable.startswith("py") and re.fullmatch(r"-3(?:\.\d+)?", args[0])):
            args = args[1:]
        runner = len(args) >= 2 and args[:2] in (["-m", "pytest"], ["-m", "unittest"])
        if args[:2] == ["-m", "pytest"] and "-V" in args:
            runner = False
    elif executable in ("pytest", "pytest.exe", "ctest", "ctest.exe", "jest", "jest.cmd", "jest.exe"):
        runner = True
        if executable.startswith("pytest") and "-V" in args:
            runner = False
    elif executable in ("node", "node.exe"):
        runner = bool(args and args[0] == "--test" and not any(arg in ("--eval", "-e", "--print", "-p") for arg in args))
    elif executable in ("npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"):
        runner = bool(args and (args[0] == "test" or args[:2] == ["run", "test"]))
    else:
        runner = False
    if not runner or re.search(r"\b[1-9]\d*\s+(?:failed|failures?|errors?)\b|\bFAILED\s*(?:\(|:)|#\s*fail\s+[1-9]", output, re.I):
        return False
    if re.search(r"\b[1-9]\d*\s+(?:tests?\s+)?passed\b|\bTests:\s*[1-9]\d*\s+passed\b", output, re.I):
        return True
    if re.search(r"\bRan\s+[1-9]\d*\s+tests?\b", output) and re.search(r"(?m)^OK(?:\s|$)", output):
        return True
    if re.search(r"100% tests passed,\s*0 tests failed out of\s*[1-9]\d*", output, re.I):
        return True
    tests = re.search(r"(?m)^(?:#|ℹ)\s*tests\s+([1-9]\d*)\s*$", output)
    passed = re.search(r"(?m)^(?:#|ℹ)\s*pass\s+([1-9]\d*)\s*$", output)
    failed = re.search(r"(?m)^(?:#|ℹ)\s*fail\s+0\s*$", output)
    return bool(tests and passed and failed and int(passed[1]) <= int(tests[1]))

# 同类组：同一个槽位只该有一个当前值，所以只在组内找"被取代"的旧条目
_KIND_GROUPS = (("fact", "preference", "person", "self"), ("decision",), ("todo",))


def _kind_group(kind: str) -> tuple[str, ...]:
    for g in _KIND_GROUPS:
        if kind in g:
            return g
    return ()

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


def atomic_write(path: str, text: str, retries: int = 5) -> None:
    """原子写文件：先写 `.tmp`，再 `os.replace` 换过去。

    直接 `open(...,"w")` 会在截断的瞬间被别的线程读到半个文件；Windows 上杀毒软件和
    索引服务还会短暂占用目标文件，`os.replace` 偶发 `PermissionError`。重试几次就过去了，
    实在写不进去才抛给调用方（Markdown 镜像丢了还能从数据库重建，数据库丢了才是真丢）。
    """
    tmp = path + ".tmp"
    for i in range(max(1, int(retries))):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
            return
        except PermissionError:
            if i >= retries - 1:
                raise
            time.sleep(0.05)

DEFAULT_TAU_HOURS = {
    "turn": 72,            # 原始对话：3 天
    "evidence": 72,        # 工具结果：保留原文，默认不生成向量
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
  superseded_by INTEGER,
  due_at REAL,
  stale_note TEXT,
  audience TEXT DEFAULT 'public',
  source_path TEXT DEFAULT '',
  project_id TEXT DEFAULT '',
  session_id TEXT DEFAULT '',
  source_event_id TEXT DEFAULT '',
  source_ids TEXT DEFAULT '[]',
  epistemic_state TEXT DEFAULT '',
  evidence_type TEXT DEFAULT '',
  evidence TEXT DEFAULT '{}'
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
  created REAL NOT NULL,
  audience TEXT DEFAULT '*',
  user_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS item_sources(
  derived_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
  PRIMARY KEY(derived_id, source_id)
);
CREATE INDEX IF NOT EXISTS ix_item_sources_source ON item_sources(source_id);
CREATE TABLE IF NOT EXISTS extraction_coverage(
  path TEXT NOT NULL, project_id TEXT DEFAULT '', covered_id INTEGER DEFAULT 0, updated REAL NOT NULL,
  session_id TEXT DEFAULT '', omissions TEXT DEFAULT '[]', PRIMARY KEY(path,project_id)
);
"""

# 老库升级：CREATE TABLE IF NOT EXISTS 不会给已有的表加列，这里补 ALTER TABLE
_MIGRATIONS = {
    "trust": "TEXT DEFAULT 'owner'",
    "pinned": "INTEGER DEFAULT 0",
    "valid_from": "REAL",
    "invalid_at": "REAL",
    "superseded_by": "INTEGER",
    "due_at": "REAL",          # 适合跟进的时刻（主动聊天的"跟进"念头用）
    "stale_note": "TEXT",      # 摘要里有过时说法时的备注（不失效整条摘要）
    "audience": "TEXT DEFAULT 'public'",
    "source_path": "TEXT DEFAULT ''",
    "project_id": "TEXT DEFAULT ''",
    "session_id": "TEXT DEFAULT ''",
    "source_event_id": "TEXT DEFAULT ''",
    "source_ids": "TEXT DEFAULT '[]'",
    "epistemic_state": "TEXT DEFAULT ''",
    "evidence_type": "TEXT DEFAULT ''",
    "evidence": "TEXT DEFAULT '{}'",
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
    due_at: Optional[float] = None
    stale_note: Optional[str] = None     # 摘要里混着旧说法时的一句话提醒（整条摘要仍然有效）
    score: float = 0.0
    weight_now: float = 0.0
    rel: float = 0.0        # 只算"相关性"、不算权重和时间衰减的得分（冷存储回想按它判断值不值得捞）
    audience: str = "public"
    source_path: str = ""
    project_id: str = ""
    session_id: str = ""
    source_event_id: str = ""
    source_ids: list[int] = field(default_factory=list)
    epistemic_state: str = ""
    evidence_type: str = ""
    evidence: dict = field(default_factory=dict)


class MemoryStore:
    def __init__(self, data_dir: str, cfg: dict | None = None, clock=time.time, embedder=None):
        cfg = cfg or {}
        self.embedder = embedder            # 可选：CachedEmbedder（见 embedding.py）
        self.embed_turns = cfg.get("embed_turns", False)
        self.embed_evidence = cfg.get("embed_evidence", False)
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
        self.auto_supersede = cfg.get("auto_supersede", True)
        self.time_boost = float(cfg.get("time_boost", 2.0))
        # Markdown 镜像的锁：所有"读 → 改 → os.replace"都必须串行，否则后台提炼线程、
        # tick 线程、HTTP 线程并发写同一个 notes.md 时会互相覆盖，被覆盖的行下一轮
        # sync_markdown() 会判成"用户删了"，直接丢记忆。
        # 锁顺序固定为 **先 md_lock 后 self.lock**（self.lock 也保护 sqlite 连接）。
        self.md_lock = threading.RLock()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(data_dir, "memory.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._migrate()
        from .projects import ProjectRegistry
        self.projects = ProjectRegistry(self.db, self.lock, self.clock)
        if self.meta_get("index_version", 1) < INDEX_VERSION:
            self.reindex()

    # ================= 迁移 =================
    def _migrate(self) -> None:
        """给旧库补列，按原路径回填来源与聊天可见范围。"""
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
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_items_project ON items(project_id,path)")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_items_event ON items(source_event_id,role,source_path)")
        if "audience" in added:
            for row in self.db.execute("SELECT id,path,trust FROM items").fetchall():
                self.db.execute("UPDATE items SET audience=?,source_path=? WHERE id=?",
                                (self._default_audience(row["path"], row["trust"]),
                                 row["path"] if row["path"].startswith(("chat/", "project/")) else "", row["id"]))
        tombstone_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(tombstones)")}
        for name, declaration in (("audience", "TEXT DEFAULT '*'"), ("user_id", "TEXT DEFAULT ''")):
            if name not in tombstone_cols:
                self.db.execute(f"ALTER TABLE tombstones ADD COLUMN {name} {declaration}")
        self.db.commit()
        if self.meta_get("schema_version", 1) < SCHEMA_VERSION:
            self.meta_set("schema_version", SCHEMA_VERSION)

    # ================= 输入门：写入 =================
    def write(self, path: str, kind: str, content: str, importance: float, *, role: str = "",
              speaker: str = "", tags: Iterable[str] = (), user_id: str = "", dedup: bool = True,
              ts: float | None = None, mirror: bool = True, trust: str = "owner",
              pinned: bool = False, by_tombstone_ok: bool = False,
              due_at: float | None = None, valid_from: float | None = None,
              reconcile: bool = True, audience: str | None = None, source_path: str = "",
              project_id: str = "", session_id: str = "", source_event_id: str = "",
              source_ids: Iterable[int] = (), epistemic_state: str = "",
              evidence_type: str = "", evidence: dict | None = None) -> Optional[int]:
        """写一条记忆。

        trust          来源可信度（见 TRUST_LEVELS）。检索时按场景过滤，默认 owner。
        pinned         永久事实（名字/生日/过敏…）：不参与衰减，也不会转冷。
        by_tombstone_ok 用户主动写入（remember 工具）时绕过墓碑；自动提炼不绕过。
        valid_from     这件事是什么时候说的 / 什么时候成立的。提炼时传原话的时间；
                       检索的"时间感知"按它（而不是入库时间）判断条目属于哪一天。
        reconcile      写入前先做规则版冲突检测：新说法真的改了旧值时，把旧条目标成"已过时"。
        """
        content = content or ""
        if kind not in ("turn", "evidence"):
            content = content.strip()
        if not content.strip() or importance < self.input_floor:
            return None
        now = ts if ts is not None else self.clock()
        trust = trust if trust in TRUST_RANK else "owner"
        user_id = str(user_id or "")
        audience = str(audience) if audience is not None else self._default_audience(path, trust)
        source_path = str(source_path or (path if path.startswith(("chat/", "project/")) else ""))
        project_id, session_id = str(project_id or ""), str(session_id or "")
        source_event_id = str(source_event_id or "")
        source_ids = list(dict.fromkeys(int(i) for i in source_ids if int(i) > 0))
        evidence = dict(evidence or {})
        if epistemic_state not in ("", "suggested", "accepted", "implemented", "verified"):
            raise ValueError("unknown epistemic_state")
        tau = float(self.tau.get(kind, 72))
        if kind == "turn" and "/group/" in path:
            tau *= self.group_turn_tau_scale
        tag_s = " ".join(sorted(set(t.strip("#") for t in tags if t)))
        merged = None
        replaced: list[int] = []
        with self.lock:
            # Idempotence uses source identity, never the text: repeating the same
            # sentence in a later turn remains a distinct event.
            if source_event_id and kind in ("turn", "evidence"):
                prior = self.db.execute(
                    "SELECT id,content FROM items WHERE source_event_id=? AND role=? AND source_path=? "
                    "AND project_id=? AND session_id=? ORDER BY id DESC",
                    (source_event_id, role, source_path, project_id, session_id)).fetchone()
                if prior:
                    exact = self.db.execute(
                        "SELECT id FROM items WHERE source_event_id=? AND role=? AND source_path=? "
                        "AND project_id=? AND session_id=? AND content=? ORDER BY id DESC LIMIT 1",
                        (source_event_id, role, source_path, project_id, session_id, content)).fetchone()
                    if exact:
                        return exact["id"]
                    # A fuller or edited provider event is a revision, never silently lost.
                    evidence["revision_of"] = prior["id"]
            if source_ids:
                sources = self.db.execute("SELECT * FROM items WHERE id IN (%s)" %
                                          ",".join("?" * len(source_ids)), source_ids).fetchall()
                if len(sources) != len(source_ids) or any(r["status"] == "deleted" for r in sources):
                    return None
                for source in sources:
                    trust = stricter(trust, source["trust"] or "owner")
                    if source["audience"] not in ("public", audience):
                        return None
                    if project_id and source["project_id"] and project_id != source["project_id"]:
                        return None
            if epistemic_state == "verified" and not self._verified_sources(source_ids, evidence):
                epistemic_state = "implemented"
            if kind != "turn" and not by_tombstone_ok and self._tombstoned(content, audience=audience, user_id=user_id):
                return None      # 用户删过这条：自动提炼不再把它复活
            if reconcile and kind != "turn" and self.auto_supersede:
                replaced = self._reconcile(path, kind, content, user_id=user_id, audience=audience,
                                           project_id=project_id)
            if not replaced and dedup and kind != "turn":
                dup = self._find_duplicate(path, kind, content, user_id=user_id, audience=audience,
                                            project_id=project_id)
                if dup is not None:
                    merged = self._merge_duplicate(dup, path, kind, content, importance, now, tag_s, trust)
            if merged is None:
                cur = self.db.execute(
                    "INSERT INTO items(path,kind,role,speaker,content,importance,tau_h,created,last_access,"
                    "tags,user_id,trust,pinned,valid_from,due_at,audience,source_path,project_id,session_id,"
                    "source_event_id,source_ids,epistemic_state,evidence_type,evidence) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path, kind, role, speaker, content, float(importance), tau, now, now, tag_s, user_id,
                     trust, 1 if pinned else 0, valid_from if valid_from is not None else now, due_at,
                     audience, source_path, project_id, session_id, source_event_id,
                     json.dumps(source_ids), epistemic_state, evidence_type,
                     json.dumps(evidence, ensure_ascii=False)))
                iid = cur.lastrowid
                self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                                (iid, index_text(content + " " + tag_s + " " + speaker)))
                self.db.commit()
            else:
                iid = merged
                previous_state = self.db.execute("SELECT epistemic_state FROM items WHERE id=?", (iid,)).fetchone()[0]
                states = ("", "suggested", "accepted", "implemented", "verified")
                if epistemic_state and states.index(epistemic_state) >= states.index(previous_state or ""):
                    self.db.execute("UPDATE items SET epistemic_state=?,evidence=? WHERE id=?",
                                    (epistemic_state, json.dumps(evidence, ensure_ascii=False), iid))
                    self.db.commit()
                if due_at is not None:
                    self.db.execute("UPDATE items SET due_at=? WHERE id=? AND due_at IS NULL", (due_at, iid))
                    self.db.commit()
            if source_ids:
                previous = self.db.execute("SELECT source_ids FROM items WHERE id=?", (iid,)).fetchone()
                combined = list(dict.fromkeys([*_json_list(previous["source_ids"]), *source_ids]))
                self.db.execute("UPDATE items SET source_ids=? WHERE id=?", (json.dumps(combined), iid))
                self.db.executemany("INSERT OR IGNORE INTO item_sources VALUES(?,?)",
                                    [(iid, source) for source in source_ids if source != iid])
                self.db.commit()
        if kind != "turn":
            with self.lock:
                self._changed()
                self.db.commit()
        # 知识更新：新写法明确改了旧值 → 旧条目失效（不删除，问"以前"时还能召回）
        for old in replaced:
            self.supersede(old, iid, at=now, new_trust=trust)
        row = None
        if merged is not None:
            row = self.db.execute("SELECT content, importance FROM items WHERE id=?", (iid,)).fetchone()
        if self.embedder is not None and (kind not in ("turn", "evidence")
                or (kind == "turn" and self.embed_turns) or (kind == "evidence" and self.embed_evidence)):
            self._embed_ids([(iid, ((row["content"] if row else content) + " " + tag_s))])
        if mirror:
            if merged is None:
                self._mirror(path, kind, content, iid, importance, speaker, role, now)
            else:
                self._rewrite_mirror_line(path, kind, iid, row["content"], row["importance"], now)
        return iid

    def resolve_project_event(self, ev, mode: str = "registry"):
        return self.projects.event(ev, mode)

    def _project_filter(self, project_id: str, path: str, prefix: str = "items.") -> tuple[str, tuple]:
        if not project_id:
            return "", ()
        legacy = not self.projects.collides(project_id)
        sql = f" AND ({prefix}path NOT LIKE 'project/%' OR {prefix}project_id=?"
        args: tuple = (project_id,)
        if legacy:
            sql += f" OR (COALESCE({prefix}project_id,'')='' AND {prefix}path=?)"
            args += (path,)
        return sql + ")", args

    def _verified_sources(self, source_ids: list[int], evidence: dict) -> bool:
        """Batch provenance is insufficient: verification needs selected tool evidence."""
        selected = _json_list(evidence.get("verification_source_ids", []))
        if not selected or any(i not in source_ids for i in selected):
            return False
        for source_id in selected:
            row = self.db.execute("SELECT * FROM items WHERE id=?", (source_id,)).fetchone()
            meta = _json_dict(row["evidence"]) if row else {}
            if (not row or row["status"] == "deleted" or row["kind"] != "evidence"
                    or row["evidence_type"] != "verification" or meta.get("origin") != "tool_result"
                    or meta.get("outcome") != "passed" or type(meta.get("exit_code")) is not int
                    or meta.get("exit_code") != 0
                    or not meta.get("tool_call_id") or row["invalid_at"] is not None
                    or not _verification_passed(str(meta.get("command") or meta.get("tool_name") or ""), row["content"])):
                return False
        return True

    def _restorable_sources(self, row) -> bool:
        source_ids = _json_list(row["source_ids"])
        if source_ids:
            sources = self.db.execute("SELECT status FROM items WHERE id IN (%s)" %
                                      ",".join("?" * len(source_ids)), source_ids).fetchall()
            if len(sources) != len(set(source_ids)) or any(source["status"] == "deleted" for source in sources):
                return False
        return row["epistemic_state"] != "verified" or self._verified_sources(source_ids, _json_dict(row["evidence"]))

    def append_turn(self, ev, role: str, content: str, *, source_event_id: str | None = None,
                    evidence: dict | None = None, trust: str = "owner",
                    audience: str | None = None, importance: float = 0.6,
                    speaker: str | None = None) -> Optional[int]:
        if role not in ("user", "assistant", "tool"):
            raise ValueError("unsupported event role")
        meta = dict(evidence or {})
        evidence_type = ""
        if role == "tool":
            evidence_type = "tool"
            command = str(meta.get("command") or meta.get("tool_name") or "")
            if (meta.get("origin") == "tool_result" and meta.get("tool_call_id")
                    and type(meta.get("exit_code")) is int and meta.get("exit_code") == 0
                    and _verification_passed(command, content)):
                evidence_type = "verification"
                meta["outcome"] = "passed"
        return self.write(ev.path, "evidence" if role == "tool" else "turn", content,
                          importance, role=role, speaker=speaker if speaker is not None else
                          (ev.user_name if role == "user" else role),
                          user_id=ev.user_id, trust=trust, audience=audience, ts=ev.ts,
                          source_path=ev.path, project_id=ev.project_id, session_id=ev.session_id,
                          source_event_id=source_event_id if source_event_id is not None else ev.event_id,
                          evidence_type=evidence_type, evidence=meta, dedup=False,
                          mirror=role != "tool", reconcile=False)

    def mark_extraction_coverage(self, path: str, covered_id: int, *, omissions=(), session_id: str = "",
                                 project_id: str = "") -> None:
        """Only called after successful extraction; the cursor never moves backwards."""
        with self.lock:
            old = self.db.execute("SELECT covered_id FROM extraction_coverage WHERE path=? AND project_id=?",
                                  (path, project_id)).fetchone()
            if old and old["covered_id"] > int(covered_id):
                return
            self.db.execute("INSERT INTO extraction_coverage VALUES(?,?,?,?,?,?) ON CONFLICT(path,project_id) DO UPDATE "
                            "SET covered_id=excluded.covered_id,updated=excluded.updated,"
                            "session_id=excluded.session_id,omissions=excluded.omissions",
                            (path, project_id, int(covered_id), self.clock(), str(session_id or ""),
                             json.dumps(list(omissions), ensure_ascii=False)))
            key = "extract_ptr:" + path + (":" + project_id if project_id else "")
            self.meta_set(key, max(int(self.meta_get(key, 0) or 0), int(covered_id)))
            self.db.commit()

    def coverage(self, path: str, user_id: str | None = None, *, project_id: str = "") -> dict:
        with self.lock:
            state = self.db.execute("SELECT * FROM extraction_coverage WHERE path=? AND project_id=?",
                                    (path, project_id)).fetchone()
            key = "extract_ptr:" + path + (":" + project_id if project_id else "")
            covered = state["covered_id"] if state else int(self.meta_get(key, 0) or 0)
            sql = "SELECT id,source_event_id,session_id FROM items WHERE path=? AND kind='turn' AND status!='deleted'"
            args: tuple = (path,)
            if user_id is not None:
                sql += " AND user_id=?"
                args += (str(user_id),)
            if project_id:
                sql += " AND project_id=?"
                args += (project_id,)
            rows = self.db.execute(sql + " ORDER BY id", args).fetchall()
            backlog = [r for r in rows if r["id"] > covered]
        return {"path": path, "project_id": project_id, "total_turns": len(rows), "latest_id": rows[-1]["id"] if rows else 0,
                "covered_id": covered, "backlog_turns": len(backlog),
                "updated": state["updated"] if state else None,
                "session_id": state["session_id"] if state else "",
                "recent_omissions": _json_list(state["omissions"]) if state else []}

    def _merge_duplicate(self, dup: int, path: str, kind: str, content: str, importance: float,
                         now: float, tag_s: str, trust: str) -> int:
        """去重命中：更新数据库**和** Markdown 镜像、全文索引、向量。

        旧实现只改数据库，下次 sync_markdown() 会以 Markdown 为准把它改回旧内容。
        """
        r = self.db.execute("SELECT content, tags, speaker, trust,epistemic_state FROM items WHERE id=?", (dup,)).fetchone()
        old = r["content"] if r else ""
        new_content = content if len(content) >= 0.8 * len(old) else old
        keep_trust = stricter(r["trust"] if r and r["trust"] else "owner", trust)
        self.db.execute(
            "UPDATE items SET importance=MAX(importance,?), last_access=?, status='hot',"
            " content=?, trust=? WHERE id=?",
            (importance, now, new_content, keep_trust, dup))
        self.db.execute("DELETE FROM items_fts WHERE rowid=?", (dup,))
        self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                        (dup, index_text(new_content + " " + (r["tags"] or tag_s) + " " + (r["speaker"] or ""))))
        self.db.execute("DELETE FROM vecs WHERE id=?", (dup,))
        if r and new_content != old and r["epistemic_state"] == "verified":
            self.db.execute("UPDATE items SET epistemic_state='implemented',evidence='{}' WHERE id=?", (dup,))
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
                + ("" if self.embed_turns else "AND items.kind!='turn' ")
                + ("" if self.embed_evidence else "AND items.kind!='evidence' ") + "LIMIT ?",
                (self.embedder.name, limit)).fetchall()
        n = 0
        for i in range(0, len(rows), batch):
            n += self._embed_ids([(r["id"], r["content"] + " " + (r["tags"] or "")) for r in rows[i:i + batch]])
        return n

    @staticmethod
    def _default_audience(path: str, trust: str = "owner") -> str:
        if (path == "core/people/local/owner" or path.startswith("core/people/local/owner/")
                or path == "core/people/owner" or path.startswith("core/people/owner/")):
            return "owner"
        if path == "core/self" or path.startswith("core/self/"):
            return "owner" if trust == "owner" else "legacy-private"
        if path.startswith("core/people/"):
            parts = path.split("/")
            if len(parts) >= 4:
                return "chat/%s/%s/%s" % (parts[2], "dm" if parts[2] == "bilibili" else "private", parts[3])
            return "legacy-private"
        if "/private/" in path or "/dm/" in path:
            return "owner" if trust == "owner" else path
        if path.startswith("project/"):
            return "owner" if trust == "owner" else path
        if path.startswith("chat/"):
            return path
        return "public"

    @staticmethod
    def _author_filter(path: str, user_id: str) -> tuple[str, tuple]:
        if "/group/" in path or "/comment/" in path:
            return " AND COALESCE(items.user_id,'')=?", (str(user_id or ""),)
        if user_id:
            return " AND (COALESCE(items.user_id,'')=? OR COALESCE(items.user_id,'')='')", (str(user_id),)
        return "", ()

    def _changed(self, clear_profiles: bool = False) -> None:
        """改变记忆内容/有效性时更新缓存版本；普通原话追加与复习不触发。"""
        revision = int(self.meta_get("memory_revision", 0) or 0) + 1
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('memory_revision',?)", (str(revision),))
        if clear_profiles:
            for key, value in (("profile_dirty", "true"), ("profile_owner", '""'), ("profile_self", '""')):
                self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, value))

    def _find_duplicate(self, path: str, kind: str, content: str, thr: float = 0.75, *,
                        user_id: str = "", audience: str = "public", project_id: str = "") -> Optional[int]:
        q = fts_query(content)
        if not q:
            return None
        author_sql, author_args = self._author_filter(path, user_id)
        if project_id:
            author_sql += " AND items.project_id=?"
            author_args += (project_id,)
        rows = self.db.execute(
            "SELECT items.id, items.content FROM items_fts JOIN items ON items.id=items_fts.rowid "
            "WHERE items_fts MATCH ? AND items.path=? AND items.kind=? AND items.audience=? "
            "AND items.status NOT IN ('merged','deleted') AND items.invalid_at IS NULL "
            + author_sql + " ORDER BY bm25(items_fts) LIMIT 5",
            (q, path, kind, audience, *author_args)).fetchall()
        for r in rows:
            if jaccard(r["content"], content) >= thr:
                return r["id"]
        return None

    def _reconcile(self, path: str, kind: str, content: str, *, user_id: str = "",
                   audience: str | None = None, project_id: str = "") -> list[int]:
        """仅同一个发言者的同类事实可以互相取代；旧库空 ID 在非群聊路径兼容。"""
        group = _kind_group(kind)
        q = fts_query(content)
        if not group or not q:
            return []
        marks = ",".join("?" * len(group))
        author_sql, author_args = self._author_filter(path, user_id)
        if project_id:
            author_sql += " AND items.project_id=?"
            author_args += (project_id,)
        if audience is not None:
            author_sql += " AND items.audience=?"
            author_args += (audience,)
        rows = self.db.execute(
            "SELECT items.id, items.content, items.kind FROM items_fts JOIN items ON items.id=items_fts.rowid "
            f"WHERE items_fts MATCH ? AND items.path=? AND items.kind IN ({marks}) "
            "AND items.status NOT IN ('merged','deleted') AND items.invalid_at IS NULL "
            + author_sql + " ORDER BY bm25(items_fts) LIMIT 10", (q, path, *group, *author_args)).fetchall()
        return find_superseded(content, [(r["id"], r["content"], r["kind"]) for r in rows])

    def supersede(self, old_id: int, new_id: int | None, *, at: float | None = None,
                  new_trust: str = "owner") -> bool:
        """让旧条目失效（不删除）。new_id 为 None 表示"作废但没有替代"。

        - 可信度闸门：低可信来源不能推翻高可信记忆（陌生人不能让主人的记忆失效）
        - pinned 也可以被取代：住址这种长期事实搬家后就是要更新
        - Markdown 镜像里在同一行的 id 注释末尾追加 superseded_by=…，正文不动，
          这样 sync_markdown() 不会把它当成"用户删了"或"用户改了"
        """
        try:
            old_id = int(old_id)
        except (TypeError, ValueError):
            return False
        if new_id is not None:
            try:
                new_id = int(new_id)
            except (TypeError, ValueError):
                return False
            if new_id == old_id:
                return False
        with self.lock:
            r = self.db.execute(
                "SELECT id, path, kind, trust, user_id, audience, status, invalid_at,project_id FROM items WHERE id=?", (old_id,)).fetchone()
            if r is None or r["invalid_at"] is not None:
                return False
            if r["status"] in ("deleted", "merged") or r["kind"] == "turn":
                return False
            if TRUST_RANK.get(new_trust, 3) < TRUST_RANK.get(r["trust"] or "owner", 3):
                log.info("拒绝用 %s 来源的记忆取代 #%s（%s）", new_trust, old_id, r["trust"])
                return False
            if new_id is not None:
                new = self.db.execute("SELECT user_id,audience,project_id FROM items WHERE id=?", (new_id,)).fetchone()
                if (new is None or r["audience"] != new["audience"]
                        or r["project_id"] != new["project_id"]
                        or (r["user_id"] and new["user_id"] and r["user_id"] != new["user_id"])):
                    return False
            at = self.clock() if at is None else at
            path, kind = r["path"], r["kind"]
            self.db.execute("UPDATE items SET invalid_at=?, superseded_by=? WHERE id=?",
                            (at, new_id, old_id))
            self._mark_stale_derived(path, old_id, new_id)
            owner_like = path == "core/self" or path.startswith("core/self/") or path.startswith("core/people/")
            if owner_like:
                # 画像里抄了一份旧说法：标脏，下次 tick 用有效条目重算
                self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('profile_dirty','true')")
            self._changed(clear_profiles=owner_like)
            self.db.commit()
        self._annotate_mirror_line(path, kind, old_id, "superseded_by=%d" % (new_id or 0))
        return True

    def _mark_stale_derived(self, path: str, old_id: int, new_id: int | None) -> None:
        """旧说法还留在早期的摘要里：给摘要挂一句 stale_note，但**不**让整条摘要失效。

        整条失效会把摘要里的其他信息一起藏起来（"注册上限每天 20 个"跟着"部署在香橙派"一起消失）。
        渲染时在行尾追加"（注意：…已过时，现为…）"，旧说法不冒充现状，其他内容还在。
        """
        r = self.db.execute("SELECT content,source_path,project_id FROM items WHERE id=?", (old_id,)).fetchone()
        old_tokens = set(tokens(r["content"])) if r else set()
        if len(old_tokens) < 3:
            return
        new_short = ""
        if new_id:
            n = self.db.execute("SELECT content FROM items WHERE id=?", (int(new_id),)).fetchone()
            if n:
                new_short = core_text(str(n["content"]))[:24]
        old_short = core_text(str(r["content"]))[:24]
        note = f"其中「{old_short}」已过时" + (f"（现在：{new_short}）" if new_short else "")
        rows = self.db.execute(
            "SELECT id, content, stale_note FROM items WHERE (path=? OR path=?) AND kind='summary' "
            "AND project_id=? AND invalid_at IS NULL AND status NOT IN ('deleted','merged') LIMIT 200",
            (path, r["source_path"] or path, r["project_id"])).fetchall()
        for x in rows:
            st = set(tokens(x["content"]))
            if not st or len(old_tokens & st) / len(old_tokens) < 0.6:
                continue
            if note in (x["stale_note"] or ""):
                continue
            merged = (x["stale_note"] + "；" if x["stale_note"] else "") + note
            self.db.execute("UPDATE items SET stale_note=? WHERE id=?", (merged[:300], x["id"]))

    # ================= 墓碑：用户删掉的内容不再自动复活 =================
    @staticmethod
    def _fp(content: str) -> str:
        return " ".join(sorted(set(tokens(content))))

    def _tombstoned(self, content: str, *, audience: str | None = None, user_id: str = "") -> bool:
        fp = self._fp(content)
        if not fp:
            return False
        sa = set(fp.split())
        sql = "SELECT fp FROM tombstones"
        args: tuple = ()
        if audience is not None:
            sql += " WHERE audience='*' OR (audience=? AND COALESCE(user_id,'')=?)"
            args = (audience, str(user_id or ""))
        for r in self.db.execute(sql + " ORDER BY id DESC LIMIT 1000", args):
            sb = set((r["fp"] or "").split())
            if not sb:
                continue
            if len(sa & sb) / len(sa | sb) >= 0.9:
                return True
        return False

    def add_tombstone(self, content: str, *, audience: str = "*", user_id: str = "") -> bool:
        fp = self._fp(content)
        if not fp:
            return False
        user_id = str(user_id or "")
        if self.db.execute(
                "SELECT 1 FROM tombstones WHERE fp=? AND audience=? AND COALESCE(user_id,'')=?",
                (fp, audience, user_id)).fetchone():
            return False
        self.db.execute("INSERT INTO tombstones(fp,content,created,audience,user_id) VALUES(?,?,?,?,?)",
                        (fp, content[:500], self.clock(), audience, user_id))
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
        """只有真的用到才刷新；被删除或失效的条目不能复活。"""
        ids = list(dict.fromkeys(int(i) for i in ids))
        if not ids:
            return 0
        now = now if now is not None else self.clock()
        with self.lock:
            cur = self.db.executemany(
                "UPDATE items SET last_access=?, access_count=access_count+1, status='hot' "
                "WHERE id=? AND status IN ('hot','cold') AND invalid_at IS NULL",
                [(now, i) for i in ids])
            self.db.commit()
            return cur.rowcount

    # ================= 输出门：分层检索 =================

    def search(self, query: str, path: str, *, k: int = 8, extra_paths: Iterable[str] = (),
               include_turns: bool = True, min_score: float = 0.02, touch: bool = True,
               include_cold: bool = False, public: bool = False, allow_projects: bool = True,
               allowed_trusts: Iterable[str] | None = None, count: bool = True,
               include_history: bool = False, window: tuple[float, float] | None = None,
               only_cold: bool = False, allowed_audiences: Iterable[str] | None = None,
               project_id: str = "") -> list[Item]:
        """先搜当前分支子树，结果不够就逐级回退：父分支 → ... → core → 全局（全局打折最狠）

        隐私规则（回退到别的分支时生效）：
          - 原始聊天记录(turn)不出本会话，跨分支只召回提炼过的条目（事实/决策/摘要…）
          - 别人的私聊、私信、人物档案永远不可见
          - allow_projects=False 时项目记忆不可见（QQ/B站场景默认如此）
          - public=True（B站评论区等公开场景）时，其他聊天分支的内容一律不可见
          - allowed_trusts / allowed_audiences：所有分支均受过滤，包括当前分支和额外分支。
            None 表示不限制（本地管理/调试用）。

        include_history：失效（被取代/作废）的条目也召回，分数打 6 折并标"已过时"——问"以前那件事"时用。
        window：(起, 止)：问句里解析出的过去时间窗。落在窗口内的条目加分，并在没实词时按时间直接取。
        only_cold：只搜冷存储。冷回想要用它——混着热条目一起排，冷条目永远排不进前几名。
        """
        q = fts_query(query)
        qtok = token_weights(query)
        content_tokens = [t for t in tokens(query) if len(t) > 1]
        if not q and not window:
            return []
        if include_cold or only_cold:
            min_score *= 0.25     # 翻冷存储是主动回溯，门槛放低
        qv = None
        if q and self.embedder is not None:
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
        status_sql = ("items.status='cold'" if only_cold else
                      ("items.status IN ('hot','cold')" if include_cold
                       else "(items.status='hot' OR (COALESCE(items.pinned,0)=1 AND items.status='cold'))"))
        if not include_history:
            status_sql += " AND items.invalid_at IS NULL"
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
        trust_sql = ((" AND items.trust IN (%s)" % ",".join("?" * len(trusts))) if trusts
                     else (" AND 0" if allowed_trusts is not None else ""))
        targs = tuple(trusts)
        audiences = list(dict.fromkeys(allowed_audiences or ()))
        audience_sql = ((" AND items.audience IN (%s)" % ",".join("?" * len(audiences))) if audiences
                        else (" AND 0" if allowed_audiences is not None else ""))
        # 来源可信度和可见范围同时约束当前、额外及回退分支。
        extra_in = ("items.path IN (%s) OR " % ",".join("?" * len(extras))) if extras else ""
        priv_sql = (" AND (items.path=? OR %s(items.kind!='turn' AND NOT (%s)))%s"
                    % (extra_in, hidden_sql, trust_sql))
        pargs = (path, *extras, *targs)
        with self.lock:
            project_sql, project_args = self._project_filter(project_id, path)
            for si, (scope, d) in enumerate(scopes):
                if scope == path:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}{trust_sql}"
                    wargs: tuple = (scope, scope + "/%", *targs)
                elif scope in extras:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}{trust_sql}"
                    wargs = (scope, scope + "/%", *targs)
                elif scope:
                    where = f"(items.path=? OR items.path LIKE ?) AND {status_sql}{kind_sql}{priv_sql}"
                    wargs = (scope, scope + "/%", *pargs)
                else:
                    where = f"{status_sql}{kind_sql}{priv_sql}"
                    wargs = pargs
                where += audience_sql + project_sql
                wargs = (*wargs, *audiences, *project_args)
                cand: dict[int, list] = {}   # id -> [row, 字词相关度, 语义相关度]
                if q:
                    for r in self.db.execute(
                            "SELECT items.* FROM items_fts JOIN items ON items.id=items_fts.rowid "
                            f"WHERE items_fts MATCH ? AND {where} ORDER BY bm25(items_fts) LIMIT 40", (q, *wargs)):
                        if r["id"] not in found:
                            cand[r["id"]] = [r, overlap(qtok, r["content"] + " " + (r["tags"] or "")), 0.0]
                if qv is not None:
                    rows = self.db.execute(
                        f"SELECT items.*, vecs.v AS _v FROM items JOIN vecs ON vecs.id=items.id "
                        f"WHERE {where} AND vecs.model=?",
                        (*wargs, self.embedder.name)).fetchall()
                    # Same-model corrupt/mismatched dimensions also fall back to FTS.
                    rows = [r for r in rows if len(r["_v"]) == len(qv) * 4]
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
                    if include_cold or only_cold:
                        w = max(w, self.cold_threshold)
                    score = rel * w * d
                    if window and _in_window(r, window):
                        score *= self.time_boost         # 问"昨天"的时候，昨天那条更该被想起
                    if include_history and r["invalid_at"] is not None:
                        score *= 0.6                     # 已过时的条目排在后面，但仍然看得见
                    if score < min_score:
                        continue
                    it = _item(r); it.score = score; it.weight_now = w; it.rel = rel
                    found[iid] = it
                # 时间问题常常没有实词（"昨天我们决定了什么"）：按时间直接取那一天的条目。
                # 隐私靠复用本 scope 的 where（含 trust / 隐藏分支条件），每个 scope 各自查一次。
                if window:
                    # 只数**提炼条目**：原始对话也会落在窗口里，但它们随后会被"最近几轮"过滤掉，
                    # 用它们判断"已经想到了"会让按时间直取永不触发（"昨天决定了什么"就这样丢掉答案）
                    in_win = sum(1 for it in found.values()
                                 if it.kind != "turn"
                                 and window[0] <= (it.valid_from if it.valid_from is not None else it.created) <= window[1])
                    if len(content_tokens) < 2 or in_win < 2:
                        rows = self.db.execute(
                            f"SELECT items.* FROM items WHERE {where} AND items.kind!='turn' "
                            "AND COALESCE(items.valid_from, items.created) BETWEEN ? AND ? "
                            "ORDER BY items.importance DESC LIMIT ?",
                            (*wargs, window[0], window[1], k)).fetchall()
                        for r in rows:
                            if r["id"] in found:
                                continue
                            w = self.weight(r, now)
                            it = _item(r); it.rel = 0.4; it.weight_now = w
                            it.score = 0.4 * w * d * self.time_boost
                            if it.score >= min_score:
                                found[r["id"]] = it
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
                        "UPDATE items SET last_access=?, access_count=access_count+1, status='hot' "
                        "WHERE id=? AND status IN ('hot','cold') AND invalid_at IS NULL",
                        [(now, it.id) for it in res])
                else:
                    # 只刷新"多久没想起"，不增加访问次数——避免"越注入越重要"的正反馈
                    self.db.executemany(
                        "UPDATE items SET last_access=?, status='hot' "
                        "WHERE id=? AND status IN ('hot','cold') AND invalid_at IS NULL",
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

    def recent_turns(self, path: str, n: int = 12, *, project_id: str = "") -> list[Item]:
        with self.lock:
            project_sql, project_args = self._project_filter(project_id, path, "")
            rows = self.db.execute(
                "SELECT * FROM items WHERE path=? AND kind='turn' AND status!='deleted' "
                + project_sql +
                "ORDER BY created DESC, id DESC LIMIT ?",
                (path, *project_args, n)).fetchall()
        return [_item(r) for r in reversed(rows)]

    def by_path(self, path: str, kinds: Iterable[str] | None = None, limit: int = 20, *,
                allowed_audiences: Iterable[str] | None = None, project_id: str = "") -> list[Item]:
        sql = ("SELECT * FROM items WHERE (path=? OR path LIKE ?) AND invalid_at IS NULL "
               "AND (status='hot' OR (COALESCE(pinned,0)=1 AND status='cold'))")
        args: list = [path, path + "/%"]
        if allowed_audiences is not None:
            audiences = list(dict.fromkeys(allowed_audiences))
            sql += (" AND audience IN (%s)" % ",".join("?" * len(audiences))) if audiences else " AND 0"
            args += audiences
        if kinds:
            kinds = list(kinds)
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += kinds
        with self.lock:
            project_sql, project_args = self._project_filter(project_id, path, "")
            sql += project_sql
            args += project_args
            rows = self.db.execute(sql + " ORDER BY importance DESC, last_access DESC LIMIT ?",
                                   args + [limit]).fetchall()
        return [_item(r) for r in rows]

    def list_projects(self) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT substr(path, 9) AS p FROM items WHERE path LIKE 'project/%'").fetchall()
        return sorted({r["p"].split("/")[0] for r in rows if r["p"]})

    # ================= 记忆管理：时间线 / 遗忘 / 恢复（v0.9） =================
    @staticmethod
    def brief(r) -> dict:
        """返回给 MCP / 设置页 / Claude Code 命令的统一字段（不含 importance 这类内部值）"""
        return {"id": r["id"], "path": r["path"], "kind": r["kind"], "content": r["content"],
                "trust": r["trust"] or "owner", "pinned": bool(r["pinned"]),
                "created": r["created"], "valid_from": r["valid_from"],
                "invalid_at": r["invalid_at"], "superseded_by": r["superseded_by"],
                "stale_note": r["stale_note"], "audience": r["audience"] or "public",
                "source_path": r["source_path"] or r["path"], "user_id": r["user_id"] or "",
                "project_id": r["project_id"] or "", "session_id": r["session_id"] or "",
                "source_event_id": r["source_event_id"] or "", "source_ids": _json_list(r["source_ids"]),
                "epistemic_state": r["epistemic_state"] or "", "evidence_type": r["evidence_type"] or "",
                "evidence": _json_dict(r["evidence"])}

    def timeline(self, limit: int = 20, path: str | None = None, top: bool = False, *,
                 project_id: str = "") -> dict:
        """最近的记忆：新增的（recent）和刚被取代的（superseded，带旧值/新值）。

        只列提炼过的条目（turn 是原始对话流水，不是"记忆"）；被合并进摘要的不列。
        top=True 时按 importance 排序（"最重要的 5 条决策"用），否则按时间。
        """
        try:
            limit = max(1, min(int(limit or 20), 200))
        except (TypeError, ValueError):
            limit = 20
        cond = "kind NOT IN ('turn','evidence') AND status NOT IN ('merged','deleted')"
        args: list = []
        if path:
            cond += " AND (path=? OR path LIKE ?)"
            args = [path, path + "/%"]
        order = "importance DESC, created DESC, id DESC" if top else "created DESC, id DESC"
        with self.lock:
            project_sql, project_args = self._project_filter(project_id, path or "", "")
            cond += project_sql
            args += project_args
            recent = [self.brief(r) for r in self.db.execute(
                f"SELECT * FROM items WHERE {cond} AND invalid_at IS NULL "
                f"ORDER BY {order} LIMIT ?", (*args, limit)).fetchall()]
            rows = self.db.execute(
                f"SELECT * FROM items WHERE {cond} AND invalid_at IS NOT NULL AND superseded_by IS NOT NULL "
                "ORDER BY invalid_at DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
            superseded = []
            for r in rows:
                n = self.db.execute("SELECT id, content FROM items WHERE id=?",
                                    (r["superseded_by"],)).fetchone()
                superseded.append({"old": self.brief(r), "at": r["invalid_at"],
                                   "new": ({"id": n["id"], "content": n["content"]} if n else None)})
        return {"recent": recent, "superseded": superseded}

    def forget(self, item_id) -> dict | None:
        """管理员/聊天入口验证权限后，撤下记忆、原话及引用该事实的摘要。"""
        try:
            iid = int(item_id)
        except (TypeError, ValueError):
            return None
        with self.lock:
            r = self.db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if r is None:
                return None
            derived = self._forget_derived(r)
            self.db.execute("UPDATE items SET status='deleted' WHERE id=?", (iid,))
            self.add_tombstone(r["content"], audience=r["audience"], user_id=r["user_id"])
            self._changed(clear_profiles=True)
            self.db.commit()
            brief = self.brief(r)
        # 与 Markdown 同步保持同一锁顺序，不在持有数据库锁时进入 md_lock。
        for row in [r, *derived]:
            self._remove_mirror_line(row["path"], row["kind"], row["id"])
        return brief

    def _forget_derived(self, row) -> list:
        """删除含有旧事实的摘要；调用者负责在释放数据库锁后更新镜像。"""
        old = set(tokens(core_text(row["content"])))
        paths = list(dict.fromkeys(p for p in (row["path"], row["source_path"]) if p))
        clauses = ["parent_id=?"]
        args: list = [row["id"]]
        # Exact provenance follows transitive dependencies, independent of wording.
        linked = {r[0] for r in self.db.execute(
            "WITH RECURSIVE deps(id) AS (SELECT derived_id FROM item_sources WHERE source_id=? "
            "UNION SELECT s.derived_id FROM item_sources s JOIN deps d ON s.source_id=d.id) "
            "SELECT id FROM deps", (row["id"],))}
        if linked:
            clauses.append("id IN (%s)" % ",".join("?" * len(linked)))
            args += sorted(linked)
        if paths:
            clauses.append("(kind='summary' AND path IN (%s))" % ",".join("?" * len(paths)))
            args += paths
        if row["parent_id"]:
            clauses.append("id=?")
            args.append(row["parent_id"])
        rows = self.db.execute(
            "SELECT * FROM items WHERE (" + " OR ".join(clauses) + ") "
            "AND id!=? AND status!='deleted'", (*args, row["id"])).fetchall()
        derived = []
        for candidate in rows:
            direct = (candidate["parent_id"] == row["id"] or candidate["id"] == row["parent_id"]
                      or candidate["id"] in linked)
            if not direct:
                if row["project_id"] != candidate["project_id"]:
                    continue
                if (row["user_id"] and candidate["user_id"]
                        and row["user_id"] != candidate["user_id"]):
                    continue
                st = set(tokens(core_text(candidate["content"])))
                if not old or len(old & st) / len(old) < 0.6:
                    continue
            self.db.execute("UPDATE items SET status='deleted' WHERE id=?", (candidate["id"],))
            self.add_tombstone(candidate["content"], audience=candidate["audience"], user_id=candidate["user_id"])
            derived.append(candidate)
        return derived

    def restore(self, item_id) -> dict | None:
        """把忘掉 / 被取代的条目恢复成有效记忆。

        - 被取代的：清空 invalid_at / superseded_by，去掉 md 注释里的 superseded_by=；
          新条目仍然有效（两条并存，由用户自己决定留哪条）；
        - 被删除的：status 回 hot、移除对应墓碑、把 md 行补回来。
        """
        try:
            iid = int(item_id)
        except (TypeError, ValueError):
            return None
        with self.lock:
            r = self.db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if r is None or r["kind"] == "turn":
                return None
            if not self._restorable_sources(r):
                return None
            path, kind, content = r["path"], r["kind"], r["content"]
            was_superseded = r["invalid_at"] is not None
            if was_superseded:
                self.db.execute("UPDATE items SET invalid_at=NULL, superseded_by=NULL WHERE id=?", (iid,))
            self.db.execute("UPDATE items SET status='hot' WHERE id=?", (iid,))
            self._changed(clear_profiles=True)
            self.db.commit()
            self._drop_tombstone(content, audience=r["audience"], user_id=r["user_id"])
            row = self.db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            brief = self.brief(row)
        if was_superseded:
            self._strip_mirror_note(path, kind, iid, "superseded_by")
        if not self._mirror_has_line(path, kind, iid):
            self._mirror(path, kind, content, iid, row["importance"], row["speaker"], row["role"],
                         row["created"] or self.clock())
        return brief

    def _drop_tombstone(self, content: str, *, audience: str | None = None, user_id: str = "") -> int:
        fp = self._fp(content)
        if not fp:
            return 0
        sql, args = "DELETE FROM tombstones WHERE fp=?", [fp]
        if audience is not None:
            sql += " AND (audience='*' OR (audience=? AND COALESCE(user_id,'')=?))"
            args += [audience, str(user_id or "")]
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    def _mirror_has_line(self, path: str, kind: str, iid: int) -> bool:
        if kind == "turn":
            return True
        fn = os.path.join(self.md_root, *path.split("/"), self._md_name(kind))
        with self.md_lock:
            try:
                with open(fn, encoding="utf-8") as f:
                    return bool(re.search(r"<!--\s*id=%d(?!\d)" % int(iid), f.read()))
            except OSError:
                return False

    def _remove_mirror_line(self, path: str, kind: str, iid: int) -> bool:
        """从 Markdown 镜像里删掉一条（forget 用）。正文按 id 注释定位，不动其他行。"""
        if kind == "turn":
            return False
        fn = os.path.join(self.md_root, *path.split("/"), self._md_name(kind))
        with self.md_lock:
            try:
                with open(fn, encoding="utf-8") as f:
                    lines = f.read().split("\n")
            except OSError:
                return False
            pat = re.compile(r"<!--\s*id=%d(?!\d)" % int(iid))
            out = [ln for ln in lines if not pat.search(ln)]
            if len(out) == len(lines):
                return False
            atomic_write(fn, "\n".join(out))
            return True

    def _strip_mirror_note(self, path: str, kind: str, iid: int, key: str) -> bool:
        """去掉 md 注释里的某个标记（restore 时清掉 superseded_by=…）"""
        if kind == "turn":
            return False
        fn = os.path.join(self.md_root, *path.split("/"), self._md_name(kind))
        with self.md_lock:
            try:
                with open(fn, encoding="utf-8") as f:
                    lines = f.read().split("\n")
            except OSError:
                return False
            pat = re.compile(r"\s*%s=\d+" % re.escape(key))
            out, changed = [], False
            for line in lines:
                if re.search(r"<!--\s*id=%d(?!\d)" % int(iid), line):
                    new = pat.sub("", line)
                    if new != line:
                        changed = True
                        line = new
                out.append(line)
            if not changed:
                return False
            atomic_write(fn, "\n".join(out))
            return True


    def move_path(self, old: str, new: str) -> int:
        """把某个分支下的条目迁到新路径（人物档案合并用）。返回迁移条数。"""
        if not old or old == new:
            return 0
        # 锁顺序：先 md_lock 再 self.lock（见 __init__ 的说明）
        with self.md_lock:
            with self.lock:
                rows = self.db.execute("SELECT id FROM items WHERE path=? OR path LIKE ?",
                                       (old, old + "/%")).fetchall()
                if not rows:
                    return 0
                self.db.execute("UPDATE items SET path=? WHERE path=?", (new, old))
                self.db.execute("UPDATE items SET path=? || substr(path, ?) WHERE path LIKE ?",
                                (new, len(old) + 1, old + "/%"))
                if new == "core/people/owner":
                    self.db.execute(
                        "UPDATE items SET audience='owner' WHERE (path=? OR path LIKE ?) "
                        "AND trust IN ('owner','known')", (new, new + "/%"))
                self._changed(clear_profiles=True)
                self.db.commit()
            # Markdown 镜像也要跟着搬：否则新目录的 notes.md 里没有这些行，
            # 下次 sync_markdown() 会把迁过来的条目当成"用户删掉了"（标 deleted + 墓碑）
            self._move_mirror(old, new, [r["id"] for r in rows])
        return len(rows)

    def _move_mirror(self, old: str, new: str, ids: list[int]) -> None:
        NL = "\n"
        with self.md_lock:
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
                        try:
                            with open(sp, encoding="utf-8") as f:
                                text = f.read()
                        except OSError:
                            continue
                        if os.path.exists(tp):
                            try:
                                with open(tp, encoding="utf-8") as f:
                                    cur = f.read()
                            except OSError:
                                cur = ""
                            text = cur + ("" if not cur or cur.endswith(NL) else NL) + text
                        atomic_write(tp, text)
                        try:
                            os.remove(sp)
                        except OSError:
                            pass
                    try:
                        os.rmdir(d)
                    except OSError:
                        pass
            # 数据库里有、但 Markdown 里没有对应行的条目（比如当初写入时没镜像），补一行，
            # 保证同步时不会被误判为"已删除"
            if not ids:
                return
            rows = self.db.execute(
                "SELECT * FROM items WHERE id IN (%s) AND kind NOT IN ('turn','evidence') AND status NOT IN ('merged','deleted')"
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
                "SELECT path,project_id, COUNT(*) c FROM items WHERE kind='turn' AND status='cold' "
                "GROUP BY path,project_id HAVING c>=?",
                (min_count,)).fetchall()
            out = []
            for r in rows:
                its = self.db.execute(
                    "SELECT * FROM items WHERE path=? AND project_id=? AND kind='turn' AND status='cold' "
                    "ORDER BY created LIMIT 200", (r["path"],r["project_id"])).fetchall()
                out.append((r["path"], [_item(x) for x in its]))
        return out

    def commit_summary(self, path: str, summary: str, source_ids: list[int],
                       importance: float = 0.6) -> Optional[int]:
        # 继承来源的可见范围与最低可信度，摘要不能变成跨聊天公开的 owner 记忆。
        if not source_ids:
            return None
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM items WHERE id IN (%s)" % ",".join("?" * len(source_ids)),
                tuple(source_ids)).fetchall()
            if len(rows) != len(set(source_ids)) or any(r["status"] == "deleted" for r in rows):
                return None
            trust = "owner"
            for r in rows:
                trust = stricter(trust, r["trust"] or "owner")
            audiences = {r["audience"] or self._default_audience(r["path"], r["trust"]) for r in rows}
            audience = next(iter(audiences)) if len(audiences) == 1 else path
            users = {r["user_id"] or "" for r in rows}
            user_id = next(iter(users)) if len(users) == 1 else ""
            sources = {r["source_path"] or r["path"] for r in rows}
            source_path = next(iter(sources)) if len(sources) == 1 else path
            projects = {r["project_id"] or "" for r in rows}
            if len(projects) > 1:
                return None
            project_id = next(iter(projects))
            sessions = {r["session_id"] or "" for r in rows}
            session_id = next(iter(sessions)) if len(sessions) == 1 else ""
        sid = self.write(path, "summary", summary, importance, dedup=False, trust=trust,
                         user_id=user_id, audience=audience, source_path=source_path,
                         project_id=project_id, session_id=session_id, source_ids=source_ids)
        if sid is None:
            return None
        with self.lock:
            deleted = self.db.execute(
                "SELECT 1 FROM items WHERE id IN (%s) AND status='deleted' LIMIT 1"
                % ",".join("?" * len(source_ids)), tuple(source_ids)).fetchone()
            if deleted:
                self.db.execute("UPDATE items SET status='deleted' WHERE id=?", (sid,))
                self.add_tombstone(summary, audience=audience, user_id=user_id)
                self._changed(clear_profiles=True)
            else:
                self.db.executemany("UPDATE items SET status='merged', parent_id=? WHERE id=?",
                                    [(sid, i) for i in source_ids])
            self.db.commit()
        if deleted:
            self._remove_mirror_line(path, "summary", sid)
            return None
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

    def due_items(self, now: float, *, lookback_h: float = 72, trusts: Iterable[str] = ("owner",)) -> list[Item]:
        """到了该跟进的时刻、但还没过太久（默认 3 天内）的条目。只取可信来源，避免陌生人的话驱动她主动发消息。"""
        trusts = [t for t in trusts if t in TRUST_RANK] or ["owner"]
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM items WHERE due_at IS NOT NULL AND due_at<=? AND due_at>=? "
                "AND status NOT IN ('deleted','merged') AND invalid_at IS NULL AND trust IN (%s) "
                "ORDER BY due_at DESC LIMIT 20" % ",".join("?" * len(trusts)),
                (now, now - lookback_h * 3600, *trusts)).fetchall()
        return [_item(r) for r in rows]

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
        # 先 md_lock 再数据库锁，读改写期间不会吞掉别的线程追加的镜像行。
        with self.md_lock:
            try:
                with open(full, encoding="utf-8") as f:
                    lines = f.read().split("\n")
            except OSError:
                return
            seen: set[int] = set()
            removed: dict[int, object] = {}
            changed_file = False
            changed_memory = False
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
                            edited = r["content"] != content or r["kind"] != kind
                            if not edited and r["status"] == "deleted" and not self._restorable_sources(r):
                                continue
                            if edited:
                                self.add_tombstone(r["content"], audience=r["audience"], user_id=r["user_id"])
                                for derived in self._forget_derived(r):
                                    removed[derived["id"]] = derived
                                # Manual wording/type changes are a new accepted claim,
                                # not implementation or verification of the old claim.
                                self.db.execute("UPDATE items SET epistemic_state=?,evidence='{}',evidence_type='',"
                                                "source_ids='[]',valid_from=? WHERE id=?",
                                                ("accepted" if r["epistemic_state"] else "", now, iid))
                                self.db.execute("DELETE FROM item_sources WHERE derived_id=?", (iid,))
                            if r["status"] == "deleted":
                                self._drop_tombstone(content, audience=r["audience"], user_id=r["user_id"])
                            self.db.execute("UPDATE items SET content=?, kind=?, status='hot', last_access=? WHERE id=?",
                                            (content, kind, now, iid))
                            self.db.execute("DELETE FROM items_fts WHERE rowid=?", (iid,))
                            self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)",
                                            (iid, index_text(content + " " + (r["tags"] or ""))))
                            self.db.execute("DELETE FROM vecs WHERE id=?", (iid,))
                            stats["updated"] += 1
                            changed_memory = True
                    else:
                        cur = self.db.execute(
                            "INSERT INTO items(path,kind,role,speaker,content,importance,tau_h,created,last_access,tags,"
                            "audience,source_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (path, kind, "", "", content, 0.85, float(self.tau.get(kind, 72)), now, now, "manual",
                             self._default_audience(path), path))
                        new_id = cur.lastrowid; seen.add(new_id)
                        self.db.execute("INSERT INTO items_fts(rowid, tok) VALUES(?,?)", (new_id, index_text(content + " manual")))
                        lines[li] = f"{prefix}[{kind}] {content}  <!-- id={new_id} imp=0.85 {time.strftime('%Y-%m-%d', time.localtime(now))} manual -->"
                        changed_file = changed_memory = True
                        stats["added"] += 1
                q = ("SELECT * FROM items WHERE path=? AND kind IN (%s) "
                     "AND status NOT IN ('merged','deleted') AND created < ?" % ",".join("?" * len(kinds)))
                gone = [r for r in self.db.execute(q, (path, *kinds, now - 5)) if r["id"] not in seen]
                for row in gone:
                    self.add_tombstone(row["content"], audience=row["audience"], user_id=row["user_id"])
                    for derived in self._forget_derived(row):
                        removed[derived["id"]] = derived
                    self.db.execute("UPDATE items SET status='deleted' WHERE id=?", (row["id"],))
                if gone:
                    stats["deleted"] += len(gone)
                    changed_memory = True
                if changed_memory:
                    self._changed(clear_profiles=True)
                self.db.commit()
            if removed:
                # 本文件用当前快照删除；其他文件可在持有 md_lock 时安全更新。
                out = []
                for line in lines:
                    match = self._MD_LINE.match(line)
                    if match and match.group(5) and int(match.group(5)) in removed:
                        changed_file = True
                        continue
                    out.append(line)
                lines = out
                for row in removed.values():
                    other = os.path.join(self.md_root, *row["path"].split("/"), self._md_name(row["kind"]))
                    if os.path.normcase(other) != os.path.normcase(full):
                        self._remove_mirror_line(row["path"], row["kind"], row["id"])
            if changed_file:
                atomic_write(full, "\n".join(lines))
        if self.embedder is not None:
            self.backfill_embeddings()

    # ================= Markdown 镜像 =================
    MD_NAMES = {"decision": "decisions.md", "todo": "todo.md", "summary": "summaries.md"}

    @classmethod
    def _md_name(cls, kind: str) -> str:
        return cls.MD_NAMES.get(kind, "notes.md")

    def _mirror(self, path, kind, content, iid, imp, speaker, role, ts) -> None:
        d = os.path.join(self.md_root, *path.split("/"))
        tm = time.localtime(ts)
        if kind in ("turn", "evidence"):
            fn = os.path.join(d, time.strftime("%Y-%m", tm) + ".md")
            who = speaker or role
            line = f"- `{time.strftime('%m-%d %H:%M', tm)}` **{who}**: {content.replace(chr(10), ' ')}\n"
        else:
            fn = os.path.join(d, self._md_name(kind))
            line = f"- [{kind}] {content.replace(chr(10), ' ')}  <!-- id={iid} imp={imp:.2f} {time.strftime('%Y-%m-%d', tm)} -->\n"
        # 追加本身不需要原子写（一行一次 write），但要和"读 → 改 → 替换"的调用方串行，
        # 否则改写方基于旧快照写回时会把刚追加的行吞掉
        with self.md_lock:
            os.makedirs(d, exist_ok=True)
            with open(fn, "a", encoding="utf-8") as f:
                f.write(line)

    def _annotate_mirror_line(self, path, kind, iid, note) -> bool:
        """在 Markdown 镜像那一行的 id 注释末尾追加标记（不改正文）。

        正文不变 → sync_markdown() 不会把它当成"用户改过"；注释里多一段也不影响 _MD_LINE
        匹配（id 之后到 --> 之间是 [^>]*）。
        """
        if kind == "turn":
            return False
        fn = os.path.join(self.md_root, *path.split("/"), self._md_name(kind))
        with self.md_lock:
            if not os.path.exists(fn):
                return False
            try:
                with open(fn, encoding="utf-8") as f:
                    lines = f.read().split("\n")
            except OSError:
                return False
            pat = re.compile(r"(<!--\s*id=%d(?=[^>]*-->)[^>]*?)(\s*-->)" % int(iid))
            out, changed = [], False
            for line in lines:
                if iid and note not in line and re.search(r"<!--\s*id=%d(?!\d)" % int(iid), line):
                    new = pat.sub(lambda m: f"{m.group(1)} {note}{m.group(2)}", line, count=1)
                    if new != line:
                        changed = True
                        line = new
                out.append(line)
            if not changed:
                return False
            atomic_write(fn, "\n".join(out))
            return True

    def _rewrite_mirror_line(self, path, kind, iid, content, imp, ts) -> bool:
        """去重合并后，把 Markdown 里对应的那一行改成新内容。

        不回写的话，下次 sync_markdown() 会认为"Markdown 才是最新的"，把数据库改回旧内容。
        """
        if kind == "turn":
            return False
        fn = os.path.join(self.md_root, *path.split("/"), self._md_name(kind))
        with self.md_lock:
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
            atomic_write(fn, "\n".join(out))
            return True

    def close(self):
        with self.lock:
            self.db.close()


def _pinned(it) -> bool:
    try:
        return bool(it["pinned"])
    except (IndexError, KeyError, TypeError):
        return bool(getattr(it, "pinned", 0))


def _in_window(r, window: tuple[float, float]) -> bool:
    """条目的"时间"= COALESCE(valid_from, created)：说的是哪天的事，而不是哪天入库的"""
    try:
        t = r["valid_from"]
    except (IndexError, KeyError):
        t = getattr(r, "valid_from", None)
    if t is None:
        try:
            t = r["created"]
        except (IndexError, KeyError):
            t = getattr(r, "created", 0.0)
    return window[0] <= t <= window[1]


def _item(r) -> Item:
    keys = Item.__dataclass_fields__.keys()
    data = {k: r[k] for k in r.keys() if k in keys}
    data["source_ids"] = _json_list(data.get("source_ids"))
    data["evidence"] = _json_dict(data.get("evidence"))
    return Item(**data)


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        result = json.loads(value or "[]")
        return result if isinstance(result, list) else []
    except (TypeError, ValueError):
        return []


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}
