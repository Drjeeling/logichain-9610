"""季节系数加载器：根据月份与运输方式计算最终运价倍数 + 中转环节系数。

改造后支持：
1. 月份 × 运输方式 二维表（铁路更稳、公路波动更大）
2. 中转环节季节系数（旺季港口更贵更慢）
3. 查验额外等待期望值（按月份概率叠加）
4. 航空运输季节系数
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class SeasonalConfig:
    # 运价：月份 × 运输方式 二维表
    month_mode_factor: Dict[int, Dict[str, float]]
    # 兼容旧字段（v1 单一月份系数 + 固定方式折扣）
    monthly_factor: Dict[int, float]
    mode_multiplier: Dict[str, float]

    # 中转季节系数
    month_transfer_factor: Dict[int, Dict[str, float]]
    air_transfer_seasonal: Dict[int, float]

    # 查验概率（按月份）
    inspection_probability: Dict[int, float]
    inspection_normal_probability: float
    inspection_base_hours_addon: float

    # 风险准备金
    risk_rate_normal: float
    risk_rate_peak: float
    peak_months: list

    @classmethod
    def from_json(
        cls,
        seasonal_path: Optional[Path] = None,
        transfer_seasonal_path: Optional[Path] = None,
    ) -> "SeasonalConfig":
        seasonal_path = seasonal_path or (DATA_DIR / "seasonal.json")
        transfer_seasonal_path = transfer_seasonal_path or (DATA_DIR / "transfer_seasonal.json")
        with open(seasonal_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        with open(transfer_seasonal_path, "r", encoding="utf-8") as f:
            t_raw = json.load(f)

        # 新版二维表优先，旧版兼容
        if "month_mode_factor" in raw:
            mmf = {int(k): v for k, v in raw["month_mode_factor"].items()}
        else:
            # 兼容 v1：monthly_factor × mode_multiplier 拼二维表
            mf = {int(k): float(v) for k, v in raw["monthly_factor"].items()}
            mml = raw.get("mode_multiplier", {"rail": 0.85, "river": 0.80, "truck": 1.00})
            mmf = {
                m: {mode: m_factor * mode_mult
                     for mode, mode_mult in mml.items()}
                for m, m_factor in mf.items()
            }

        # 中转系数表
        mtf = {int(k): v for k, v in t_raw.get("month_transfer_factor", {}).items()}
        # 跳过 _note 等元字段
        ats = {
            int(k): float(v)
            for k, v in t_raw.get("air_transfer_seasonal", {}).items()
            if not k.startswith("_")
        }
        peak_months = t_raw.get("peak_months", [9, 10, 11, 12, 1])

        # 查验概率从 transfer_baseline.json 读
        baseline_path = DATA_DIR / "transfer_baseline.json"
        try:
            with open(baseline_path, "r", encoding="utf-8") as f:
                b = json.load(f)
            insp = b.get("inspection_surcharge", {})
            ip = {int(k): float(v) for k, v in insp.get("probability_by_month", {}).items()}
            inp = float(insp.get("normal_probability", 0.05))
            ibh = float(insp.get("base_hours_addon", 24))
            rr = b.get("risk_reserve", {})
            rnorm = float(rr.get("rate_normal", 0.03))
            rpeak = float(rr.get("rate_peak_season", 0.05))
        except Exception:
            ip, inp, ibh, rnorm, rpeak = {}, 0.05, 24.0, 0.03, 0.05

        return cls(
            month_mode_factor=mmf,
            monthly_factor={int(k): float(v) for k, v in raw.get("monthly_factor", {}).items()},
            mode_multiplier=raw.get("mode_multiplier", {}),
            month_transfer_factor=mtf,
            air_transfer_seasonal=ats,
            inspection_probability=ip,
            inspection_normal_probability=inp,
            inspection_base_hours_addon=ibh,
            risk_rate_normal=rnorm,
            risk_rate_peak=rpeak,
            peak_months=peak_months,
        )

    def factor(self, month: int, mode: str) -> float:
        """返回 (月份, 运输方式) 对应的最终运价倍数。"""
        mode_table = self.monthly_factor  # 兼容旧表：单一月份系数
        if self.month_mode_factor and month in self.month_mode_factor:
            mt = self.month_mode_factor[month]
            return mt.get(mode, 1.0)
        # 兜底：月份系数 × 方式系数
        m_w = self.monthly_factor.get(month, 1.0)
        mode_w = self.mode_multiplier.get(mode, 1.0)
        return m_w * mode_w

    def adjust_price(self, base_price: float, month: int, mode: str) -> float:
        """对基准运价应用季节系数。"""
        return base_price * self.factor(month, mode)

    # ----------------------------------------------------------------- #
    # 中转环节系数（新增）
    # ----------------------------------------------------------------- #
    def transfer_factor(self, month: int) -> Tuple[float, float, str]:
        """返回 (cost_mult, hours_mult, note) 三元组。"""
        if month in self.month_transfer_factor:
            t = self.month_transfer_factor[month]
            return float(t["cost_mult"]), float(t["hours_mult"]), t.get("note", "")
        return 1.0, 1.0, ""

    def air_transfer_factor(self, month: int) -> float:
        """航空中转环节系数。"""
        return self.air_transfer_seasonal.get(month, 1.0)

    def inspection_expected_hours(self, month: int) -> float:
        """查验额外等待期望值 = base_hours × 当月概率。"""
        prob = self.inspection_probability.get(month, self.inspection_normal_probability)
        return self.inspection_base_hours_addon * prob

    def is_peak_season(self, month: int) -> bool:
        """是否旺季（用于风险准备金比例）。"""
        return month in self.peak_months

    def risk_reserve_rate(self, month: int) -> float:
        """风险准备金比例：旺季 5%，淡季 3%。"""
        return self.risk_rate_peak if self.is_peak_season(month) else self.risk_rate_normal
