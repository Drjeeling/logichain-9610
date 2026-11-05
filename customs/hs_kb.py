"""HS 编码知识库：JSON 加载 + Chroma 向量检索 + LLM 候选选择。

扩展后门设计：
- 当前用 5 品类 JSON 小库（data/hs_codes.json）
- 切换为完整海关税则：只需替换 JSON 文件，或调用 load_external_tax_schedule() 即可
- 切换为 Chroma 向量库：调用 enable_chroma() 即可，无需改 customs.py 上层代码

对外暴露接口（保持稳定，不改 customs.py）：
- lookup(cargo_name) -> List[Dict]  返回 Top3 候选
- choose(candidates, cargo_name) -> Dict  LLM 在候选中选择
- all_entries() -> List[Dict]  返回全部条目（OCR 模块用）
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_HS_PATH = DATA_DIR / "hs_codes.json"

# 材质/区分维度词表（detect_info_gap 用于从 keywords 与 easily_confused_with.reason
# 中提取区分维度，作为追问 options）
_MATERIAL_LEXICON = (
    "硅胶", "塑料", "PC", "TPU", "金属", "铝合金", "不锈钢", "钢",
    "铁", "皮革", "皮", "棉", "纺织", "化纤", "人发", "贵金属",
    "银", "金", "铂", "贱金属", "玻璃", "陶瓷", "木质", "木",
    "纸", "橡胶", "锂离子", "锂", "铅酸", "LED", "白炽",
)

# --- 打分常量（P0 修正：权重重排 + 归一化及格线） --- #
MAX_SCORE = 6 + 5 + 2  # keyword封顶6 + category 5 + description 2 = 13
THRESHOLD = 0.35  # 归一化及格线，低于此值返回空（NO_MATCH）

# --- 必填槽位表（P1 修正：槽位驱动提问，替代 easily_confused_with 单字段驱动） --- #
# 键名严格对齐 hs_codes.json 的 category 字段值，便于按品类查询槽位
REQUIRED_SLOTS: Dict[str, List[str]] = {
    "锂电池": ["电极材质", "用途", "是否含汞"],
    "塑料制品": ["材质", "是否含电子元件", "是否含品牌图案", "是否带功能件"],
    "照明": ["用途", "是否含品牌标识"],
    "服装": ["材质成分", "针织/梭织", "是否含品牌标识"],
    "电子产品": ["额定电压", "是否有接头", "用途"],
    "玩具": ["种类", "是否含品牌标识", "是否带动力装置"],
    "日用品": ["内胆材质", "是否含品牌标识"],
    "美发用品": ["制作材料", "长度"],
    "饰品": ["材质", "加工方法", "首饰种类", "贵金属成分含量"],
    "鞋类": ["鞋面材料", "外底材料", "款式"],
    "音箱": ["结构类型（是否有箱体）", "喇叭数量和尺寸", "是否可独立播放", "供电方式", "主要功能"],
}

# --- 知名品牌词表（P1 修正：知识产权风险独立检测） --- #
KNOWN_BRANDS = [
    "apple", "iphone", "ipad", "macbook", "airpods",
    "samsung", "galaxy", "huawei", "华为", "xiaomi", "小米",
    "sony", "索尼", "nike", "adidas", "puma",
    "disney", "迪士尼", "marvel", "pokemon", "宝可梦",
    "naruto", "火影", "one piece", "海贼王",
    "chanel", "香奈儿", "lv", "louis vuitton", "gucci",
    "rolex", "劳力士", "lego", "乐高", "barbie", "芭比",
]


class HSKnowledgeBase:
    """HS 编码知识库（可扩展）。"""

    def __init__(
        self,
        hs_path: Optional[Path] = None,
        use_chroma: bool = False,
        chroma_dir: Optional[Path] = None,
        verbose: bool = False,
    ):
        """
        Args:
            hs_path: HS 编码 JSON 文件路径
            use_chroma: 是否启用 Chroma 向量检索（大库时建议启用）
            chroma_dir: Chroma 持久化目录
            verbose: 详细日志
        """
        self.hs_path = hs_path or DEFAULT_HS_PATH
        self.use_chroma = use_chroma
        self.chroma_dir = chroma_dir or (DATA_DIR.parent / "chroma")
        self.verbose = verbose
        self._entries: List[Dict] = []
        self._meta: Dict = {}
        self._chroma_collection = None
        self._load()

    # ----------------------------------------------------------------- #
    # 加载（含扩展后门）
    # ----------------------------------------------------------------- #
    def _load(self) -> None:
        """从 JSON 加载 HS 编码库。"""
        with open(self.hs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._meta = data.get("_meta", {})
        self._entries = data.get("hs_codes", [])
        if self.verbose:
            print(f"[HS-KB] 加载 {len(self._entries)} 条 HS 编码，"
                  f"version={self._meta.get('version')}")

        # 若启用 Chroma，构建索引
        if self.use_chroma:
            self._build_chroma()

    def _build_chroma(self) -> None:
        """构建 Chroma 向量索引（首次启用会耗时）。"""
        try:
            import chromadb
        except ImportError:
            if self.verbose:
                print("[HS-KB] Chroma 未安装，降级为关键词匹配")
            self.use_chroma = False
            return

        client = chromadb.PersistentClient(path=str(self.chroma_dir))
        self._chroma_collection = client.get_or_create_collection(
            name="hs_codes",
            metadata={"description": "HS 编码知识库"},
        )

        # 已存在则跳过重建
        if self._chroma_collection.count() >= len(self._entries):
            if self.verbose:
                print(f"[HS-KB] Chroma 已有 {self._chroma_collection.count()} 条，跳过重建")
            return

        # 批量入库
        docs = [self._entry_to_text(e) for e in self._entries]
        ids = [str(i) for i in range(len(self._entries))]
        metadatas = [
            {"code": e["code"], "category": e.get("category", "")}
            for e in self._entries
        ]
        self._chroma_collection.add(documents=docs, ids=ids, metadatas=metadatas)
        if self.verbose:
            print(f"[HS-KB] Chroma 入库 {len(docs)} 条")

    @staticmethod
    def _entry_to_text(entry: Dict) -> str:
        """把 HS 条目转为可检索的文本（拼接所有关键词）。"""
        keywords = " / ".join(entry.get("keywords", []))
        return (
            f"HS编码 {entry['code']}：{entry['description']}。"
            f"关键词：{keywords}。"
            f"税率 {entry.get('tariff_rate', '未知')}，"
            f"监管 {entry.get('supervision_code', '无')}。"
            f"备注：{entry.get('remark', '')}"
        )

    # ----------------------------------------------------------------- #
    # 扩展后门：加载外部完整税则
    # ----------------------------------------------------------------- #
    def load_external_tax_schedule(self, json_path: Path) -> None:
        """加载外部完整海关税则（覆盖现有 5 品类小库）。

        外部 JSON 格式需保持 hs_codes.json 同结构：
        {
          "_meta": {...},
          "hs_codes": [
            {"code":"...", "description":"...", "tariff_rate":"...",
             "supervision_code":"...", "keywords":[...], "category":"..."}
          ]
        }
        """
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._meta = data.get("_meta", {})
        self._entries = data.get("hs_codes", [])
        if self.verbose:
            print(f"[HS-KB] 加载外部税则 {json_path.name}：{len(self._entries)} 条")

        # 若 Chroma 已启用，重新构建
        if self.use_chroma:
            # 清空原索引
            if self._chroma_collection:
                self._chroma_collection.delete(
                    ids=[str(i) for i in range(100000)]
                )
            self._build_chroma()

    # ----------------------------------------------------------------- #
    # 查询接口
    # ----------------------------------------------------------------- #
    def lookup(self, query: str, top_k: int = 3) -> List[Dict]:
        """返回 Top-K 候选 HS 编码条目（归一化打分 + 及格线过滤）。

        P0 修正：
        - 权重重排：keyword min(len,6) + category 5 + description 2（从 10 降至 2）
        - 引入归一化及格线 THRESHOLD=0.35，低于此值返回空（NO_MATCH）
        - 返回的条目附带 _score / _norm_score 供 calc_confidence 使用

        P1 增强：common_names 子串匹配 +5（俗称精确命中是结构化强信号）
        - 解决短查询（如"手机壳"/"数据线"）过不了及格线的问题
        - 无意义查询（如"东西"）仍 0 分被卡，符合"防止凑数"本意
        - norm 封顶 1.0，避免多信号累加导致 norm>1
        """
        if not self._entries:
            return []

        # Chroma 路径：向量检索（大库时使用）
        if self.use_chroma and self._chroma_collection:
            results = self._chroma_collection.query(
                query_texts=[query], n_results=min(top_k, len(self._entries))
            )
            ids = results["ids"][0]
            return [self._entries[int(i)] for i in ids]

        # 关键词匹配打分（权重重排：category/common_names > keyword > description）
        scored = []
        q_lower = query.lower()
        for entry in self._entries:
            score = 0
            # keyword：封顶 6，长词权重高但不垄断
            for kw in entry.get("keywords", []):
                if kw.lower() in q_lower:
                    score += min(len(kw), 6)
            # category：结构化硬信号，权重最高
            if entry.get("category", "").lower() in q_lower:
                score += 5
            # common_names 子串匹配：俗称命中（结构化强信号，权重 5）
            # 解决"手机壳"/"数据线"等短俗称词过不了及格线的问题
            common_names = entry.get("common_names", []) or []
            if any(cn and cn.lower() in q_lower for cn in common_names):
                score += 5
            # description：自由文本，权重最低（从 10 降至 2）
            if entry["description"].lower() in q_lower:
                score += 2
            # norm 封顶 1.0，避免多信号累加导致 norm>1
            norm = min(score / MAX_SCORE, 1.0) if MAX_SCORE > 0 else 0
            scored.append((score, norm, entry))

        scored.sort(key=lambda x: -x[0])

        # 及格线过滤：归一化最高分 < THRESHOLD → NO_MATCH（明确返回空，直接转人工）
        if not scored or scored[0][1] < THRESHOLD:
            return []

        # 只返回达到及格线的 Top-K，附带分数供下游使用
        result = []
        for s, norm, e in scored[:top_k]:
            if s > 0:
                out = dict(e)  # 浅拷贝，不污染原条目
                out["_score"] = s
                out["_norm_score"] = round(norm, 3)
                result.append(out)
        return result

    def choose(self, candidates: List[Dict], cargo_query: str) -> Optional[Dict]:
        """在候选中选择最匹配的 HS 编码，并输出可解释归类推理链。

        P0 修正：
        - 新增 calc_confidence() 双维度判定（绝对分 + 区分度 gap）
        - 唯一候选不再自动判 high，需绝对分 > 0.70
        - 无 LLM 时 confidence 封顶 medium + degraded=True
        - LLM 置信度不超过算法置信度（取较低值）

        返回的 Dict 在原 entry 字段基础上新增：
        - reasoning_chain: List[str]  归类推理步骤
        - counterfactual: Dict        反事实对比
        - confidence: str            置信度 high/medium/low/none
        - confidence_reason: str      置信度理由
        - degraded: bool             降级标记（无 LLM 或 LLM 失败时为 True）
        """
        if not candidates:
            return None

        # ③.5 calc_confidence：双维度判定（绝对分 + 区分度）
        algo_conf, algo_reason = self._calc_confidence(candidates)

        # 无 API Key：封顶 medium，降级标记，禁止 A 级
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            capped = self._cap_confidence("medium", algo_conf)
            return self._annotate(
                candidates[0],
                reasoning_chain=[],
                counterfactual={},
                confidence=capped,
                confidence_reason=f"推理链缺失，仅关键词匹配（{algo_reason}）",
                degraded=True,
            )

        # LLM 可解释推理路径
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

            prompt = self._build_choose_prompt(candidates, cargo_query)
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=800,
            )
            answer = resp.choices[0].message.content.strip()
            parsed = self._parse_json_from_text(answer)

            if isinstance(parsed, dict):
                idx = self._safe_index(parsed.get("selected_index"), len(candidates))
                if idx is not None:
                    confused = parsed.get("counterfactual") or {}
                    llm_conf = str(parsed.get("confidence") or "medium")
                    # LLM 置信度不超过算法置信度
                    final_conf = self._cap_confidence(llm_conf, algo_conf)
                    return self._annotate(
                        candidates[idx],
                        reasoning_chain=self._as_str_list(parsed.get("reasoning_chain")),
                        counterfactual={
                            "confused_code": confused.get("confused_code", ""),
                            "why_not": confused.get("why_not", ""),
                        },
                        confidence=final_conf,
                        confidence_reason=str(parsed.get("confidence_reason") or algo_reason),
                        degraded=False,
                    )
                if self.verbose:
                    print(f"[HS-KB] LLM selected_index 无效，降级: {parsed}")
            else:
                if self.verbose:
                    print("[HS-KB] LLM 返回非 JSON，尝试兼容旧版编号提取")
                import re
                m = re.search(r"[1-9]\d*", answer or "")
                if m:
                    idx = int(m.group()) - 1
                    if 0 <= idx < len(candidates):
                        capped = self._cap_confidence("medium", algo_conf)
                        return self._annotate(
                            candidates[idx],
                            reasoning_chain=[],
                            counterfactual={},
                            confidence=capped,
                            confidence_reason=f"LLM 仅返回编号（{algo_reason}）",
                            degraded=True,
                        )
        except Exception as e:
            if self.verbose:
                print(f"[HS-KB] LLM 推理失败，降级: {e}")

        # LLM 失败：降级
        capped = self._cap_confidence("medium", algo_conf)
        return self._annotate(
            candidates[0],
            reasoning_chain=[],
            counterfactual={},
            confidence=capped,
            confidence_reason=f"LLM 调用失败，降级（{algo_reason}）",
            degraded=True,
        )

    @staticmethod
    def _calc_confidence(candidates: List[Dict]) -> tuple:
        """双维度置信度判定：绝对分（norm）+ 区分度（gap）。

        - 无候选 → ("none", "无匹配候选")
        - 绝对分 < THRESHOLD → ("low", ...)
        - 唯一候选：norm > 0.70 → high，否则 medium（不再自动判 high）
        - 多候选：gap < 0.15 → low；gap > 0.40 → high；否则 medium
        """
        if not candidates:
            return ("none", "无匹配候选")

        top1_norm = candidates[0].get("_norm_score", 0)

        if top1_norm < THRESHOLD:
            return ("low", f"绝对分 {top1_norm:.2f} 低于及格线 {THRESHOLD}")

        if len(candidates) == 1:
            if top1_norm > 0.70:
                return ("high", f"绝对分 {top1_norm:.2f} 足够高")
            return ("medium", f"唯一候选，绝对分 {top1_norm:.2f} 不足以判 high")

        # 多候选：看区分度
        top2_norm = candidates[1].get("_norm_score", 0)
        gap = (top1_norm - top2_norm) / top1_norm if top1_norm > 0 else 0

        if gap < 0.15:
            return ("low", f"区分度 gap={gap:.2f} 过小，知识库无法区分")
        if gap > 0.40:
            return ("high", f"绝对分 {top1_norm:.2f}，区分度 gap={gap:.2f}")
        return ("medium", f"绝对分 {top1_norm:.2f}，区分度 gap={gap:.2f}")

    @staticmethod
    def _cap_confidence(llm_conf: str, algo_conf: str) -> str:
        """取 LLM 置信度与算法置信度中较低的一个（安全优先）。"""
        order = {"low": 0, "medium": 1, "high": 2, "none": 0}
        llm_val = order.get(llm_conf, 1)
        algo_val = order.get(algo_conf, 1)
        result_val = min(llm_val, algo_val)
        for k, v in order.items():
            if v == result_val and k != "none":
                return k
        return "medium"

    # ---------------------------------------------------------------- #
    # choose 辅助方法（可解释推理链构建/解析）
    # ---------------------------------------------------------------- #
    @staticmethod
    def _annotate(entry: Dict, reasoning_chain: List[str],
                  counterfactual: Dict, confidence: str,
                  confidence_reason: str, degraded: bool = False) -> Dict:
        """在候选 entry 的浅拷贝上附加推理链字段（不污染原知识库条目）。"""
        out = dict(entry)  # 浅拷贝，避免修改 self._entries
        out["reasoning_chain"] = reasoning_chain
        out["counterfactual"] = counterfactual
        out["confidence"] = confidence
        out["confidence_reason"] = confidence_reason
        out["degraded"] = degraded
        return out

    @staticmethod
    def _build_choose_prompt(candidates: List[Dict], cargo_query: str) -> str:
        """构建可解释归类推理 prompt（含候选字段 + 归类总规则 + 反事实对比要求）。"""
        prompt = (
            "你是海关 HS 编码归类专家。请按归类总规则（类注→章注→子目注释）"
            f"逐步推演，从下列候选中选出最匹配商品「{cargo_query}」的编码。\n\n"
            f"商品描述：{cargo_query}\n\n"
            "候选 HS 编码：\n"
        )
        for i, e in enumerate(candidates, 1):
            confused = e.get("easily_confused_with") or {}
            prompt += (
                f"\n{i}. {e.get('code')} - {e.get('description')}\n"
                f"   官方名称：{e.get('official_name', '')}\n"
                f"   关键词：{', '.join(e.get('keywords', []) or [])}\n"
                f"   俗称：{', '.join(e.get('common_names', []) or [])}\n"
                f"   易混淆：{confused.get('code', '')} - {confused.get('reason', '')}\n"
            )
        prompt += (
            "\n要求：\n"
            "1. 按归类总规则推演：先确定章（材料/用途），再确定子目，给出每步推理；\n"
            "2. 给出反事实对比：为什么不是那个最容易混淆的编码；\n"
            "3. 按商品族（材质/用途明确程度）给出置信度 high/medium/low。\n\n"
            "输出严格 JSON（不要 markdown 代码块，不要多余文字）：\n"
            "{\n"
            '  "selected_index": 1,\n'
            '  "reasoning_chain": ["步骤1：...", "步骤2：...", "步骤3：..."],\n'
            '  "counterfactual": {\n'
            '    "confused_code": "8507.10.00",\n'
            '    "why_not": "因为..."\n'
            '  },\n'
            '  "confidence": "high",\n'
            '  "confidence_reason": "材质明确，用途明确..."\n'
            "}"
        )
        return prompt

    @staticmethod
    def _parse_json_from_text(text: str) -> Optional[Dict]:
        """从 LLM 输出中提取首个 JSON 对象（兼容 markdown 代码块包裹）。"""
        import re
        if not text:
            return None
        # 优先取 ```json ... ``` 块
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = m.group(1) if m else text
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(candidate[start:end + 1])
        except Exception:
            return None

    @staticmethod
    def _safe_index(raw, length: int) -> Optional[int]:
        """把 LLM 返回的 selected_index 转成 0-based 索引，越界返回 None。"""
        try:
            idx = int(raw) - 1
        except (TypeError, ValueError):
            return None
        return idx if 0 <= idx < length else None

    @staticmethod
    def _as_str_list(raw) -> List[str]:
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return []

    def all_entries(self) -> List[Dict]:
        """返回全部条目（OCR 模块用于商品名匹配）。"""
        return self._entries

    def find_by_code(self, code: str) -> Optional[Dict]:
        """按精确 HS 编码查询。"""
        for e in self._entries:
            if e["code"] == code:
                return e
        return None

    def find_by_category(self, category: str) -> Optional[Dict]:
        """按品类名查询（OCR 模块常用）。"""
        for e in self._entries:
            if e.get("category") == category:
                return e
        return None

    # ---------------------------------------------------------------- #
    # 主动提问：信息缺口检测
    # ---------------------------------------------------------------- #
    def detect_info_gap(self, cargo_query: str) -> Optional[Dict]:
        """检测商品信息是否完整，不完整时生成结构化提问。

        P1 修正：槽位驱动提问
        - 从 REQUIRED_SLOTS 表查询品类必填槽位（替代 easily_confused_with 单字段触发）
        - easily_confused_with 字段保留用于反事实对比展示（见 choose()），不再驱动提问
        - NO_MATCH（lookup 返回空）→ 不提问，留给上层 ask() 处理

        返回结构：
            {
              "missing_fields": ["材质", "用途", ...],
              "question": "您的商品「...」需要补充以下信息以精确归类：...？",
              "options": ["硅胶", "塑料", ...],
              "why_matters": "上述字段决定 HS 编码归类子目"
            }
        """
        if not cargo_query or not self._entries:
            return None

        query = cargo_query.strip()

        # 描述太短 → 兜底提问
        if len(query) < 4:
            return {
                "missing_fields": ["材质", "用途"],
                "question": f"您的商品描述「{query}」较短，请补充材质和用途信息？",
                "options": list(_MATERIAL_LEXICON)[:8],
                "why_matters": "材质和用途是 HS 编码归类的核心依据，缺失可能导致归类错误",
            }

        # 找到匹配的候选
        matched = self.lookup(query, top_k=3)
        if not matched:
            return None  # NO_MATCH，留给上层 ask() 处理

        # 合并候选品类的必填槽位（去重，保持顺序）
        required: List[str] = []
        seen: set = set()
        for entry in matched:
            cat = entry.get("category", "")
            for slot in REQUIRED_SLOTS.get(cat, []):
                if slot not in seen:
                    required.append(slot)
                    seen.add(slot)

        if not required:
            return None  # 品类无必填槽位定义，无需提问

        # 检测每个槽位是否已填
        missing = [s for s in required if not self._slot_filled(s, query)]
        if not missing:
            return None  # 信息已足够

        # 取前 4 个缺失槽位（避免提问过长）
        missing = missing[:4]

        # 提取材质备选选项（供用户参考）
        options: List[str] = []
        opt_seen: set = set()
        for entry in matched:
            for o in self._extract_material_options(entry):
                if o not in opt_seen:
                    options.append(o)
                    opt_seen.add(o)

        # why_matters：合并 easily_confused_with.reason 作为风险提示
        why_parts = []
        for entry in matched:
            c = entry.get("easily_confused_with") or {}
            if c.get("reason"):
                why_parts.append(f"{entry.get('code')}: {c['reason']}")
        why = "；".join(why_parts) if why_parts else "上述字段决定 HS 编码归类子目"

        field_label = "、".join(missing)
        return {
            "missing_fields": missing,
            "question": f"您的商品「{query}」需要补充以下信息以精确归类：{field_label}？",
            "options": options[:8],
            "why_matters": why,
        }

    @staticmethod
    def _slot_filled(slot: str, query: str) -> bool:
        """启发式判定槽位是否已在 query 中填写。

        - 材质类槽位：检查是否含 _MATERIAL_LEXICON 中的材质词
        - "针织/梭织"：检查是否含"针织"或"梭织"
        - "额定电压"：检查是否含数字+V 模式
        - "长度"：检查是否含长度数字模式
        - "是否XXX"类二元槽位：保守视为未填（需用户显式回答）
        - 其他无明确关键词的槽位：保守视为未填
        """
        import re
        q = query.lower()

        # 材质类槽位
        if slot in ("材质", "材质成分", "内胆材质", "鞋面材料",
                    "外底材料", "电极材质", "制作材料"):
            return any(m.lower() in q for m in _MATERIAL_LEXICON)

        if slot == "针织/梭织":
            return "针织" in query or "梭织" in query

        if slot == "额定电压":
            return bool(re.search(r"\d+\s*v", q))

        if slot == "长度":
            return bool(re.search(r"\d+\s*(cm|寸|英寸|inch)", q))

        # "是否XXX" 类二元槽位：需用户显式回答
        if slot.startswith("是否"):
            return False

        # 用途/种类/款式/加工方法/首饰种类/贵金属成分含量等：
        # 无明确关键词，保守视为未填（避免误判归类错误）
        return False

    @staticmethod
    def _extract_material_options(entry: Dict) -> List[str]:
        """从 entry 的 keywords 与 easily_confused_with.reason 中提取材质/区分维度词。"""
        options: List[str] = []
        seen: set = set()
        # 从 keywords 中挑选含材质词的项（如"硅胶手机壳""塑料手机壳"）
        for kw in entry.get("keywords", []) or []:
            if any(m in kw for m in _MATERIAL_LEXICON) and kw not in seen:
                options.append(kw)
                seen.add(kw)
        # 从 easily_confused_with.reason 中补充材质词（如"锂离子""铅酸""LED"）
        confused = entry.get("easily_confused_with") or {}
        reason = confused.get("reason", "") or ""
        for m in _MATERIAL_LEXICON:
            if m in reason and m not in seen:
                options.append(m)
                seen.add(m)
        return options

    # ---------------------------------------------------------------- #
    # 知识产权风险独立检测（P1 修正：跨境电商高频触发条件）
    # ---------------------------------------------------------------- #
    @staticmethod
    def detect_ip_risk(cargo_query: str) -> Optional[Dict]:
        """检测商品描述是否涉及知名品牌，触发知识产权侵权风险提示。

        跨境电商 9610 高频触发条件：
        - 商品名/品牌词命中 KNOWN_BRANDS 词表 → 视为 IP_RISK
        - 由 customs.py _determine_tier 强制进入 C 级分流（挂起人工审核）

        返回：
            - 无风险 → None
            - 有风险 → {
                "is_risky": True,
                "matched_brands": ["apple", "iphone"],
                "advice": "..."
              }
        """
        if not cargo_query:
            return None
        q = cargo_query.lower()
        matched = [b for b in KNOWN_BRANDS if b in q]
        if not matched:
            return None
        return {
            "is_risky": True,
            "matched_brands": matched,
            "advice": (
                "跨境电商 9610 出口高频触发知识产权侵权风险："
                "命中知名品牌词，需核查 (1) 是否为正品授权货物 "
                "(2) 是否有商标/外观专利授权文件 "
                "(3) 是否涉及目的国商标注册。"
                "未提供授权证明前，归类结果强制进入 C 级人工审核。"
            ),
        }
