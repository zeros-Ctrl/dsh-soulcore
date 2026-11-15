"""LLM 客户端：DeepSeek（OpenAI 兼容接口，仅用标准库）+ 离线 Mock（无 key 也能跑通整条管线）"""
from __future__ import annotations
import json, os, re, random, time, urllib.request, urllib.error
from .usage import current_purpose, record_request, usage_scope


class LLMError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg); self.status = status


class DeepSeekLLM:
    def __init__(self, cfg: dict):
        self.provider = cfg.get("provider", "deepseek")
        self.usage_ledger = None
        self.base = cfg.get("base_url", "https://api.deepseek.com").rstrip("/")
        self.key = cfg.get("api_key") or os.environ.get(cfg.get("api_key_env", "DEEPSEEK_API_KEY"), "")
        self.model = cfg.get("model", "deepseek-chat")
        self.extract_model = cfg.get("extract_model", self.model)
        self.temperature = cfg.get("temperature", 1.0)
        self.max_tokens = cfg.get("max_tokens", 400)
        self.timeout = cfg.get("timeout", 60)
        if not self.key:
            raise LLMError("没有找到 DeepSeek API key（配置 llm.api_key 或环境变量 DEEPSEEK_API_KEY），可用 --mock 离线运行")

    def bind_usage(self, ledger):
        self.usage_ledger = ledger
        return self

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise LLMError(f"HTTP {e.code}: {e.read()[:300]!r}", e.code)
        except urllib.error.URLError as e:
            raise LLMError(f"网络错误: {e}")

    def chat(self, messages, *, json_mode=False, temperature=None, max_tokens=None, model=None) -> str:
        body = {"model": model or self.model, "messages": messages,
                "temperature": self.temperature if temperature is None else temperature,
                "max_tokens": max_tokens or self.max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        started, response, success, failure = time.perf_counter(), None, False, None
        try:
            response = self._req("POST", "/chat/completions", body)
            reply = response["choices"][0]["message"]["content"] or ""
            success = True
            return reply
        except Exception as ex:
            failure = ex
            raise
        finally:
            record_request(self.usage_ledger, provider=self.provider, model=body["model"],
                           usage=response.get("usage") if isinstance(response, dict) else None,
                           success=success, elapsed_ms=(time.perf_counter()-started)*1000,
                           status_code=getattr(failure, "status", None),
                           error_type=type(failure).__name__ if failure else "")

    def chat_json(self, messages, **kw) -> dict:
        options = {"json_mode": True, "temperature": 0.2, "model": self.extract_model, "max_tokens": 1200, **kw}
        with usage_scope(current_purpose("structured")):
            txt = self.chat(messages, **options)
        return parse_json(txt)

    def balance(self) -> dict | None:
        """GET /user/balance → {'available': bool, 'total': float}"""
        r = self._req("GET", "/user/balance")
        tot = 0.0
        for b in r.get("balance_infos", []):
            try:
                tot += float(b.get("total_balance", 0))
            except ValueError:
                pass
        return {"available": bool(r.get("is_available", True)), "total": tot}


def parse_json(txt: str) -> dict:
    txt = txt.strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


class MockLLM:
    """离线模拟：回复里会体现当前情绪和检索到的记忆，便于肉眼检查管线是否通"""
    def __init__(self, cfg: dict | None = None, seed: int = 0):
        self.rng = random.Random(seed)
        self.balance_value = 50.0

    def bind_usage(self, ledger):
        # Offline simulation has no provider-reported billable tokens.
        self.usage_ledger = ledger
        return self

    def chat(self, messages, **kw) -> str:
        sys = messages[0]["content"] if messages else ""
        user = messages[-1]["content"] if messages else ""
        mood = re.search(r"你现在([^。，（]+)", sys)
        mem = re.findall(r"^- (?:\[[^\]]+\] )?(.+)$", sys.split("## 你记得的相关事情")[-1], re.M) if "## 你记得的相关事情" in sys else []
        s = f"（{mood.group(1) if mood else '平静'}）"
        if mem:
            s += f"我记得：{mem[0][:40]}。"
        s += f"你说的「{user.strip()[-30:]}」我听到啦。"
        return s

    def chat_json(self, messages, **kw) -> dict:
        sys = messages[0]["content"]
        body = messages[-1]["content"]
        if "TASK:EXTRACT" in sys:
            from .extractor import heuristic_extract
            from .timeparse import parse_due_string
            m = re.search(r"\[当前时间\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", body)
            # [已有相关记忆] 之类的段落不是对话：只把 [对话] 之后的内容喂给规则提炼
            talk = body.split("[对话]\n", 1)[1] if "[对话]" in body else body
            return {"memories": heuristic_extract(talk, parse_due_string(m.group(1)) if m else None)}
        if "TASK:PROFILE" in sys:
            rows = [re.sub(r"^-\s*(\[[^\]]+\]\s*)?", "", ln).strip()
                    for ln in body.splitlines() if ln.strip()]
            return {"owner": "；".join(rows[:6])[:250],
                    # 自我认识不能照抄主人档案：混进正文会让她把"主人"的事当成"自己"的事
                    "self": "我记得主人告诉我的这些事，也会记住自己答应过的话。"}
        if "TASK:SUMMARIZE" in sys:
            lines = [l for l in body.splitlines() if l.strip()]
            return {"summary": "（摘要）" + "；".join(l[:20] for l in lines[:6])}
        if "TASK:APPRAISE" in sys:
            from .appraisal import heuristic_appraise
            return {"event": heuristic_appraise(body.split("\n")[-1], False)}
        return {}

    def balance(self):
        return {"available": self.balance_value > 0, "total": self.balance_value}


class HeuristicLLM(MockLLM):
    """没有 API key 时的降级：记忆提炼/事件评价用规则；不能生成回复（DSH 钩子模式下回复由 DSH 自己生成，不需要）"""
    def chat(self, messages, **kw) -> str:
        raise LLMError("未配置 DeepSeek API key：当前为规则模式，无法独立生成回复")

    def balance(self):
        return None


def make_llm(cfg: dict, mock: bool = False):
    if mock or cfg.get("provider") == "mock":
        return MockLLM(cfg)
    if cfg.get("provider") in ("heuristic", "none"):
        return HeuristicLLM(cfg)
    try:
        return DeepSeekLLM(cfg)
    except LLMError as ex:
        import logging
        logging.getLogger("soulcore").warning("%s —— 降级为规则模式", ex)
        return HeuristicLLM(cfg)
