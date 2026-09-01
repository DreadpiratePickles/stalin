"""Ollama client + the tool loop harness + HTML pruning.

The LLM is NEVER in the hot path of a scrape. It runs exactly twice in a
source's life: at compile time (`add`) and at heal time (rung 2). Both are
tool-driven loops where every proposal is executed against the real DOM and
the model iterates on real feedback.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

import httpx
import lxml.html


class LLMUnavailable(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 300.0,
                 think: bool = True):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.think = think

    @classmethod
    def create(cls, cfg) -> "OllamaClient":
        """From project LLMConfig; resolves model 'auto' to a tool-capable local model."""
        base = cfg.base_url.rstrip("/")
        model = cfg.model
        try:
            tags = httpx.get(f"{base}/api/tags", timeout=5).json()
        except Exception as e:
            raise LLMUnavailable(f"ollama not reachable at {base}: {e}")
        names = [m["name"] for m in tags.get("models", [])]
        if not names:
            raise LLMUnavailable("ollama is running but has no models — `ollama pull qwen3`")
        if model == "auto":
            # prefer a tool-capable model, but any model works: the selector
            # protocol is plain text with a sentinel (council-style), not tool calls
            model = next((n for n in names
                          if "tools" in cls._capabilities(base, n)), names[0])
        elif model not in names:
            raise LLMUnavailable(f"model {model!r} not found (have: {names})")
        return cls(base, model)

    @staticmethod
    def _capabilities(base: str, name: str) -> list[str]:
        try:
            r = httpx.post(f"{base}/api/show", json={"model": name}, timeout=10)
            return r.json().get("capabilities", [])
        except Exception:
            return []

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None) -> dict:
        payload: dict = {"model": self.model, "messages": messages, "stream": False,
                         "think": self.think,
                         "options": {"temperature": 0.1, "num_ctx": 8192}}
        if tools:
            payload["tools"] = tools
        try:
            r = httpx.post(f"{self.base_url}/api/chat", json=payload,
                           timeout=self.timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"ollama chat failed: {e}")
        return r.json().get("message", {})


def tool_loop(client: OllamaClient, system: str, user: str, tools: list[dict],
              executor: Callable[[str, dict], tuple[str, bool]],
              max_rounds: int = 4,
              on_round: Optional[Callable[[int, str, dict], None]] = None) -> Optional[dict]:
    """Run a tool-driven loop.

    `executor(tool_name, args) -> (feedback_json_str, done)`. When done=True the
    loop ends and the final (tool_name, args) is returned as
    {"tool": name, "args": args}. Returns None if rounds are exhausted.
    """
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    last_call: Optional[dict] = None
    for rnd in range(1, max_rounds + 1):
        try:
            msg = client.chat(messages, tools)
        except LLMUnavailable:
            if rnd == 1:
                raise
            return last_call          # died mid-loop: use best proposal so far
        calls = msg.get("tool_calls") or []
        if not calls:
            # nudge once: the model must act through tools
            messages.append({"role": "assistant", "content": msg.get("content", "")})
            messages.append({"role": "user", "content":
                             "You must respond by calling one of the provided tools."})
            try:
                msg = client.chat(messages, tools)
            except LLMUnavailable:
                return last_call
            calls = msg.get("tool_calls") or []
            if not calls:
                return last_call
        messages.append({"role": "assistant", "content": msg.get("content", ""),
                         "tool_calls": calls})
        for call in calls[:1]:                      # one call per round keeps it honest
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if on_round:
                on_round(rnd, name, args)
            feedback, done = executor(name, args)
            last_call = {"tool": name, "args": args, "round": rnd}
            if done:
                return last_call
            messages.append({"role": "tool", "content": feedback, "tool_name": name})
    return None


def complete(client: "OllamaClient", system: str, user: str) -> str:
    """One-shot text completion. The reliable path for small local models:
    free-form reasoning + a sentinel-marked answer line, parsed leniently."""
    msg = client.chat([{"role": "system", "content": system},
                       {"role": "user", "content": user}])
    return (msg.get("content") or "") + "\n" + str(msg.get("thinking") or "")


SELECTOR_RE = re.compile(r"SELECTOR:\s*`?([^`\n]+?)`?\s*(?:$|\n)", re.MULTILINE)
ATTR_RE = re.compile(r"ATTR:\s*`?(\w[\w-]*)`?", re.MULTILINE)
ABSENT_RE = re.compile(r"ABSENT:\s*(.+)", re.MULTILINE)


def parse_selector_reply(text: str) -> dict:
    """Layered, forgiving parse of the sentinel protocol.

    Expected tail:  SELECTOR: .some > css   /  ATTR: text|href
    Fallbacks rescue sloppy output the way llm-council parses rankings."""
    out: dict = {}
    m = ABSENT_RE.search(text)
    if m:
        out["absent"] = m.group(1).strip()
        return out
    m = SELECTOR_RE.search(text)
    if m:
        out["css"] = m.group(1).strip().strip('"')
    else:
        # last-ditch: a lone plausible selector on its own line or in backticks
        for cand in re.findall(r"`([.#\w][^`\n]{0,80})`", text):
            if re.match(r"^[.#\w\[][-\w.#>\s:()\[\]='\"^$*~|]+$", cand):
                out["css"] = cand.strip()
                break
    m = ATTR_RE.search(text)
    out["attr"] = m.group(1).strip() if m else "text"
    if out.get("attr") in ("link", "url"):
        out["attr"] = "href"
    return out


# --- HTML pruning for prompts -------------------------------------------

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "link", "meta")
_KEEP_ATTRS = ("class", "id", "href", "src", "name", "type", "title", "datetime",
               "data-id", "role", "aria-label")


def prune_html(html: str, cap: int = 14000) -> str:
    """Strip noise, truncate text, keep structural attrs. Deterministic."""
    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return html[:cap]
    for tag in _DROP_TAGS:
        for el in root.iter(tag):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for k in list(el.attrib):
            if k not in _KEEP_ATTRS:
                del el.attrib[k]
            elif len(el.attrib[k]) > 120:
                el.attrib[k] = el.attrib[k][:120]
        if el.text and len(el.text) > 80:
            el.text = el.text[:80] + "…"
        if el.tail and len(el.tail) > 40:
            el.tail = el.tail[:40] + "…"
    out = lxml.html.tostring(root, encoding="unicode")
    out = re.sub(r"\n\s*\n", "\n", out)
    if len(out) > cap:
        out = out[:cap] + "\n<!-- truncated -->"
    return out


def candidate_regions(html: str, samples: list[str], shape: str,
                      cap: int = 12000) -> str:
    """Candidate region mining for heals: subtrees whose text matches old
    sample values or the learned value shape, plus ancestors."""
    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return prune_html(html, cap)
    rx = None
    if shape:
        try:
            rx = re.compile(shape)
        except re.error:
            rx = None
    hits = []
    for el in root.iter():
        if not isinstance(el.tag, str) or el.tag in _DROP_TAGS:
            continue
        txt = (el.text or "").strip()
        if not txt or len(txt) > 200:
            continue
        matched = any(s and s in txt for s in samples) or (rx and rx.match(txt))
        if matched:
            node = el
            for _ in range(2):                      # climb to a meaningful ancestor
                if node.getparent() is not None:
                    node = node.getparent()
            if node not in hits:
                hits.append(node)
        if len(hits) >= 8:
            break
    if not hits:
        return prune_html(html, cap)
    parts = []
    budget = cap
    for node in hits:
        frag = prune_html(lxml.html.tostring(node, encoding="unicode"), budget // max(len(hits), 1))
        parts.append(frag)
    return "\n<!-- ▸ candidate region -->\n".join(parts)[:cap]
