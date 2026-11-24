import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import ClassVar, Optional

log = logging.getLogger(__name__)

@dataclass
class Config:
    # --- API Binance ---
    api_key: str = ""
    api_secret: str = ""
    api_key_future: str = ""
    api_secret_future: str = ""
    testnet: bool = True

    # --- Giao dịch ---
    market_type: str = "SPOT"
    leverage: int = 3
    margin_type: str = "ISOLATED"
    interval: str = "1h"

    # --- Chế độ & Rủi ro ---
    trade_mode: str = "FIXED"
    fixed_amount: float = 20.0
    max_risk_percent: float = 5.0
    max_open_positions: int = 5
    max_allocation_percent: float = 50.0
    # ✅ FIX: hạ default về 60 — trước đây 70 khiến PULLBACK (60+) / RANGING (65+) bị vô hiệu
    min_signal_score: int = 60
    max_per_trade: float = 100.0

    # --- Vòng lặp ---
    scan_interval_sec: int = 15
    auto_scan_interval_min: int = 15
    cvd_scan_interval_min: int = 10
    auto_sync_spot: bool = False

    # ✅ SMC
    enable_smc: bool = True
    smc_sweep_bonus: int = 15
    smc_fvg_bonus: int = 10

    # =========================================================
    # ✅ QUẢN LÝ SL/TP — CHỈ DÙNG CỨNG (OCO TRÊN SÀN)
    # =========================================================
    # SL
    sl_atr_mult: float = 2.5
    sl_min_percent: float = 0.02
    sl_max_percent: float = 0.4
    skip_if_sl_over_percent: float = 8.0

    # TP cứng (đặt OCO trên sàn)
    min_rr: float = 2.0               # RR tối thiểu — tính toán + có thể skip lệnh

    # ✅ MỚI: VWAP TREND & SOFT FILTERS
    use_vwap_trend: bool = True       # Bật xác định trend bằng VWAP + CVD (Thay thế EMA200)
    smc_bypass_bb_zone: bool = True   # Cho phép SMC (sweep/FVG) bypass vùng giữa BB
    pullback_vol_min_ratio: float = 0.4  # Ngưỡng volume tối thiểu cho PULLBACK (vol cạn)

    # ✅ MỚI: CẤU HÌNH TRAILING MỀM (SOFT TRAILING)
    soft_trailing_activation_rr: float = 1.0  # Bắt đầu trailing khi lời 1.0 R
    soft_trailing_drop_pct: float = 0.02     # Rớt 1.5% từ đỉnh/đáy thì cắt

    CONFIG_FILE: ClassVar[Path] = Path("bot_config.json")

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or cls.CONFIG_FILE
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**valid)
            except Exception as e:
                log.warning("Không đọc được config: %s", e)
        return cls()

    def save(self, path: Optional[Path] = None) -> None:
        path = path or self.CONFIG_FILE
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)