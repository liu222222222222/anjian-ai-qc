# -*- coding: utf-8 -*-
"""模型适配层 - 统一接口，三来源可切换

所有适配器只暴露一个方法：
    chat_with_images(images: list[str], prompt: str) -> str

images 是本地图片绝对路径。适配器负责转 base64 并调用对应 API。
"""
import base64, json, os, time, urllib.request, urllib.error, ssl

CTX = ssl._create_unverified_context()


class ModelError(Exception):
    pass


class RateLimitError(ModelError):
    pass


def _post(url, payload, key, timeout=120, retries=6, backoff=(3, 6, 12, 25, 45, 60)):
    """带长退避的 POST，处理限流与过载"""
    data = json.dumps(payload).encode("utf-8")
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body), body
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            last = f"HTTP {e.code}: {raw[:400]}"
            # 429 限流、503 过载 → 退避重试
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(backoff[min(i, len(backoff) - 1)])
                continue
            if e.code == 429:
                raise RateLimitError(last)
            raise ModelError(last)
        except Exception as e:
            last = str(e)
            if i < retries - 1:
                time.sleep(backoff[min(i, len(backoff) - 1)])
    raise ModelError("请求失败: " + str(last))


def b64_image(path, max_side=1568, quality=82):
    """读图并压到合理尺寸（视觉 API 通用上限约 1568 短边）"""
    from PIL import Image
    import io
    im = Image.open(path)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    s = max(w, h)
    if s > max_side:
        r = max_side / s
        im = im.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class BaseAdapter:
    source = "base"
    supports_images = False

    def chat_with_images(self, images, prompt, **kw):
        raise NotImplementedError

    def chat_text(self, prompt, **kw):
        raise NotImplementedError


class OpenAICompatAdapter(BaseAdapter):
    """OpenAI 兼容：DeepSeek 官方 / 任何中转 / GLM / qwen-vl 通用"""

    def __init__(self, api_base, api_key, model_id, thinking="disabled", extra_headers=None):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.thinking = thinking
        self.extra_headers = extra_headers or {}
        self.supports_images = True

    def _build(self, images, prompt):
        content = [{"type": "text", "text": prompt}]
        for p in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + b64_image(p)},
            })
        return {"role": "user", "content": content}

    def _payload(self, messages):
        p = {
            "model": self.model_id,
            "messages": messages,
            "stream": False,
            "temperature": 0.1,
        }
        # thinking 仅对支持的模型传，未知模型不传避免报错
        if self.thinking == "disabled" and "deepseek" in self.model_id.lower():
            p["thinking"] = {"type": "disabled"}
        elif self.thinking == "enabled" and "deepseek" in self.model_id.lower():
            p["thinking"] = {"type": "enabled"}
        return p

    def chat_with_images(self, images, prompt, json_mode=True, **kw):
        p = self._payload([self._build(images, prompt)])
        if json_mode:
            p["response_format"] = {"type": "json_object"}
        obj, _ = _post(self.api_base + "/chat/completions", p, self.api_key,
                       timeout=kw.get("timeout", 180))
        return self._extract(obj)

    def chat_text(self, prompt, json_mode=True, **kw):
        p = self._payload([{"role": "user", "content": prompt}])
        if json_mode:
            p["response_format"] = {"type": "json_object"}
        obj, _ = _post(self.api_base + "/chat/completions", p, self.api_key,
                       timeout=kw.get("timeout", 60))
        return self._extract(obj)

    @staticmethod
    def _extract(obj):
        try:
            u = obj.get("usage") or {}
            txt = obj["choices"][0]["message"]["content"] or ""
            return {"text": txt, "tokens_in": u.get("prompt_tokens", 0),
                    "tokens_out": u.get("completion_tokens", 0)}
        except Exception:
            raise ModelError("返回结构异常: " + json.dumps(obj)[:300])

    def self_check(self):
        """视觉能力自检：发一张带文字的测试图，看模型能否读出"""
        probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe.jpg")
        from PIL import Image, ImageDraw
        if not os.path.exists(probe):
            im = Image.new("RGB", (640, 240), "white")
            d = ImageDraw.Draw(im)
            d.rectangle([40, 60, 600, 160], outline="black", width=3)
            d.text((70, 95), "2020.09 182000000002T", fill="black")
            im.save(probe, quality=90)
        try:
            r = self.chat_with_images([probe], "只输出图中印刷的那串数字，不要其他内容。",
                                      json_mode=False, timeout=60)
            t = (r["text"] or "").replace(" ", "")
            ok = "182000000002" in t
            return {"ok": ok, "raw": r["text"][:200],
                    "msg": "视觉能力正常" if ok else "模型未读出测试图文字，可能不支持图片输入"}
        except ModelError as e:
            return {"ok": False, "raw": "", "msg": str(e)}


class WorkbuddyAdapter(BaseAdapter):
    """WorkBuddy 云服务免密钥模型（需 applicationId，未接入时不可用）"""

    def __init__(self, application_id=None, model_id=None, **kw):
        self.application_id = application_id
        self.model_id = model_id or "hy3"

    def chat_with_images(self, images, prompt, **kw):
        raise ModelError(
            "WorkBuddy 云服务模型未接入：本项目缺少 .workbuddy/applications.yaml。"
            "请先在 WorkBuddy 中为本项目激活云服务，再到设置页填写 applicationId；"
            "或改用「自定义API/DeepSeek官方」来源。")

    def chat_text(self, prompt, **kw):
        raise ModelError("WorkBuddy 云服务模型未接入，请切换模型来源。")


def build_adapter(cfg):
    src = cfg.get("model_source", "deepseek")
    if src == "workbuddy":
        return WorkbuddyAdapter(cfg.get("application_id"), cfg.get("model_id"))
    base = cfg.get("api_base") or "https://api.deepseek.com"
    return OpenAICompatAdapter(base, cfg.get("api_key", ""), cfg.get("model_id") or "deepseek-flash",
                               cfg.get("thinking", "disabled"))


# 模型预设（设置页下拉用）
MODEL_PRESETS = [
    {"source": "deepseek", "label": "DeepSeek 官方 deepseek-flash", "model_id": "deepseek-flash",
     "api_base": "https://api.deepseek.com", "vision": True,
     "price": "输入 1-2 元/百万tok，输出 4-8 元/百万tok"},
    {"source": "openai_compat", "label": "智谱 GLM-4.6V-Flash（免费额度）", "model_id": "glm-4.6v-flash",
     "api_base": "https://open.bigmodel.cn/api/paas/v4", "vision": True, "price": "有免费额度"},
    {"source": "openai_compat", "label": "阿里 qwen3-vl-flash", "model_id": "qwen3-vl-flash",
     "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "vision": True,
     "price": "输入 0.18 元/百万tok"},
    {"source": "openai_compat", "label": "自定义中转（OpenAI 兼容）", "model_id": "",
     "api_base": "", "vision": True, "price": "取决于中转"},
    {"source": "workbuddy", "label": "WorkBuddy 云服务 Hy3", "model_id": "hy3",
     "api_base": "", "vision": True, "price": "限时免费（需激活云服务）"},
]
