import copy
import logging
import math
import time
from typing import Any, Dict

import pandas as pd
from binance.client import Client

from config import Config

log = logging.getLogger(__name__)

class BinanceDataService:
    def __init__(self, cfg: Config):
        self.cfg = copy.deepcopy(cfg)

        self.client = Client(
            self.cfg.api_key or None,
            self.cfg.api_secret or None,
            testnet=self.cfg.testnet,
            requests_params={'timeout': 15}
        )

        # ✅ FIX: Tự động đồng bộ bù thời gian lệch
        try:
            server_time = self.client.get_server_time()
            self.client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)
            log.info(f"Đã đồng bộ thời gian (Offset: {self.client.timestamp_offset}ms)")
        except Exception as e:
            log.warning(f"Không thể đồng bộ thời gian: {e}")

        self.public_client = Client(requests_params={'timeout': 15})
        self._filters_cache: Dict[str, Dict[str, str]] = {}
        self._time_synced = False

        # ✅ KIỂM TRA KẾT NỐI VÀ QUYỀN API NGAY LẬP TỨC
        if self.cfg.market_type == "FUTURE" and self.cfg.api_key:
            try:
                self.client.futures_account()
                log.info("✅ Kết nối API Futures thành công. API Key có đủ quyền.")
            except Exception as e:
                print(f"\n=== LỖI KẾT NỐI API FUTURES: {e} ===\n")
                log.error(f"Lỗi kết nối API: {e}")

    def _ensure_time_synced(self):
        """✅ Lazy sync time — chỉ gọi 1 lần khi thực sự cần đặt lệnh."""
        if self._time_synced:
            return
        try:
            server_time = self.client.get_server_time()
            self.client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)
        except Exception as e:
            log.warning(f"Không thể đồng bộ thời gian: {e}")
        self._time_synced = True
  
    def get_ohlcv(self, symbol: str, interval: str = "1h", limit: int = 200, for_chart: bool = False) -> pd.DataFrame:
        try:
            if for_chart:
                try:
                    if self.cfg.market_type == "FUTURE":
                        raw = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
                    else:
                        raw = self.public_client.get_klines(symbol=symbol, interval=interval, limit=limit)
                except:
                    raw = self.public_client.get_klines(symbol=symbol, interval=interval, limit=limit)
            else:
                if self.cfg.market_type == "FUTURE":
                    raw = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
                else:
                    raw = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        except Exception as e:
            if not for_chart:
                return pd.DataFrame()
            raw = self.public_client.get_klines(symbol=symbol, interval=interval, limit=limit)

        if not raw:
            return pd.DataFrame()

        cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "tb_base", "tb_quote", "ignore"]
        df = pd.DataFrame(raw, columns=cols)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")

        for c in ["open", "high", "low", "close", "volume", "tb_base"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df.set_index("open_time", inplace=True)
        df.rename(columns={"tb_base": "taker_buy_vol"}, inplace=True)

        # ✅ FIX #15: Fill NaN để CVD không bị NaN
        df = df.fillna(0.0)

        return df[["open", "high", "low", "close", "volume", "taker_buy_vol"]]

    def get_current_price(self, symbol: str) -> float:
        try:
            if self.cfg.market_type == "FUTURE":
                return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])
            else:
                return float(self.client.get_symbol_ticker(symbol=symbol)["price"])
        except Exception:
            return float(self.public_client.get_symbol_ticker(symbol=symbol)["price"])

    # ✅ FIX #11: Hàm mới - chỉ lấy số dư KHẢ DỤNG
    def get_free_usdt(self) -> float:
        """Chỉ lấy số dư khả dụng, KHÔNG tính tiền đang lock."""
        try:
            if self.cfg.market_type == "FUTURE":
                bal = self.client.futures_account_balance()
                for asset in bal:
                    if asset['asset'] == 'USDT':
                        return float(asset.get('availableBalance', asset['balance']))
                return 0.0
            else:
                bal = self.client.get_asset_balance(asset="USDT")
                return float(bal["free"]) if bal else 0.0
        except:
            return 0.0

    def get_usdt_balance(self) -> float:
        """Tổng số dư (dùng cho UI hiển thị)."""
        try:
            if self.cfg.market_type == "FUTURE":
                # Dùng futures_account() để lấy số dư Futures
                acc = self.client.futures_account()
                for asset in acc['assets']:
                    if asset['asset'] == 'USDT':
                        return float(asset['availableBalance'])
                return 0.0
            else:
                bal = self.client.get_asset_balance(asset="USDT")
                return float(bal["free"]) + float(bal["locked"]) if bal else 0.0
        except Exception as e:
            # ✅ IN THẲNG LỖI RA MÀN HÌNH ĐEN
            print(f"\n=== LỖI LẤY SỐ DƯ: {e} ===\n") 
            log.error(f"Lỗi lấy số dư API: {e}") 
            return 0.0

    def has_open_position(self, symbol: str) -> bool:
        try:
            if self.cfg.market_type == "FUTURE":
                positions = self.client.futures_position_information(symbol=symbol)
                for pos in positions:
                    # Nếu positionAmt khác 0 (âm hoặc dương) tức là đang có vị thế
                    if abs(float(pos['positionAmt'])) > 0.000001:
                        return True
                return False
            else:
                base = symbol.replace("USDT", "")
                bal = self.client.get_asset_balance(asset=base)
                if bal:
                    total = float(bal["free"]) + float(bal["locked"])
                    return total > 1e-8
                return False
        except Exception as e:
            # ✅ FIX P0: KHÔNG return False khi lỗi API — tránh bot tưởng lệnh đã đóng
            log.warning(f"has_open_position lỗi {symbol}: {e} — assume còn lệnh")
            return True

    def _get_filters(self, symbol: str) -> Dict[str, str]:
        if symbol in self._filters_cache: return self._filters_cache[symbol]

        f = {}
        if self.cfg.market_type == "FUTURE":
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['symbol'] == symbol:
                    f = {flt["filterType"]: flt for flt in s["filters"]}
                    break
        else:
            info = self.client.get_symbol_info(symbol)
            f = {flt["filterType"]: flt for flt in info["filters"]}

        step_size_str = "0.001"
        if "MARKET_LOT_SIZE" in f:
            step_size_str = f["MARKET_LOT_SIZE"]["stepSize"]
        elif "LOT_SIZE" in f:
            step_size_str = f["LOT_SIZE"]["stepSize"]

        res = {
            "step_size_str": step_size_str,
            "tick_size_str": f.get("PRICE_FILTER", {}).get("tickSize", "0.01")
        }
        self._filters_cache[symbol] = res
        return res

    def _format_value(self, value: float, step_str: str) -> str:
        if not step_str or float(step_str) <= 0:
            return str(value)

        if "." in step_str:
            precision = len(step_str.split(".")[1])
        else:
            precision = 0

        factor = 10 ** precision
        truncated = math.floor(value * factor) / factor
        return f"{truncated:.{precision}f}"

    def market_buy_by_quote(self, symbol: str, quote_amount: float, direction: str = "BUY") -> Dict[str, Any]:
        try:
            if self.cfg.market_type == "FUTURE":
                try:
                    self.client.futures_change_margin_type(symbol=symbol, marginType=self.cfg.margin_type)
                except Exception:
                    pass
                try:
                    self.client.futures_change_leverage(symbol=symbol, leverage=self.cfg.leverage)
                except Exception as e:
                    log.warning(f"Không thể set đòn bẩy cho {symbol}: {e}")

                price = self.get_current_price(symbol)
                qty = (quote_amount * self.cfg.leverage) / price
                f = self._get_filters(symbol)
                qty_str = self._format_value(qty, f["step_size_str"])

                if float(qty_str) == 0:
                    log.warning(f"[{symbol}] Số lượng làm tròn = 0. Không thể mua.")
                    return {}

                side = "BUY" if direction == "BUY" else "SELL"
                log.info(f"[{symbol}] Gửi lệnh {side} Futures: Qty = {qty_str}")
                return self.client.futures_create_order(
                    symbol=symbol, side=side, type="MARKET", quantity=qty_str
                )
            else:
                if direction == "BUY":
                    return self.client.order_market_buy(symbol=symbol, quoteOrderQty=quote_amount)
                return {}
        except Exception as e:
            log.error(f"Lỗi mua/bán {symbol}: {e}")
            return {}

    def market_sell(self, symbol: str, quantity: float) -> Dict[str, Any]:
        try:
            f = self._get_filters(symbol)
            qty_str = self._format_value(quantity, f["step_size_str"])
            if self.cfg.market_type == "FUTURE":
                return self.client.futures_create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty_str)
            else:
                return self.client.order_market_sell(symbol=symbol, quantity=qty_str)
        except Exception as e:
            log.error(f"Lỗi bán {symbol}: {e}")
            return {}

    def place_oco_sell(self, symbol: str, quantity: float, take_profit_price: float,
                       stop_price: float, stop_limit_price: float, direction: str = "BUY"):
        try:
            self._ensure_time_synced()
            f = self._get_filters(symbol)
            tp_str = self._format_value(take_profit_price, f["tick_size_str"])
            sp_str = self._format_value(stop_price, f["tick_size_str"])

            if self.cfg.market_type == "FUTURE":
                close_side = "SELL" if direction == "BUY" else "BUY"
                qty_str = self._format_value(quantity, f["step_size_str"])

                ok_tp = ok_sl = False
                tp_err = sl_err = ""

                try:
                    self.client.futures_create_order(
                        symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
                        stopPrice=tp_str, quantity=qty_str, reduceOnly=True, workingType="MARK_PRICE"
                    )
                    ok_tp = True
                except Exception as e:
                    tp_err = str(e)
                    log.error(f"[{symbol}] TP fail: {e}")

                try:
                    self.client.futures_create_order(
                        symbol=symbol, side=close_side, type="STOP_MARKET",
                        stopPrice=sp_str, quantity=qty_str, reduceOnly=True, workingType="MARK_PRICE"
                    )
                    ok_sl = True
                except Exception as e:
                    sl_err = str(e)
                    log.error(f"[{symbol}] SL fail: {e}")

                # ✅ FIX P1: Nếu thiếu 1 trong 2 → raise để _place_oco_with_retry retry
                if not (ok_tp and ok_sl):
                    # Cố gắng hủy lệnh đã đặt (tránh để 1 lệnh mồ côi)
                    try:
                        self.client.futures_cancel_all_open_orders(symbol=symbol)
                    except Exception:
                        pass
                    raise RuntimeError(f"OCO không đầy đủ (TP={ok_tp} {tp_err} | SL={ok_sl} {sl_err})")

                return {"msg": "Đã đặt TP/SL Future"}
            else:
                qty_str = self._format_value(quantity, f["step_size_str"])
                stop_limit_adj = stop_price * 0.998
                stop_limit_str = self._format_value(stop_limit_adj, f["tick_size_str"])
                return self.client.create_oco_order(
                    symbol=symbol, side="SELL", quantity=qty_str,
                    price=tp_str, stopPrice=sp_str,
                    stopLimitPrice=stop_limit_str, stopLimitTimeInForce="GTC"
                )
        except Exception as e:
            log.error(f"Lỗi đặt TP/SL {symbol}: {e}")
            raise   # ✅ FIX: raise để caller biết thất bại

    def place_partial_tp_futures(self, symbol: str, quantity: float,
                                tp1_price: float, tp2_price: float,
                                sl_price: float, direction: str = "BUY",
                                split_ratio: float = 0.5) -> Dict[str, Any]:
        """
        Đặt 2 TP + 1 SL cho Futures (chia nhỏ TP, SL giữ nguyên 100%).
        - TP1: chốt split_ratio (vd 50%) ở giá tp1_price
        - TP2: chốt phần còn lại ở giá tp2_price
        - SL: 1 lệnh duy nhất bảo vệ 100% qty (reduceOnly tự động giảm khi TP khớp)
        """
        try:
            f = self._get_filters(symbol)
            close_side = "SELL" if direction == "BUY" else "BUY"

            # Tính qty cho từng phần TP
            qty_tp1 = quantity * split_ratio
            qty_tp2 = quantity - qty_tp1  # tránh sai số làm tròn

            qty_tp1_str = self._format_value(qty_tp1, f["step_size_str"])
            qty_tp2_str = self._format_value(qty_tp2, f["step_size_str"])
            qty_full_str = self._format_value(quantity, f["step_size_str"])  # ✅ SL dùng full qty

            # Kiểm tra min qty sau khi chia
            if float(qty_tp1_str) <= 0 or float(qty_tp2_str) <= 0:
                log.warning(f"[{symbol}] Qty sau khi chia = 0. Không thể đặt partial TP.")
                return {}

            tp1_str = self._format_value(tp1_price, f["tick_size_str"])
            tp2_str = self._format_value(tp2_price, f["tick_size_str"])
            sl_str  = self._format_value(sl_price, f["tick_size_str"])

            results = []

            # ✅ TP1 (50%)
            if tp1_price > 0:
                try:
                    r1 = self.client.futures_create_order(
                        symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
                        stopPrice=tp1_str, quantity=qty_tp1_str,
                        reduceOnly=True, workingType="MARK_PRICE"
                    )
                    results.append(("TP1", r1))
                except Exception as e:
                    log.error(f"[{symbol}] Lỗi đặt TP1: {e}")

            # ✅ TP2 (50% còn lại)
            try:
                r2 = self.client.futures_create_order(
                    symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
                    stopPrice=tp2_str, quantity=qty_tp2_str,
                    reduceOnly=True, workingType="MARK_PRICE"
                )
                results.append(("TP2", r2))
            except Exception as e:
                log.error(f"[{symbol}] Lỗi đặt TP2: {e}")

            # ✅ SL (1 lệnh duy nhất, full qty - reduceOnly tự động giảm khi TP khớp)
            try:
                r3 = self.client.futures_create_order(
                    symbol=symbol, side=close_side, type="STOP_MARKET",
                    stopPrice=sl_str, quantity=qty_full_str,
                    reduceOnly=True, workingType="MARK_PRICE"
                )
                results.append(("SL", r3))
            except Exception as e:
                log.error(f"[{symbol}] Lỗi đặt SL: {e}")

            return {"msg": f"Đã đặt {len(results)}/3 lệnh (2 TP + 1 SL)", "orders": results}

        except Exception as e:
            log.error(f"Lỗi đặt partial TP {symbol}: {e}")
            return {}

    def get_position_pnl(self, symbol: str, entry_price: float, qty: float, direction: str):
        try:
            if self.cfg.market_type == "FUTURE":
                positions = self.client.futures_position_information(symbol=symbol)
                for pos in positions:
                    if pos['symbol'] == symbol and float(pos['positionAmt']) != 0:
                        pnl_usdt = float(pos['unRealizedProfit'])
                        margin = float(pos['isolatedWallet']) if float(pos['isolatedWallet']) > 0 else float(pos['notional']) / self.cfg.leverage
                        pnl_pct = (pnl_usdt / margin) * 100 if margin > 0 else 0
                        return {"pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct}
            else:
                current_price = self.get_current_price(symbol)
                if direction == "BUY":
                    pnl_usdt = (current_price - entry_price) * qty
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    pnl_usdt = (entry_price - current_price) * qty
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100
                return {"pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct}
        except Exception as e:
            log.error(f"Lỗi lấy PnL {symbol}: {e}")
        return {"pnl_usdt": 0, "pnl_pct": 0}

    def cancel_open_orders(self, symbol: str):
        try:
            if self.cfg.market_type == "FUTURE":
                orders = self.client.futures_get_open_orders(symbol=symbol)
                if orders:
                    self.client.futures_cancel_all_open_orders(symbol=symbol)
                    # ✅ FIX: giảm sleep 0.2→0.1
                    time.sleep(0.1)
            else:
                open_orders = self.client.get_open_orders(symbol=symbol)
                for order in open_orders:
                    self.client.cancel_order(symbol=symbol, orderId=order['orderId'])
        except Exception as e:
            log.error(f"Lỗi hủy lệnh {symbol}: {e}")


