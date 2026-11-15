"""
可选的语义向量（embedding）。不配置时插件完全离线工作（字词 + 概念词典检索）；配置后叠加真正的语义检索。

两种提供方：
  1) HTTP（推荐）：任何 OpenAI 兼容的 /embeddings 接口，例如
       硅基流动 SiliconFlow:  base_url=https://api.siliconflow.cn/v1  model=BAAI/bge-m3
       本地 Ollama:           base_url=http://127.0.0.1:11434/v1      model=bge-m3
  2) fastembed（本地 ONNX，需 pip install fastembed，首次会下载约 90MB 模型）
       provider=fastembed  model=BAAI/bge-small-zh-v1.5

配置写在 config.json 的 memory.embedding 里：
  {"provider": "http", "base_url": "...", "api_key": "...", "model": "BAAI/bge-m3"}
切换同名模型的服务或版本时，可显式设置新的 space_id；旧向量先由字词检索兜底，
再由维护器分批重建。未设置时保留旧命名，不因升级自动触发付费重建。
"""
from __future__ import annotations
import hashlib, json, logging, math, os, time, urllib.request
from array import array
from collections import OrderedDict
from .usage import record_request

log = logging.getLogger("soulcore.embed")

try:
    import numpy as _np   # DSH 自带的 Python 里有 numpy；没有也能跑（纯 Python 余弦）
except Exception:          # pragma: no cover
    _np = None


class HTTPEmbedder:
    def __init__(self, cfg: dict):
        self.provider = cfg.get("provider", "http")
        self.usage_ledger = None
        self.base = cfg.get("base_url", "").rstrip("/")
        self.key = cfg.get("api_key") or os.environ.get(cfg.get("api_key_env", "SOULCORE_EMBED_KEY"), "")
        self.model = cfg.get("model", "BAAI/bge-m3")
        self.timeout = cfg.get("timeout", 20)
        self.name = f"http:{self.model}"
        # Explicit opt-in namespace for endpoint/model revisions. Keep the old
        # default to avoid an automatic paid re-embedding migration on upgrade.
        if isinstance(cfg.get("space_id"), str) and cfg["space_id"].strip():
            digest = hashlib.sha256(cfg["space_id"].strip().encode()).hexdigest()[:16]
            self.name += f":space:{digest}"
        if not self.base:
            raise ValueError("embedding.base_url 未配置")

    def bind_usage(self, ledger):
        self.usage_ledger = ledger
        return self

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.base + "/embeddings", data=body, headers=headers, method="POST")
        started, data, success, failure = time.perf_counter(), None, False, None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
            rows = sorted(data["data"], key=lambda x: x.get("index", 0))
            vectors = [row["embedding"] for row in rows]
            success = True
            return vectors
        except Exception as ex:
            failure = ex
            raise
        finally:
            record_request(self.usage_ledger, provider=self.provider, model=self.model, operation="embedding",
                           usage=data.get("usage") if isinstance(data, dict) else None,
                           success=success, elapsed_ms=(time.perf_counter()-started)*1000,
                           status_code=getattr(failure, "code", None),
                           error_type=type(failure).__name__ if failure else "")


class FastEmbedder:
    def __init__(self, cfg: dict):
        from fastembed import TextEmbedding   # 可选依赖
        self.model_name = cfg.get("model", "BAAI/bge-small-zh-v1.5")
        self.m = TextEmbedding(self.model_name, cache_dir=cfg.get("cache_dir"))
        self.name = f"fastembed:{self.model_name}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self.m.embed(texts)]


class CachedEmbedder:
    """查询向量 LRU 缓存 + 失败熔断（连续失败 3 次后 10 分钟内不再尝试，避免拖慢每一轮）"""
    def __init__(self, inner, clock, size: int = 256):
        self.inner, self.clock, self.size = inner, clock, size
        self.name = inner.name
        self.cache: OrderedDict[str, list[float]] = OrderedDict()
        self.fails, self.open_until = 0, 0.0

    def bind_usage(self, ledger):
        if hasattr(self.inner, "bind_usage"):
            self.inner.bind_usage(ledger)
        return self

    @property
    def available(self) -> bool:
        return self.clock() >= self.open_until

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.available:
            return None
        todo = [t for t in texts if t not in self.cache]
        if todo:
            try:
                vecs = self.inner.embed(todo)
                self.fails = 0
            except Exception as ex:
                self.fails += 1
                if self.fails >= 3:
                    self.open_until = self.clock() + 600
                log.warning("embedding 调用失败（%s），本次只用字词检索", ex)
                return None
            for t, v in zip(todo, vecs):
                self.cache[t] = normalize(v)
                if len(self.cache) > self.size:
                    self.cache.popitem(last=False)
        return [self.cache[t] for t in texts]


def normalize(v) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def pack(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def unpack(b: bytes) -> array:
    a = array("f"); a.frombytes(b); return a


def cosines(q: list[float], blobs: list[bytes]) -> list[float]:
    if not blobs:
        return []
    if _np is not None:
        M = _np.frombuffer(b"".join(blobs), dtype=_np.float32).reshape(len(blobs), -1)
        return (M @ _np.asarray(q, dtype=_np.float32)).tolist()
    out = []
    for b in blobs:
        a = unpack(b)
        out.append(sum(x * y for x, y in zip(a, q)))
    return out


def make_embedder(cfg: dict | None, clock):
    if not cfg or not cfg.get("provider"):
        return None
    try:
        p = cfg["provider"]
        inner = HTTPEmbedder(cfg) if p in ("http", "openai", "siliconflow", "ollama") else FastEmbedder(cfg) if p == "fastembed" else None
        if inner is None:
            log.warning("未知的 embedding provider: %s", p)
            return None
        return CachedEmbedder(inner, clock)
    except Exception as ex:
        log.warning("embedding 初始化失败（%s），退回纯字词检索", ex)
        return None
