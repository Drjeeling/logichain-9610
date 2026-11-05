"""OCR 模块：PaddleOCR 提取文本 + DeepSeek-VLM 结构化抽取。

双路设计：
1. PaddleOCR（本地）：从图片提取所有可见文本，输出 raw_text
2. DeepSeek-VLM（可选）：把 raw_text 结构化为 JSON 字段

留有后门：
- 无图片时支持纯文字输入
- 无 VLM API 时直接用 PaddleOCR 文本 + 正则兜底
- VLM 失败时降级为正则提取
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

try:  # 独立调用时也加载 .env，保证 DEEPSEEK_API_KEY 可用
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# OCR 引擎懒加载，避免 import 时下载模型
# 优先 rapidocr-onnxruntime（与 doc_reader 保持一致、安装轻量），
# 未安装时回退 paddleocr。
_rapid_ocr = None
_paddle_ocr = None


def _get_rapid():
    global _rapid_ocr
    if _rapid_ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid_ocr = RapidOCR()
    return _rapid_ocr


def _get_paddle():
    global _paddle_ocr
    if _paddle_ocr is None:
        from paddleocr import PaddleOCR
        # paddleocr 3.x 已弃用 show_log 参数，用 try 兼容
        try:
            _paddle_ocr = PaddleOCR(use_angle_cls=True, lang="ch")
        except TypeError:
            _paddle_ocr = PaddleOCR(lang="ch")
    return _paddle_ocr


@dataclass
class CargoInfo:
    """从发票/装箱单抽取的结构化商品信息。"""
    cargo_name: str = ""
    tonnage: float = 0.0
    unit_price_usd: float = 0.0
    material: str = ""
    usage: str = ""
    raw_text: str = ""           # OCR 原始文本，便于追溯
    source: str = "unknown"     # 来源：text_input / paddle_ocr / vlm

    def to_dict(self) -> Dict:
        return {
            "cargo_name": self.cargo_name,
            "tonnage": self.tonnage,
            "unit_price_usd": self.unit_price_usd,
            "material": self.material,
            "usage": self.usage,
            "source": self.source,
        }


class OCRExtractor:
    """OCR + VLM 双路抽取器。"""

    def __init__(self, use_vlm: bool = True, verbose: bool = False):
        self.use_vlm = use_vlm and bool(os.getenv("DEEPSEEK_API_KEY"))
        self.verbose = verbose

    # ----------------------------------------------------------------- #
    # 主入口
    # ----------------------------------------------------------------- #
    def extract(self, input_data: str) -> CargoInfo:
        """输入支持两种形式：
        - 文本描述（如 "锂电池20吨"）：直接走正则抽取
        - 图片路径（如 "/path/to/invoice.jpg"）：走 PaddleOCR + VLM
        """
        if not input_data:
            return CargoInfo()

        # 判断是否为图片路径
        if input_data.strip().lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
            return self._extract_from_image(input_data)
        else:
            return self._extract_from_text(input_data)

    # ----------------------------------------------------------------- #
    # 纯文字抽取（兜底/简化模式）
    # ----------------------------------------------------------------- #
    def _extract_from_text(self, text: str) -> CargoInfo:
        """从文字描述中抽取商品信息（正则匹配）。"""
        if self.verbose:
            print(f"[OCR] 走文字抽取路径: {text}")

        # 商品名：从已知品类关键词中匹配
        cargo_name = self._match_cargo_name(text)

        # 货量（优先「吨」，其次「KG/KGS/千克」按 1000kg=1吨换算）
        m = re.search(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*吨", text)
        if m:
            tonnage = float(m.group(1).replace(",", ""))
        else:
            m = re.search(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:KGS?|千克)",
                          text, re.I)
            tonnage = float(m.group(1).replace(",", "")) / 1000.0 if m else 0.0

        # 单价（美元）
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:美元|USD|\$)", text)
        unit_price = float(m.group(1)) if m else 0.0

        return CargoInfo(
            cargo_name=cargo_name or "未识别商品",
            tonnage=tonnage,
            unit_price_usd=unit_price,
            material="",
            usage="",
            raw_text=text,
            source="text_input",
        )

    # ----------------------------------------------------------------- #
    # 图片抽取（PaddleOCR + VLM）
    # ----------------------------------------------------------------- #
    def _extract_from_image(self, image_path: str) -> CargoInfo:
        """从图片中提取商品信息。"""
        if self.verbose:
            print(f"[OCR] 走图片抽取路径: {image_path}")

        try:
            raw_text = self._paddle_extract(image_path)
        except Exception as e:
            if self.verbose:
                print(f"[OCR] PaddleOCR 失败，降级: {e}")
            return CargoInfo(cargo_name="图片识别失败", source="error")

        if self.use_vlm:
            structured = self._vlm_extract(raw_text)
            if structured:
                return CargoInfo(
                    cargo_name=structured.get("cargo_name", ""),
                    tonnage=float(structured.get("tonnage", 0)),
                    unit_price_usd=float(structured.get("unit_price_usd", 0)),
                    material=structured.get("material", ""),
                    usage=structured.get("usage", ""),
                    raw_text=raw_text,
                    source="ocr+vlm",
                )

        # VLM 不可用或失败：走正则兜底
        return self._extract_from_text(raw_text)

    def _paddle_extract(self, image_path: str) -> str:
        """提取所有可见文本：rapidocr 优先，paddleocr 兜底。"""
        try:
            result, _ = _get_rapid()(image_path)
            if result:
                return "\n".join(item[1] for item in result)
            return ""
        except ImportError:
            pass

        ocr = _get_paddle()
        result = ocr.ocr(image_path, cls=True)
        if not result or not result[0]:
            return ""
        lines = [line[1][0] for line in result[0]]
        return "\n".join(lines)

    def _vlm_extract(self, raw_text: str) -> Optional[Dict]:
        """DeepSeek-VLM 把 OCR 文本结构化为 JSON。"""
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                base_url="https://api.deepseek.com",
            )
            prompt = """从以下发票 OCR 文本中提取关键字段，输出 JSON：
{
  "cargo_name": "商品名称",
  "tonnage": 数量(数字,单位吨),
  "unit_price_usd": 单价(数字,美元),
  "material": "材质",
  "usage": "用途"
}
只输出 JSON，不要其他解释。

OCR 文本：
""" + raw_text

            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
            )
            content = resp.choices[0].message.content.strip()
            # 去掉可能的 ```json ... ``` 包裹
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE)
            return json.loads(content)
        except Exception as e:
            if self.verbose:
                print(f"[OCR] VLM 抽取失败: {e}")
            return None

    # ----------------------------------------------------------------- #
    # 商品名匹配辅助（与 hs_codes.json 联动）
    # ----------------------------------------------------------------- #
    @staticmethod
    def _match_cargo_name(text: str) -> Optional[str]:
        """从文本中匹配已知商品名。"""
        from .hs_kb import HSKnowledgeBase
        kb = HSKnowledgeBase()
        for entry in kb.all_entries():
            for kw in entry.get("keywords", []):
                if kw.lower() in text.lower():
                    return entry.get("category", "")
        return None
