"""E 归并 Agent。

9610 出口"清单核放、汇总申报"模式下，结关后需按"八个同一"
对清单进行归并，生成月度汇总报关单。归并错误影响退税。

"八个同一"：同一商品名称、同一 HS 编码、同一规格型号、
同一计量单位、同一币制、同一原产国、同一最终目的国、同一收发货人。

本 Agent（简化版）：
1. 检查当前清单的归并要素是否齐全。
2. 若提供多票清单，按"八个同一"分组。
3. 检出分组冲突（同一商品被归到不同税号等）。

接入契约（state.py AGENT_IO_CONTRACT）：
- reads: order, sku_info, hs_code, declaration_dict, decl_compliance
- writes: merge_result

不调用 LLM（纯分组算法）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .base import BaseAgent, AgentResult


# "八个同一"字段定义
MERGE_KEYS = [
    ("cargo_name", "商品名称"),
    ("hs_code", "HS编码"),
    ("specification", "规格型号"),
    ("unit", "计量单位"),
    ("currency", "币制"),
    ("origin_country", "原产国"),
    ("dest_country", "最终目的国"),
    ("shipper_consignee", "收发货人"),
]


class MergeAgent(BaseAgent):
    """归并 Agent：八个同一分组 + 冲突检测。"""

    name = "merge"
    description = "9610 月度汇总归并：八个同一分组 + 冲突检测"

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        ctx = context or {}
        order = ctx.get("order") or {}
        manifests = ctx.get("manifests") or []

        # K3 修复：归并要素不能只看原始 order，
        # 需消费 A（hs_code/declaration_dict）与 C（decl_compliance）的产出。
        effective = self._merge_upstream(order, ctx)

        # ---- 单票归并预检 ----
        single_check = self._check_single_merge(effective)

        # ---- 合规阻断（C 槽 critical）----
        dc = ctx.get("decl_compliance") or {}
        compliance_critical = [
            i for i in dc.get("issues") or [] if i.get("severity") == "critical"
        ]
        single_check["compliance_blocked"] = bool(compliance_critical)
        single_check["compliance_critical_count"] = len(compliance_critical)

        # ---- 多票分组（如有）----
        groups: List[Dict] = []
        conflicts: List[Dict] = []
        if manifests:
            groups, conflicts = self._group_manifests(manifests)

        result_data = {
            "merge_result": {
                "single_check": single_check,
                "effective_record": {k: effective.get(k) for k, _ in MERGE_KEYS},
                "groups": groups,
                "conflicts": conflicts,
                "group_count": len(groups),
                "conflict_count": len(conflicts),
            }
        }

        # ---- summary ----
        lines = [f"## 归并预检结果"]
        lines.append(f"- **归并要素齐全**：{'✅' if single_check['complete'] else '❌'}")
        if not single_check["complete"]:
            lines.append(f"- **缺失要素**：{', '.join(single_check['missing'])}")
            lines.append("  → 要素不全无法归并，需补全申报要素")
        if single_check.get("compliance_blocked"):
            lines.append(
                f"- **合规阻断**：{single_check['compliance_critical_count']} 项 critical，"
                "未处理前不得归并"
            )

        if manifests:
            lines.append(f"- **清单数**：{len(manifests)}")
            lines.append(f"- **归并组数**：{len(groups)}")
            for i, g in enumerate(groups, 1):
                lines.append(f"  - 组{i}：{g['key'][:50]}...（{g['count']}票）")
            if conflicts:
                lines.append(f"- **冲突**（{len(conflicts)}）：")
                for cf in conflicts:
                    lines.append(f"  - ⚠️ {cf['detail']}")

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(lines),
            data=result_data,
            next_hint=(
                "归并要素齐全，可参与月度汇总" if single_check["complete"]
                else "归并要素不全，需补全后再汇总"
            ),
            confidence=0.9 if single_check["complete"] else 0.6,
            needs_human_review=(
                bool(conflicts)
                or not single_check["complete"]
                or single_check.get("compliance_blocked", False)
            ),
        )

    # ----------------------------------------------------------------- #
    # 上游产物归并（A 归类 / C 合规）
    # ----------------------------------------------------------------- #
    @staticmethod
    def _merge_upstream(order: Dict, ctx: Dict) -> Dict:
        """用 A / C 槽的产出补全归并要素；原始 order 优先，缺失才回填。"""
        rec = dict(order)

        sku = ctx.get("sku_info") or {}
        hs = ctx.get("hs_code") or {}
        if isinstance(hs, str):
            hs = {"code": hs}
        dd = ctx.get("declaration_dict") or {}

        if not rec.get("cargo_name"):
            rec["cargo_name"] = (
                sku.get("cargo_name")
                or dd.get("cargo_common")
                or dd.get("cargo_spec")
            )
        if not rec.get("hs_code"):
            rec["hs_code"] = hs.get("code") or dd.get("hs_code")
        if not rec.get("specification"):
            rec["specification"] = dd.get("model") or dd.get("cargo_spec")
        if not rec.get("unit"):
            rec["unit"] = dd.get("unit")
        if not rec.get("currency"):
            rec["currency"] = dd.get("currency")
        if not rec.get("origin_country"):
            rec["origin_country"] = dd.get("origin_country")
        if not rec.get("dest_country"):
            rec["dest_country"] = dd.get("dest_country")
        if not rec.get("shipper_consignee"):
            shipper = dd.get("shipper")
            consignee = dd.get("consignee")
            if shipper and consignee:
                rec["shipper_consignee"] = f"{shipper}/{consignee}"
            else:
                rec["shipper_consignee"] = shipper or consignee

        return rec

    # ----------------------------------------------------------------- #
    # 单票归并要素检查
    # ----------------------------------------------------------------- #
    @staticmethod
    def _check_single_merge(order: Dict) -> Dict:
        """检查单票清单的八个同一要素是否齐全。"""
        present = []
        missing = []
        for key, cn in MERGE_KEYS:
            val = order.get(key)
            if val:
                present.append(key)
            else:
                missing.append(cn)
        return {
            "complete": len(missing) == 0,
            "present": present,
            "missing": missing,
        }

    # ----------------------------------------------------------------- #
    # 多票分组
    # ----------------------------------------------------------------- #
    def _group_manifests(self, manifests: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """按八个同一分组，检出冲突。"""
        groups_map: Dict[str, List[Dict]] = {}
        conflicts: List[Dict] = []

        for mf in manifests:
            key_parts = []
            for key, _ in MERGE_KEYS:
                key_parts.append(str(mf.get(key, "")))
            key = "|".join(key_parts)
            groups_map.setdefault(key, []).append(mf)

        groups = [
            {"key": k, "count": len(v), "manifests": v}
            for k, v in groups_map.items()
        ]

        # 冲突检测：同一商品名称但不同HS编码
        name_to_hs: Dict[str, set] = {}
        for mf in manifests:
            name = mf.get("cargo_name", "")
            hs = mf.get("hs_code", "")
            if name and hs:
                name_to_hs.setdefault(name, set()).add(hs)
        for name, hss in name_to_hs.items():
            if len(hss) > 1:
                conflicts.append({
                    "type": "hs_inconsistency",
                    "detail": f"商品 '{name}' 被归到不同 HS 编码：{', '.join(hss)}",
                    "severity": "warning",
                })

        return groups, conflicts


if __name__ == "__main__":
    agent = MergeAgent()

    # 测试1：单票要素齐全
    print("=" * 50)
    print("测试1：单票要素齐全")
    res = agent.ask("归并", context={
        "order": {
            "cargo_name": "硅胶手机壳", "hs_code": "3926909090",
            "specification": "iPhone 15", "unit": "千克", "currency": "USD",
            "origin_country": "中国", "dest_country": "美国",
            "shipper_consignee": "深圳XX/John Smith",
        }
    })
    print(res.summary)
    print()

    # 测试2：单票要素不全
    print("=" * 50)
    print("测试2：单票要素不全")
    res2 = agent.ask("归并", context={
        "order": {"cargo_name": "硅胶手机壳", "hs_code": "3926909090"},
    })
    print(res2.summary)
    print()

    # 测试3：多票分组 + 冲突
    print("=" * 50)
    print("测试3：多票分组 + HS冲突")
    manifests = [
        {"cargo_name": "硅胶手机壳", "hs_code": "3926909090", "specification": "iPhone 15",
         "unit": "千克", "currency": "USD", "origin_country": "中国",
         "dest_country": "美国", "shipper_consignee": "深圳XX/John"},
        {"cargo_name": "硅胶手机壳", "hs_code": "3926909090", "specification": "iPhone 15",
         "unit": "千克", "currency": "USD", "origin_country": "中国",
         "dest_country": "美国", "shipper_consignee": "深圳XX/John"},
        {"cargo_name": "硅胶手机壳", "hs_code": "3926909010", "specification": "iPhone 15",
         "unit": "千克", "currency": "USD", "origin_country": "中国",
         "dest_country": "美国", "shipper_consignee": "深圳XX/John"},
    ]
    res3 = agent.ask("归并", context={"order": {}, "manifests": manifests})
    print(res3.summary)
