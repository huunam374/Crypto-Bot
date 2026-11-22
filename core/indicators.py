import logging
from typing import Dict, Optional
import pandas as pd
import pandas_ta as ta

from config import Config

log = logging.getLogger(__name__)

class TechnicalIndicatorCalculator:
    @staticmethod
    def enrich(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ema34"] = ta.ema(out["close"], length=34)
        out["ema89"] = ta.ema(out["close"], length=89)
        out["ema200"] = ta.ema(out["close"], length=200)

        bb = ta.bbands(out["close"], length=20, std=2.0)
        if bb is not None and not bb.empty:
            lower_col = next((c for c in bb.columns if c.upper().startswith("BBL")), bb.columns[0])
            mid_col   = next((c for c in bb.columns if c.upper().startswith("BBM")), bb.columns[1])
            upper_col = next((c for c in bb.columns if c.upper().startswith("BBU")), bb.columns[2])
            out["bb_lower"] = bb[lower_col]
            out["bb_mid"]   = bb[mid_col]
            out["bb_upper"] = bb[upper_col]
            out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"]
            out["bb_width_ma"] = out["bb_width"].rolling(20).mean()

        out["vol_ma20"] = out["volume"].rolling(20).mean()
        out["vol_ratio"] = out["volume"] / out["vol_ma20"].replace(0, 1e-9)
        out["obv"] = ta.obv(out["close"], out["volume"])
        out["vwap"] = ta.vwap(out["high"], out["low"], out["close"], out["volume"])

        hl_range = out["high"] - out["low"]
        out["clv"] = ((out["close"] - out["low"]) - (out["high"] - out["close"])) / hl_range.replace(0, 1e-9)
        out["atr"] = ta.atr(out["high"], out["low"], out["close"], length=14)
        out["rsi"] = ta.rsi(out["close"], length=14)
        out = TechnicalIndicatorCalculator.calculate_cvd(out)
        return out

    @staticmethod
    def get_market_regime(df_htf: pd.DataFrame, cfg: Optional[Config] = None) -> str:
        """Xác định trend bằng VWAP + CVD + Độ dốc BB Mid (Thay thế EMA200)"""
        use_vwap = getattr(cfg, 'use_vwap_trend', True) if cfg else True
        
        if not use_vwap or len(df_htf) < 50:
            return "RANGING"

        last = df_htf.iloc[-1]
        vwap_val = last.get("vwap", 0)
        
        # 1. Điều kiện VWAP
        vwap_bullish = last["close"] > vwap_val if vwap_val > 0 else False
        vwap_bearish = last["close"] < vwap_val if vwap_val > 0 else False

        # 2. Điều kiện CVD (Dòng tiền)
        cvd_slope = 0
        if len(df_htf) >= 20 and "cvd" in df_htf.columns:
            cvd_slope = df_htf["cvd"].iloc[-1] - df_htf["cvd"].iloc[-20]
        cvd_bullish = cvd_slope > 0
        cvd_bearish = cvd_slope < 0

        # 3. Điều kiện Bollinger Mid (Thay vì EMA200)
        bb_mid_slope = 0
        if "bb_mid" in df_htf.columns and len(df_htf) >= 10:
            bb_mid_slope = df_htf["bb_mid"].iloc[-1] - df_htf["bb_mid"].iloc[-10]
        bb_bullish = bb_mid_slope > 0
        bb_bearish = bb_mid_slope < 0

        # Đếm số điều kiện đồng thuận
        bull_count = sum([vwap_bullish, cvd_bullish, bb_bullish])
        bear_count = sum([vwap_bearish, cvd_bearish, bb_bearish])

        if bull_count >= 2: return "UPTREND"
        if bear_count >= 2: return "DOWNTREND"
        return "RANGING"

    @staticmethod
    def get_signal_score(df: pd.DataFrame, strategy: str, direction: str = "BUY", cfg: Optional[Config] = None) -> int:
        if len(df) < 200: return 0
        last = df.iloc[-1]
        prev = df.iloc[-2]

        enable_smc = cfg.enable_smc if cfg else True
        smc_bypass = getattr(cfg, 'smc_bypass_bb_zone', True) if cfg else True
        pullback_vol_min = getattr(cfg, 'pullback_vol_min_ratio', 0.4) if cfg else 0.4

        # ✅ FIX LỖI VÀNG: Khởi tạo tường minh các biến SMC từ đầu
        sweep = "NONE"
        fvg = {"bullish": None, "bearish": None}
        smc_confirms = False

        if enable_smc:
            sweep = TechnicalIndicatorCalculator.check_liquidity_sweep(df, lookback=20)
            fvg = TechnicalIndicatorCalculator.check_fvg(df, lookback=20)
            
            if direction == "BUY" and sweep == "BULLISH": smc_confirms = True
            elif direction == "SELL" and sweep == "BEARISH": smc_confirms = True
            elif direction == "BUY" and fvg["bullish"]:
                fvg_bot, fvg_top = fvg["bullish"]["bottom"], fvg["bullish"]["top"]
                atr = last["atr"] if not pd.isna(last["atr"]) else last["close"] * 0.02
                if fvg_bot <= last["close"] <= fvg_top + (atr * 0.5): smc_confirms = True
            elif direction == "SELL" and fvg["bearish"]:
                fvg_bot, fvg_top = fvg["bearish"]["bottom"], fvg["bearish"]["top"]
                atr = last["atr"] if not pd.isna(last["atr"]) else last["close"] * 0.02
                if fvg_top >= last["close"] >= fvg_bot - (atr * 0.5): smc_confirms = True

        # =====================================================================
        # ✅ 1. BỘ LỌC HÀNH VI GIÁ (ANTI-FOMO & VSA) - MỚI
        # =====================================================================
        body_size = abs(last["close"] - last["open"])
        candle_range = last["high"] - last["low"]
        body_ratio = body_size / candle_range if candle_range > 0 else 0
        
        lower_wick = min(last["open"], last["close"]) - last["low"]
        upper_wick = last["high"] - max(last["open"], last["close"])

        # Chặn bắt dao rơi (Râu quá dài)
        if direction == "SELL" and lower_wick > body_size * 2.0:
            return 0 
        if direction == "BUY" and upper_wick > body_size * 2.0:
            return 0

        # Chặn đỉnh/đáy Climax (Volume cực to nhưng thân ngắn)
        if last["vol_ratio"] > 3.0 and body_ratio < 0.4:
            if direction == "SELL" and last["rsi"] < 30: return 0
            if direction == "BUY" and last["rsi"] > 70: return 0

        # =====================================================================
        # ✅ 2. BỘ LỌC VOLUME CƠ BẢN
        # =====================================================================
        vol_min = pullback_vol_min if strategy == "PULLBACK" else 0.8
        if last["vol_ratio"] < vol_min:
            return 0

        # =====================================================================
        # ✅ 3. BỘ LỌC BOLLINGER CHUYÊN SÂU (SQUEEZE & WALKING BANDS)
        # =====================================================================
        bb_range = last["bb_upper"] - last["bb_lower"]
        if bb_range > 0:
            bb_pos = (last["close"] - last["bb_lower"]) / bb_range
            bb_mid = last["bb_mid"]
            atr = last["atr"] if not pd.isna(last["atr"]) else last["close"] * 0.02
            
            # Chặn FOMO nếu giá quá xa BB Mid (trừ khi là Breakout)
            if abs(last["close"] - bb_mid) > (atr * 3) and strategy != "BREAKOUT":
                return 0

            # Bollinger Walking (Bám dải)
            is_walking_down = (prev["close"] < prev["bb_lower"]) and (last["close"] < last["bb_lower"])
            is_walking_up = (prev["close"] > prev["bb_upper"]) and (last["close"] > last["bb_upper"])
            
            if direction == "SELL" and is_walking_down and last["vol_ratio"] > 1.5:
                pass # Cho phép đánh tiếp sóng thần giảm
            elif direction == "BUY" and is_walking_up and last["vol_ratio"] > 1.5:
                pass # Cho phép đánh tiếp sóng thần tăng
            elif 0.35 <= bb_pos <= 0.65:
                # ✅ SỬA TẠI ĐÂY: Chỉ chặn nếu KHÔNG PHẢI là chiến lược PULLBACK
                # Nếu là PULLBACK, cho phép đi tiếp vào phần chấm điểm
                if strategy != "PULLBACK" and strategy != "BREAKOUT":
                    if not (smc_bypass and smc_confirms):
                        return 0 # Chặn vùng nhiễu giữa BB đối với các chiến lược khác

        # =====================================================================
        # 4. CHẤM ĐIỂM KỸ THUẬT (Tích hợp Squeeze)
        # =====================================================================
        score = 0
        dragon_spread = abs(last["ema34"] - last["ema89"]) / last["close"]

        # Kiểm tra Bollinger Squeeze (Độ rộng BB nhỏ hơn 80% MA20 của nó)
        is_squeeze = last["bb_width"] < last["bb_width_ma"] * 0.8 if "bb_width_ma" in last else False

        if strategy == "BREAKOUT":
            if direction == "BUY":
                if last["ema34"] > last["ema89"]: score += 15
                if dragon_spread > 0.008: score += 15
                if is_squeeze: score += 20 # Điểm cao cho Squeeze
                if last["close"] > last["bb_upper"] and body_ratio > 0.7: score += 20
                if last["vol_ratio"] > 1.5 and body_ratio > 0.6: score += 20 # Effort vs Result
                if last["obv"] > prev["obv"]: score += 10
            else:
                if last["ema34"] < last["ema89"]: score += 15
                if dragon_spread > 0.008: score += 15
                if is_squeeze: score += 20
                if last["close"] < last["bb_lower"] and body_ratio > 0.7: score += 20
                if last["vol_ratio"] > 1.5 and body_ratio > 0.6: score += 20
                if last["obv"] < prev["obv"]: score += 10
                
        elif strategy == "PULLBACK":
            if direction == "BUY":
                if last["ema34"] > last["ema89"]: score += 15
                if lower_wick > body_size * 1.5 and last["close"] > last["open"]: score += 25
                if last["low"] <= last["ema34"] and last["close"] > last["ema34"]: score += 20
                if last["vol_ratio"] < 1.2: score += 15 # Volume cạn kiệt
                if last["rsi"] < 45 and last["rsi"] > prev["rsi"]: score += 15
            else:
                if last["ema34"] < last["ema89"]: score += 15
                if upper_wick > body_size * 1.5 and last["close"] < last["open"]: score += 25
                if last["high"] >= last["ema34"] and last["close"] < last["ema34"]: score += 20
                if last["vol_ratio"] < 1.2: score += 15
                if last["rsi"] > 55 and last["rsi"] < prev["rsi"]: score += 15
                
        elif strategy == "RANGING":
            if direction == "BUY":
                if last["close"] <= last["bb_lower"] * 1.002: score += 30
                if last["rsi"] < 30: score += 20
                if last["clv"] > 0.6 and last["close"] > last["open"]: score += 20
            else:
                if last["close"] >= last["bb_upper"] * 0.998: score += 30
                if last["rsi"] > 70: score += 20
                if last["clv"] < -0.6 and last["close"] < last["open"]: score += 20

        # =====================================================================
        # 5. SMC BONUS (Giữ nguyên)
        # =====================================================================
        if enable_smc:
            sweep_bonus = cfg.smc_sweep_bonus if cfg else 15
            if direction == "BUY" and sweep == "BULLISH": score += sweep_bonus
            elif direction == "SELL" and sweep == "BEARISH": score += sweep_bonus

            fvg_bonus = cfg.smc_fvg_bonus if cfg else 10
            atr = last["atr"] if not pd.isna(last["atr"]) else last["close"] * 0.02
            if direction == "BUY" and fvg["bullish"]:
                fvg_bot, fvg_top = fvg["bullish"]["bottom"], fvg["bullish"]["top"]
                if fvg_bot <= last["close"] <= fvg_top + (atr * 0.5): score += fvg_bonus
            elif direction == "SELL" and fvg["bearish"]:
                fvg_bot, fvg_top = fvg["bearish"]["bottom"], fvg["bearish"]["top"]
                if fvg_top >= last["close"] >= fvg_bot - (atr * 0.5): score += fvg_bonus

        if bb_range > 0:
            bb_pos = (last["close"] - last["bb_lower"]) / bb_range
            if 0.35 <= bb_pos <= 0.65 and strategy != "BREAKOUT" and smc_confirms:
                score -= 10

        return score

    @staticmethod
    def check_fvg(df: pd.DataFrame, lookback: int = 20) -> dict:
        result = {"bullish": None, "bearish": None}
        if len(df) < 3: return result
        
        for i in range(len(df) - lookback - 2, len(df) - 2):
            if i < 0: continue
            high_1 = df["high"].iloc[i]
            low_1 = df["low"].iloc[i]
            high_3 = df["high"].iloc[i+2]
            low_3 = df["low"].iloc[i+2]
            
            if high_1 < low_3:
                result["bullish"] = {"top": low_3, "bottom": high_1}
            if low_1 > high_3:
                result["bearish"] = {"top": low_1, "bottom": high_3}
                
        return result

    @staticmethod
    def check_liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> str:
        if len(df) < lookback + 2: return "NONE"
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        hist = df.iloc[-lookback-2:-2]
        
        recent_low = hist["low"].min()
        recent_high = hist["high"].max()
        
        if (last["low"] < recent_low and last["close"] > recent_low) or \
           (prev["low"] < recent_low and prev["close"] > recent_low):
            return "BULLISH"
            
        if (last["high"] > recent_high and last["close"] < recent_high) or \
           (prev["high"] > recent_high and prev["close"] < recent_high):
            return "BEARISH"
            
        return "NONE"

    @staticmethod
    def calculate_sl_tp(df: pd.DataFrame, entry_price: float, strategy: str,
                        direction: str = "BUY", cfg: Optional[Config] = None) -> Dict[str, float]:
        SL_ATR_MULT = cfg.sl_atr_mult if cfg else 2.5
        SL_MIN_PCT  = cfg.sl_min_percent if cfg else 0.025
        SL_MAX_PCT  = cfg.sl_max_percent if cfg else 0.10
        SKIP_OVER   = cfg.skip_if_sl_over_percent if cfg else 6.0

        last = df.iloc[-1]
        atr = last["atr"] if not pd.isna(last["atr"]) else entry_price * 0.02

        # =====================================================================
        # ✅ 1. TÍNH SL THEO CẤU TRÚC 100 NẾN QUÁ KHỨ (Thay thế Bollinger)
        # =====================================================================
        # Lấy đỉnh/đáy của 100 nến gần nhất để tìm vùng cấu trúc vĩ mô
        recent_high_100 = df["high"].tail(100).max()
        recent_low_100 = df["low"].tail(100).min()
        
        # Lấy đỉnh/đáy của nến tín hiệu (hiện tại)
        sl_struct_last = last["low"] if direction == "BUY" else last["high"]

        if direction == "BUY":
            # So sánh đáy nến hiện tại với đáy 100 nến để chọn cái THẤP HƠN (an toàn hơn)
            sl_base = min(recent_low_100, sl_struct_last)
            # Cộng thêm đệm ATR để tránh quét râu
            sl_price = sl_base - (0.2 * atr)
            
            max_sl = entry_price * (1 - SL_MAX_PCT)
            if sl_price < max_sl: sl_price = max_sl
            min_sl = entry_price * (1 - SL_MIN_PCT)
            if sl_price > min_sl: sl_price = min_sl

            risk = entry_price - sl_price
            if risk <= 0: risk = entry_price * SL_MIN_PCT; sl_price = entry_price - risk

        else: # SELL
            # So sánh đỉnh nến hiện tại với đỉnh 100 nến để chọn cái CAO HƠN (an toàn hơn)
            sl_base = max(recent_high_100, sl_struct_last)
            # Cộng thêm đệm ATR
            sl_price = sl_base + (0.2 * atr)
            
            max_sl = entry_price * (1 + SL_MAX_PCT)
            if sl_price > max_sl: sl_price = max_sl
            min_sl = entry_price * (1 + SL_MIN_PCT)
            if sl_price < min_sl: sl_price = min_sl

            risk = sl_price - entry_price
            if risk <= 0: risk = entry_price * SL_MIN_PCT; sl_price = entry_price + risk

        # =====================================================================
        # ✅ 2. TÍNH TP THEO ĐỈNH/ĐÁY 200 NẾN + ATR (Giữ nguyên phần đã sửa)
        # =====================================================================
        macro_high = df["high"].tail(200).max()
        macro_low = df["low"].tail(200).min()
        atr_safe_distance = atr * 2.5 

        if direction == "BUY":
            tp_macro = macro_high * 0.995
            if (tp_macro - entry_price) < atr_safe_distance:
                tp_price = max(last["bb_upper"] * 0.998, entry_price + atr_safe_distance)
            else:
                tp_price = tp_macro
        else: 
            tp_macro = macro_low * 1.005
            if (entry_price - tp_macro) < atr_safe_distance:
                tp_price = min(last["bb_lower"] * 1.002, entry_price - atr_safe_distance)
            else:
                tp_price = tp_macro

        return {
            "entry": entry_price, "stop_price": sl_price, "stop_limit": sl_price,
            "take_profit": tp_price, "risk_pct": (risk / entry_price) * 100,
            "skip": (risk / entry_price) * 100 > SKIP_OVER, "atr": atr,
        }

    @staticmethod
    def calculate_cvd(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "taker_buy_vol" not in out.columns:
            out["taker_buy_vol"] = 0.0
        out["taker_buy_vol"] = out["taker_buy_vol"].fillna(0.0)
        out["volume"] = out["volume"].fillna(0.0)
        out["delta"] = out["taker_buy_vol"] - (out["volume"] - out["taker_buy_vol"])
        out["cvd"] = out["delta"].fillna(0.0).cumsum()
        return out

    @staticmethod
    def check_divergence(df: pd.DataFrame, lookback: int = 20) -> str:
        if len(df) < lookback: return "NONE"
        recent = df.tail(lookback).copy()
        half = lookback // 2

        h1 = recent.iloc[:half]
        h1_max_p, h1_min_p = h1["high"].max(), h1["low"].min()
        h1_max_c, h1_min_c = h1["cvd"].max(), h1["cvd"].min()

        h2 = recent.iloc[half:]
        h2_max_p, h2_min_p = h2["high"].max(), h2["low"].min()
        h2_max_c, h2_min_c = h2["cvd"].max(), h2["cvd"].min()

        if h2_max_p > h1_max_p and h2_max_c < h1_max_c:
            return "BEARISH"
        if h2_min_p < h1_min_p and h2_min_c > h1_min_c:
            return "BULLISH"
        return "NONE"