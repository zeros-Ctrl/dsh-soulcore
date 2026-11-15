"""Persistent project identity without writing markers into a user's repository.

Registry mode preserves legacy paths. Strict mode uses UUID paths. A basename is
an alias, never a unique identity; old unbound records are never silently claimed.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import replace

from .events import Event, _safe


def canonical_project_root(cwd: str) -> str:
    """Find the repository root, including file-based .git worktrees."""
    if not str(cwd or "").strip():
        return ""
    original = os.path.realpath(os.path.abspath(str(cwd)))
    current = original
    for _ in range(40):
        if os.path.exists(os.path.join(current, ".git")):
            return os.path.normcase(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.normcase(original)


class ProjectRegistry:
    def __init__(self, db, lock, clock):
        self.db, self.lock, self.clock = db, lock, clock
        with lock:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects(
                    project_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    created REAL NOT NULL, last_seen REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_projects_name ON projects(name);
                CREATE TABLE IF NOT EXISTS project_roots(
                    canonical_root TEXT PRIMARY KEY, project_id TEXT NOT NULL
                );
            """)
            db.commit()

    def resolve(self, cwd: str = "", name: str = "", project_id: str = "",
                mode: str = "registry") -> dict:
        if mode not in ("registry", "strict", "legacy"):
            raise ValueError("project_identity must be registry, strict or legacy")
        root = canonical_project_root(cwd)
        name = _safe(name or os.path.basename(root) or "default")
        if project_id:
            try:
                project_id = str(uuid.UUID(str(project_id)))
            except (ValueError, TypeError, AttributeError) as ex:
                raise ValueError("project_id must be a UUID") from ex
        with self.lock:
            if not root and not project_id:
                named = self.db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchall()
                if len(named) > 1:
                    raise ValueError("项目名有歧义，请使用明确的 project UUID")
                if named:
                    project_id = named[0]["project_id"]
            existing = (self.db.execute(
                "SELECT p.* FROM project_roots r JOIN projects p ON p.project_id=r.project_id "
                "WHERE r.canonical_root=?", (root,)).fetchone() if root else None)
            if existing and project_id and existing["project_id"] != project_id:
                raise ValueError("cwd is already registered to another project_id")
            if existing:
                project_id, name = existing["project_id"], existing["name"]
            elif root or project_id:
                project_id = project_id or str(uuid.uuid4())
                known = self.db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
                if known:
                    name = known["name"]
                else:
                    self.db.execute("INSERT INTO projects VALUES(?,?,?,?)",
                                    (project_id, name, self.clock(), self.clock()))
                if root:
                    self.db.execute("INSERT INTO project_roots VALUES(?,?)", (root, project_id))
            if project_id:
                self.db.execute("UPDATE projects SET last_seen=? WHERE project_id=?", (self.clock(), project_id))
            collision = self.db.execute("SELECT COUNT(*) FROM projects WHERE name=?", (name,)).fetchone()[0] > 1
            legacy_path = "project/" + name
            legacy_count = self.db.execute(
                "SELECT COUNT(*) FROM items WHERE path=? AND COALESCE(project_id,'')='' AND status!='deleted'",
                (legacy_path,)).fetchone()[0]
            self.db.commit()
        channel = project_id if mode == "strict" and project_id else name
        return {"project_id": project_id, "name": name, "canonical_root": root,
                "channel_id": channel, "path": "project/" + channel,
                "mode": mode, "collision": collision, "legacy_path": legacy_path,
                "legacy_unbound": legacy_count, "include_legacy": not collision and mode != "strict"}

    def collides(self, project_id: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT name FROM projects WHERE project_id=?", (project_id,)).fetchone()
            return bool(row and self.db.execute(
                "SELECT COUNT(*) FROM projects WHERE name=?", (row["name"],)).fetchone()[0] > 1)

    def event(self, ev: Event, mode: str = "registry") -> Event:
        resolve_name = bool((ev.raw or {}).get("resolve_project_name"))
        if ev.platform != "local" or ev.scene != "project" or not (ev.cwd or ev.project_id or resolve_name):
            return ev
        info = self.resolve(ev.cwd, ev.channel_id, ev.project_id, mode)
        return replace(ev, channel_id=info["channel_id"], project_id=info["project_id"],
                       raw={**(ev.raw or {}), "project_identity": info})

    def status(self) -> dict:
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM projects ORDER BY name,created")]
            collisions = [r[0] for r in self.db.execute(
                "SELECT name FROM projects GROUP BY name HAVING COUNT(*)>1")]
        return {"projects": rows, "collisions": collisions}
