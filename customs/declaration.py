"""报关单证生成 + 合规规则引擎（v4：结构化时效 + 例外 + 审核状态）。"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DATA_DIR = Path(__file__).resolve().parent / "data"


class DeclarationGenerator:
    """报关单生成器（Jinja2 模板）。"""

    def __init__(self, templates_dir: Path = TEMPLATES_DIR):
        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape([]),
            keep_trailing_newline=True,
        )

    def render(
        self,
        cargo: str,
        hs_code: Dict,
        context: Optional[Dict[str, Any]] = None,
        cargo_info: Optional[Dict] = None,
    ) -> str:
        """渲染报关单草稿。"""
        ctx = context or {}
        info = cargo_info or {}

        template = self.env.get_template("declaration.j2")
        return template.render(
            cargo=cargo,
            hs_code=hs_code or {},
            from_port=ctx.get("from_port") or ctx.get("destination_port"),
            dest_port=ctx.get("dest_port"),
            tonnage=info.get("tonnage"),
            unit_price=info.get("unit_price_usd"),
            total_usd=(
                float(info.get("tonnage", 0)) * float(info.get("unit_price_usd", 0))
                if info.get("tonnage") and info.get("unit_price_usd") else None
            ),
            package_type=info.get("package_type"),
            gross_weight=info.get("gross_weight"),
            net_weight=info.get("net_weight"),
            supervision_code=(hs_code or {}).get("supervision_code"),
        )


class ComplianceRulesEngine:
    """合规规则引擎：v4 结构化时效 + 例外 + 审核状态；向后兼容 v3 格式。"""

    EXPIRING_SOON_DAYS = 180  # 到期前多少天内算"临期"

    def __init__(self, rules_path: Path = DATA_DIR / "rules.json"):
        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._meta = data.get("_meta", {})
        self._rules: List[Dict] = data.get("rules", [])
        # 启动时先按当前日期对每条规则的 policy_validity 做一次状态刷新
        self._refresh_rule_statuses()

    # ----------------------------------------------------------------- #
    # 主入口
    # ----------------------------------------------------------------- #
    def validate(
        self,
        cargo: str,
        hs_code: Optional[Dict],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """检查所有 active 规则，返回触发的风险列表。

        v4 新增：
        - 自动跳过 status ∈ {expired, superseded} 的规则
        - review_status ∈ {draft, needs_review} 的规则 risk 标注"待核验"
        - exceptions 命中时不触发（例外优先）
        """
        ctx = context or {}
        dest = ctx.get("dest_port") or ctx.get("dest_country", "")
        tariff = (hs_code or {}).get("tariff_rate", "0%")
        hs_category = (hs_code or {}).get("category", "")
        hs_code_str = (hs_code or {}).get("code", "")

        # 构建 condition 之外的额外信号
        extra = {
            "trade_mode": ctx.get("trade_mode"),
        }

        risks = []
        for rule in self._rules:
            status = rule.get("status", "active")
            # 跳过已废弃/过期
            if status in ("expired", "superseded"):
                continue

            cond = rule.get("condition", {}) or {}
            if not self._match_rule(
                cond, cargo, dest, tariff, hs_category,
                hs_code_str=hs_code_str, extra=extra,
            ):
                continue

            # 例外检查：命中例外则跳过
            if self._match_exceptions(rule.get("exceptions"), cargo):
                continue

            risk_item = self._build_risk_item(rule, tariff, cargo, dest)
            risks.append(risk_item)

        return risks

    # ----------------------------------------------------------------- #
    # 时效状态机
    # ----------------------------------------------------------------- #
    def _refresh_rule_statuses(self) -> None:
        """按当前日期刷新每条规则的 status 字段。

        状态派生优先级：superseded > expired > expiring_soon > active。
        R017 这类"到期前 N 个月预警"的规则本身是 active，它的 condition 触发另一个 info 提示。
        """
        today = date.today()
        for rule in self._rules:
            # 规则自带明确状态优先（比如 R017 的 expiring_soon_when_proximity）
            declared = rule.get("status", "active")
            if declared in ("superseded",):
                continue  # superseded 不自动派生，保持原样

            validity = rule.get("policy_validity") or {}
            valid_to_str = validity.get("valid_to")
            if not valid_to_str:
                rule["status"] = "active"
                continue

            try:
                valid_to = datetime.strptime(valid_to_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                rule["status"] = declared if declared != "expiring_soon_when_proximity" else "active"
                continue

            days_left = (valid_to - today).days
            if days_left < 0:
                rule["status"] = "expired"
            elif days_left <= self.EXPIRING_SOON_DAYS:
                rule["status"] = "expiring_soon"
            else:
                rule["status"] = "active"

    def expired_rules(self) -> List[Dict]:
        """返回当前已过期或临期的规则清单（启动/演示时调用一次即可）。"""
        return [r for r in self._rules if r.get("status") in ("expired", "expiring_soon")]

    # ----------------------------------------------------------------- #
    # 规则匹配
    # ----------------------------------------------------------------- #
    @staticmethod
    def _match_rule(
        cond: Dict,
        cargo: str,
        dest: str,
        tariff: str,
        hs_category: str,
        hs_code_str: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """v3/v4 兼容的 AND 条件匹配。

        新增 trade_mode 条件（extra 传入）。
        """
        extra = extra or {}

        if "cargo" in cond and cond["cargo"] != cargo:
            return False
        if "hs_category" in cond and cond["hs_category"] != hs_category:
            return False
        if "dest_country" in cond and cond["dest_country"] != dest:
            return False
        if "trade_mode" in cond and cond["trade_mode"] != extra.get("trade_mode"):
            return False

        if "cargo_in" in cond and cargo not in cond["cargo_in"]:
            return False

        if "dest_country_in" in cond and dest not in cond["dest_country_in"]:
            return False

        if "hs_tariff_gt" in cond:
            try:
                threshold = float(cond["hs_tariff_gt"].rstrip("%"))
                actual = float(tariff.rstrip("%"))
                if actual <= threshold:
                    return False
            except (ValueError, AttributeError):
                return False

        if "cargo_contains" in cond:
            needles = cond["cargo_contains"]
            if isinstance(needles, str):
                needles = [needles]
            if not any(n in cargo for n in needles):
                return False

        if "hs_code_ends_with" in cond:
            suffixes = cond["hs_code_ends_with"]
            if isinstance(suffixes, str):
                suffixes = [suffixes]
            if not hs_code_str or not any(
                hs_code_str.endswith(s) for s in suffixes
            ):
                return False

        if "hs_code_not_ten_digit" in cond:
            code = (hs_code_str or "").replace(".", "")
            if code.isdigit() and len(code) == 10:
                return False

        if "dest_country_contains" in cond:
            needles = cond["dest_country_contains"]
            if isinstance(needles, str):
                needles = [needles]
            if not any(n in dest for n in needles):
                return False

        return True

    @staticmethod
    def _match_exceptions(
        exceptions: Optional[List[Dict]],
        cargo: str,
    ) -> bool:
        """OR 分组例外：任一命中即视为"例外成立"，规则跳过不触发。

        例外格式：
        [{"field": "cargo", "op": "contains", "value": "食品", "reason": "..."}]
        """
        if not exceptions:
            return False
        for exc in exceptions:
            field = exc.get("field")
            op = exc.get("op", "contains")
            value = exc.get("value")
            if field == "cargo" and op == "contains" and value in cargo:
                return True
        return False

    # ----------------------------------------------------------------- #
    # 风险项拼装（消费 v4 字段）
    # ----------------------------------------------------------------- #
    def _build_risk_item(
        self,
        rule: Dict,
        tariff: str,
        cargo: str,
        dest: str,
    ) -> Dict:
        """把规则 dict 渲染成风险项；v4 字段优先消费，fallback 到 v3 字段。"""
        risk_text = rule["risk"].format(tariff=tariff, cargo=cargo, dest=dest)

        # review_status 标注
        review_status = rule.get("review_status")
        if review_status in ("draft", "needs_review"):
            risk_text += "（⚠️ 本条规则未经原文核验，使用前请对照法规原文确认）"

        item = {
            "rule_id": rule.get("rule_id"),
            "rule_name": rule.get("rule_name"),
            "risk": risk_text,
            "advice": rule.get("advice", ""),
            "severity": rule.get("severity", "info"),
        }

        # 政策依据：v4 source_document_id 优先
        if rule.get("source_document_id"):
            item["source_document_id"] = rule["source_document_id"]
            item["source_article"] = rule.get("source_article")
        # v3 policy_source / policy_validity fallback
        if rule.get("policy_source"):
            item["policy_source"] = rule["policy_source"]
        validity = rule.get("policy_validity")
        if validity and isinstance(validity, dict):
            item["policy_validity"] = validity
        elif rule.get("policy_validity"):
            item["policy_validity"] = rule["policy_validity"]

        # 规则状态
        status = rule.get("status", "active")
        if status in ("expiring_soon", "expired"):
            item["rule_status"] = status

        return item
