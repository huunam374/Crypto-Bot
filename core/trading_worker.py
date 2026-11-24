import logging
import pandas as pd
import time
import traceback
import threading
from typing import Dict, Optional
import concurrent.futures
import copy
from PyQt6.QtCore import QThread, pyqtSignal

from config import Config
from services.binance_service import BinanceDataService
from core.indicators import TechnicalIndicatorCalculator

log = logging.getLogger(__name__)


class CoinScannerThread(QThread):
    log_signal = pyqtSignal(str, str)
    result_signal = pyqtSignal(list)

    def __init__(self, cfg: Config, is_auto: bool = False, market_type: str = "SPOT"):
        super().__init__()
        self.cfg = copy.deepcopy(cfg)
        self.cfg.market_type = market_type
        self.binance = BinanceDataService(self.cfg)
        self.is_auto = is_auto

    def _scan_single_coin(self, symbol: str, htf_interval: str):
        try:
            time.sleep(0.05)   # ✅ FIX: chống rate-limit
            use_chart_data = not getattr(self, 'is_auto', False)

            df_htf = self.binance.get_ohlcv(symbol, htf_interval, 250, for_chart=use_chart_data)
            if df_htf.empty or len(df_htf) < 200:
                return None

            df_htf = df_htf.iloc[:-1]
            df_htf = TechnicalIndicatorCalculator.enrich(df_htf)
            regime = TechnicalIndicatorCalculator.get_market_regime(df_htf, cfg=self.cfg)

            if self.cfg.market_type == "SPOT" and regime == "DOWNTREND":
                return None

            df = self.binance.get_ohlcv(symbol, self.cfg.interval, 250, for_chart=use_chart_data)
            if df.empty or len(df) < 201:
                return None

            df = df.iloc[:-1]
            df = TechnicalIndicatorCalculator.enrich(df)

            best_score = 0
            best_dir = ""
            best_strategy = ""

            # ✅ FIX P1: Dùng cfg.min_signal_score thay vì hardcode
            base_min = self.cfg.min_signal_score

            if regime == "UPTREND":
                sc_bo = TechnicalIndicatorCalculator.get_signal_score(df, "BREAKOUT", "BUY", self.cfg)
                sc_pb = TechnicalIndicatorCalculator.get_signal_score(df, "PULLBACK", "BUY", self.cfg)
                if sc_bo >= max(base_min + 10, 70):
                    best_score, best_dir, best_strategy = sc_bo, "BUY", "BREAKOUT"
                elif sc_pb >= base_min:
                    best_score, best_dir, best_strategy = sc_pb, "BUY", "PULLBACK"

            elif regime == "DOWNTREND" and self.cfg.market_type == "FUTURE":
                sc_bd = TechnicalIndicatorCalculator.get_signal_score(df, "BREAKOUT", "SELL", self.cfg)
                sc_pb = TechnicalIndicatorCalculator.get_signal_score(df, "PULLBACK", "SELL", self.cfg)
                if sc_bd >= max(base_min + 10, 70):
                    best_score, best_dir, best_strategy = sc_bd, "SELL", "BREAKDOWN"
                elif sc_pb >= base_min:
                    best_score, best_dir, best_strategy = sc_pb, "SELL", "PULLBACK"

            elif regime == "RANGING":
                sc_buy = TechnicalIndicatorCalculator.get_signal_score(df, "RANGING", "BUY", self.cfg)
                if sc_buy >= base_min + 5:
                    best_score, best_dir, best_strategy = sc_buy, "BUY", "RANGING"
                if self.cfg.market_type == "FUTURE":
                    sc_sell = TechnicalIndicatorCalculator.get_signal_score(df, "RANGING", "SELL", self.cfg)
                    if sc_sell >= base_min + 5 and sc_sell > best_score:
                        best_score, best_dir, best_strategy = sc_sell, "SELL", "RANGING"

            if best_score >= base_min:
                return (symbol, best_score, best_dir, best_strategy)
            return None
        except Exception as e:
            self.log_signal.emit("WARN", f"Bỏ qua {symbol}: {e}")
            return None

    def run(self):
        self.log_signal.emit("INFO", f"Đang lấy danh sách coin trên sàn {self.cfg.market_type}...")
        try:
            if self.cfg.market_type == "FUTURE":
                info = self.binance.client.futures_exchange_info()
            else:
                info = self.binance.client.get_exchange_info()

            blacklist = ['USDC', 'FDUSD', 'TUSD', 'BUSD', 'DAI', 'EUR', 'USDP', 'XAUT', 'PAXG']
            bad_suffixes = ['UPUSDT', 'DOWNUSDT', 'BULLUSDT', 'BEARUSDT']
            valid_symbols = []
            for s in info['symbols']:
                if s['status'] == 'TRADING' and s['symbol'].endswith('USDT'):
                    base_asset = s.get('baseAsset', '')
                    if any(stable in base_asset for stable in blacklist):
                        continue
                    if s['symbol'] in bad_suffixes:
                        continue
                    if not base_asset.isascii() or not base_asset.isalnum():
                        continue
                    valid_symbols.append(s['symbol'])

            # ✅ Lấy ticker 1 lần, dùng cho cả lọc volume và sort
            try:
                tickers = self.binance.public_client.get_ticker()
                ticker_dict = {t['symbol']: float(t['quoteVolume'])
                               for t in tickers if t['symbol'].endswith('USDT')}
            except Exception as e:
                self.log_signal.emit("WARN", f"Không lấy được ticker, dùng danh sách mặc định: {e}")
                ticker_dict = {}

            # Lọc volume > 5M
            if ticker_dict:
                valid_symbols = [s for s in valid_symbols if ticker_dict.get(s, 0) > 5_000_000]
                valid_symbols.sort(key=lambda sym: ticker_dict.get(sym, 0), reverse=True)

            # ✅ FIX P0: Giới hạn top 80 coin thanh khoản nhất
            # Trước đây quét ~300-400 coin × 2 request = 600-800 request → Binance rate-limit
            # → request treo tới timeout 30s → log spam → UI giật.
            MAX_SCAN = 80
            top_symbols = valid_symbols[:MAX_SCAN]

            if not top_symbols:
                self.log_signal.emit("WARN", "Không có coin nào đủ thanh khoản để quét.")
                self.result_signal.emit([])
                return

            self.log_signal.emit(
                "INFO",
                f"Bắt đầu quét ĐA LUỒNG {len(top_symbols)} coin thanh khoản nhất "
                f"(trong tổng {len(valid_symbols)} coin đủ điều kiện)..."
            )

            htf_map = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1w"}
            htf_interval = htf_map.get(self.cfg.interval, "4h")

            scanned_coins = []

            # ✅ FIX P0: giảm workers 5→4 và sleep 0.05s trong _scan_single_coin
            # (bạn đã sửa _scan_single_coin thêm time.sleep(0.05) ở bước trước)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(self._scan_single_coin, symbol, htf_interval)
                           for symbol in top_symbols]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                        if result:
                            scanned_coins.append(result)
                    except Exception as e:
                        # Không để 1 coin lỗi làm hỏng cả vòng quét
                        self.log_signal.emit("WARN", f"Lỗi 1 coin khi quét: {e}")

            scanned_coins.sort(key=lambda x: x[1], reverse=True)
            top_coins = scanned_coins[:15]

            potential = [f"{sym} ({strat} - {dir}: {sc}/100)" for sym, sc, dir, strat in top_coins]

            if not potential:
                self.log_signal.emit("WARN", "Không có coin nào đạt đủ điểm và đúng chiến lược.")
            else:
                self.log_signal.emit("SUCCESS", f"Tìm thấy {len(potential)} coin tiềm năng!")

            self.result_signal.emit(potential)

        except Exception as e:
            self.log_signal.emit("ERROR", f"Lỗi quét coin: {e}")
            self.result_signal.emit([])

class TradingWorker(QThread):
    log_signal        = pyqtSignal(str, str)
    balance_signal    = pyqtSignal(float)
    trade_signal      = pyqtSignal(str, str, dict)
    update_row_signal = pyqtSignal(str, str, dict)
    trade_closed_signal = pyqtSignal(str, str, dict)
    remove_coin_signal = pyqtSignal(str, str)

    def __init__(self, cfg: Config, market_type: str):
        super().__init__()
        self.cfg = copy.deepcopy(cfg)
        self.cfg.market_type = market_type
        self.binance = BinanceDataService(self.cfg)
        self._stop_flag = False
        self.watchlist = {}
        self._lock = threading.Lock()
        self.last_qty_sync_time = 0.0

    def add_coin(self, symbol: str, interval: str, manual_amount: float = None):
        with self._lock:
            if symbol not in self.watchlist:
                self.watchlist[symbol] = {
                    "status": "Chờ duyệt", "auto_active": False,
                    "entry": 0, "tp": 0, "sl": 0, "direction": "",
                    "interval": interval,
                    "manual_amount": manual_amount
                }
                self.log_signal.emit("INFO", f"[{self.cfg.market_type}] Đã thêm {symbol} (Khung {interval}) vào danh sách.")

    def toggle_auto(self, symbol: str, is_active: bool):
        with self._lock:
            if symbol in self.watchlist:
                self.watchlist[symbol]["auto_active"] = is_active
                status = "Đang tự động Trade" if is_active else "Đã tắt Auto Trade"
                self.watchlist[symbol]["status"] = status
                self.log_signal.emit("INFO", f"[{self.cfg.market_type}] {symbol}: {status}")

    def update_coin_amount(self, symbol: str, amount: float):
        with self._lock:
            if symbol in self.watchlist:
                self.watchlist[symbol]["manual_amount"] = amount
                self.log_signal.emit("INFO", f"[{self.cfg.market_type}] Đã cập nhật vốn cho {symbol}: {amount:.2f} USDT")

    def remove_coin(self, symbol: str):
        with self._lock:
            if symbol in self.watchlist:
                del self.watchlist[symbol]

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        self.log_signal.emit("INFO", f"[{self.cfg.market_type}] Bot khởi động | Testnet={self.cfg.testnet}")

        self._sync_positions_with_exchange()
        last_sync_time = time.time()

        while not self._stop_flag:
            try:
                self._scan_once()

                if time.time() - last_sync_time > 60:
                    self.log_signal.emit("INFO", f"[{self.cfg.market_type}] Đang tự động đồng bộ lệnh thủ công từ sàn...")
                    self._sync_positions_with_exchange()
                    last_sync_time = time.time()

            except Exception:
                log.error(traceback.format_exc())

            for _ in range(self.cfg.scan_interval_sec):
                if self._stop_flag: break
                time.sleep(1)

    def _sync_positions_with_exchange(self):
        self.log_signal.emit("INFO", f"[{self.cfg.market_type}] Đang quét tìm lệnh thủ công trên sàn...")
        try:
            if self.cfg.market_type == "FUTURE":
                self._sync_future_positions()
            else:
                if getattr(self.cfg, 'auto_sync_spot', False):
                    self._sync_spot_positions()
                else:
                    self.log_signal.emit("INFO", "[SPOT] Auto-sync Spot đang TẮT (bật trong config nếu muốn).")
        except Exception as e:
            self.log_signal.emit("WARN", f"[{self.cfg.market_type}] Không thể đồng bộ: {e}")

    def _sync_future_positions(self):
        try:
            positions = self.binance.client.futures_position_information()
            active_positions = [p for p in positions if abs(float(p['positionAmt'])) > 0.000001]
            self.log_signal.emit("INFO", f"Tìm thấy {len(active_positions)} lệnh đang mở trên sàn.")
        except Exception as e:
            self.log_signal.emit("ERROR", f"Lỗi kết nối Binance: {e}")
            return

        for pos in active_positions:
            try:
                symbol = pos['symbol']
                amt = float(pos['positionAmt'])
                entry_price = float(pos['entryPrice'])
                direction = "BUY" if amt > 0 else "SELL"
                qty_abs = abs(amt)

                with self._lock:
                    if symbol not in self.watchlist:
                        self.watchlist[symbol] = {
                            "status": "Đang giữ lệnh (Sync)", "auto_active": True,
                            "entry": entry_price, "tp": 0, "sl": 0,
                            "direction": direction, "interval": self.cfg.interval,
                            "manual_amount": None, "qty_bought": qty_abs,
                        }

                    # ✅ QUÉT TẤT CẢ CÁC LỆNH MỀM (OPEN ORDERS)
                    sl_val, tp_val = 0.0, 0.0
                    try:
                        orders = self.binance.client.futures_get_open_orders(symbol=symbol)
                        for o in orders:
                            close_side = "SELL" if direction == "BUY" else "BUY"
                            if o.get("side") == close_side:
                                # Lấy giá của lệnh (dùng stopPrice hoặc price)
                                o_price = float(o.get("stopPrice", 0) or o.get("price", 0) or 0)
                                if o_price > 0:
                                    # Phân biệt SL/TP dựa vào vị trí so với Entry
                                    if direction == "BUY":
                                        if o_price > entry_price: tp_val = o_price
                                        else: sl_val = o_price
                                    else: # SELL
                                        if o_price < entry_price: tp_val = o_price
                                        else: sl_val = o_price
                    except Exception as e:
                        self.log_signal.emit("WARN", f"Lỗi lấy Open Orders {symbol}: {e}")

                    self.watchlist[symbol]["sl"] = sl_val
                    self.watchlist[symbol]["tp"] = tp_val

                    # ✅ FIX TREO APP: KHÔNG TỰ ĐỘNG ĐẶT SL/TP BÙ KHI NHẬN NUÔI LỆNH CŨ
                    if sl_val == 0 and tp_val == 0:
                        self.log_signal.emit("WARN", f"[FUTURE] {symbol} chưa có SL/TP. Vui lòng tự đặt thủ công trên Binance.")
                        # Đã xóa hoàn toàn lệnh gọi _place_missing_sl_tp ở đây để tránh treo App

                    self.log_signal.emit("SUCCESS", f"[FUTURE] Đã nhận nuôi {symbol} (SL: {sl_val}, TP: {tp_val}).")
                    
                    # ✅ CẬP NHẬT LÊN UI
                    self.update_row_signal.emit(symbol, "Đang giữ lệnh", {
                        "entry": entry_price, 
                        "direction": direction,
                        "tp": tp_val, 
                        "sl": sl_val,
                        "interval": self.cfg.interval,
                    })
            except Exception as inner_e:
                self.log_signal.emit("WARN", f"Bỏ qua {pos.get('symbol', 'Unknown')}: {inner_e}")
                continue

        try:
            all_orders = self.binance.client.futures_get_open_orders()
            active_symbols = {p['symbol'] for p in active_positions}
            orphan_orders = [o for o in all_orders if o['symbol'] not in active_symbols]
            
            for o in orphan_orders:
                sym = o['symbol']
                try:
                    self.binance.client.futures_cancel_order(symbol=sym, orderId=o['orderId'])
                    self.log_signal.emit("INFO", f"[Dọn rác] Đã hủy lệnh mồ côi {sym} (orderId={o['orderId']})")
                except Exception as e:
                    self.log_signal.emit("WARN", f"Không hủy được lệnh mồ côi {sym}: {e}")
        except Exception as e:
            self.log_signal.emit("WARN", f"Lỗi dọn lệnh mồ côi: {e}")
        # ✅ KẾT THÚC ĐOẠN CODE MỚI


    def _place_missing_sl_tp(self, symbol: str, entry_price: float, direction: str, qty_abs: float):
        """Chỉ dùng 1 lần khi sync thấy lệnh mở mà trên sàn ko có SL/TP"""
        try:
            tp_val, sl_val = self._calc_safe_sl_tp(symbol, entry_price, direction)
            if tp_val > 0 and sl_val > 0:
                self.binance.place_oco_sell(symbol, qty_abs, tp_val, sl_val, sl_val, direction)
                with self._lock:
                    self.watchlist[symbol]["tp"] = tp_val
                    self.watchlist[symbol]["sl"] = sl_val
                self.log_signal.emit("SUCCESS", f"[FUTURE] Đã đặt bù SL/TP cho {symbol}.")
        except Exception as e:
            self.log_signal.emit("ERROR", f"Không đặt được SL/TP bù cho {symbol}: {e}")

    def _sync_spot_positions(self):
        account = self.binance.client.get_account()
        for bal in account['balances']:
            asset = bal['asset']
            if asset == 'USDT':
                continue
            total = float(bal['free']) + float(bal['locked'])
            if total <= 0:
                continue
            symbol = asset + 'USDT'
            try:
                price = self.binance.get_current_price(symbol)
            except Exception:
                continue
            value_usdt = total * price
            if value_usdt < 5.0:
                continue

            # ✅ FIX P1: Bỏ qua coin rác — volume 24h < 20M USDT
            try:
                tk = self.binance.public_client.get_ticker(symbol=symbol)
                if float(tk.get('quoteVolume', 0)) < 20_000_000:
                    continue
            except Exception:
                continue

            with self._lock:
                tp_val, sl_val = self._calc_safe_sl_tp(symbol, price, "BUY")
                original_risk_val = abs(price - sl_val) if sl_val > 0 else 0
                atr_val = price * 0.02
                try:
                    df = self.binance.get_ohlcv(symbol, self.cfg.interval, 250)
                    if not df.empty and len(df) >= 200:
                        df = df.iloc[:-1]
                        df = TechnicalIndicatorCalculator.enrich(df)
                        last_atr = df["atr"].iloc[-1]
                        if not pd.isna(last_atr) and last_atr > 0:
                            atr_val = last_atr
                except Exception:
                    pass

                if symbol not in self.watchlist:
                    self.watchlist[symbol] = {
                        "status": "Đang giữ lệnh (Sync)", "auto_active": True,
                        "entry": price,
                        "tp": tp_val, "sl": sl_val,
                        "direction": "BUY", "interval": self.cfg.interval,
                        "manual_amount": None,
                        "qty_bought": total,
                        "original_risk": original_risk_val,
                        "atr": atr_val,
                    }
                    self.log_signal.emit("SUCCESS", f"[SPOT] Đã nhận nuôi {symbol} (~{value_usdt:.2f} USDT).")
                else:
                    self.watchlist[symbol].update({
                        "status": "Đang giữ lệnh (Sync)",
                        "auto_active": True,
                        "entry": price,
                        "tp": tp_val,
                        "sl": sl_val,
                        "direction": "BUY",
                        "qty_bought": total,
                        "original_risk": original_risk_val,
                        "atr": atr_val,
                    })
                    self.log_signal.emit("INFO", f"[SPOT] Đã cập nhật {symbol} từ sàn.")

                if not self._has_existing_protection(symbol) and tp_val > 0 and sl_val > 0:
                    try:
                        self.binance.place_oco_sell(symbol, total, tp_val, sl_val, sl_val, "BUY")
                        self.log_signal.emit("INFO", f"[SPOT] Đã đặt SL/TP bảo vệ {symbol}.")
                    except Exception as e:
                        self.log_signal.emit("WARN", f"[SPOT] Không đặt được OCO {symbol}: {e}")

    def _calc_safe_sl_tp(self, symbol: str, entry_price: float, direction: str):
        tp_val, sl_val = 0.0, 0.0
        try:
            df = self.binance.get_ohlcv(symbol, self.cfg.interval, 250)
            if not df.empty and len(df) >= 200:
                df = df.iloc[:-1]
                df = TechnicalIndicatorCalculator.enrich(df)
                plan = TechnicalIndicatorCalculator.calculate_sl_tp(
                    df, entry_price, "BREAKOUT", direction, cfg=self.cfg
                )
                tp_val, sl_val = plan["take_profit"], plan["stop_price"]
        except Exception as e:
            self.log_signal.emit("WARN", f"{symbol}: Không tính được SL/TP ({e}). Fallback.")

        if sl_val <= 0:
            sl_val = entry_price * (1 - self.cfg.sl_min_percent) if direction == "BUY" \
                    else entry_price * (1 + self.cfg.sl_min_percent)
        if tp_val <= 0:
            tp_val = entry_price * (1 + self.cfg.sl_min_percent * self.cfg.min_rr) if direction == "BUY" \
                    else entry_price * (1 - self.cfg.sl_min_percent * self.cfg.min_rr)

        return tp_val, sl_val

    def _has_existing_protection(self, symbol: str) -> bool:
        try:
            if self.cfg.market_type == "FUTURE":
                orders = self.binance.client.futures_get_open_orders(symbol=symbol)
                if not orders: return False
                sl_count = sum(1 for o in orders if o.get("type") in ["STOP_MARKET", "STOP"])
                tp_count = sum(1 for o in orders if o.get("type") in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"])
                return (sl_count >= 1 and tp_count >= 1)
            else:
                # ✅ FIX P1: SPOT dùng get_open_orders (OCO = 2 lệnh)
                orders = self.binance.client.get_open_orders(symbol=symbol)
                return len(orders) >= 2
        except Exception:
            return True

    def _scan_once(self) -> None:
        try: usdt = self.binance.get_usdt_balance()
        except: usdt = 0.0
        self.balance_signal.emit(usdt)

        with self._lock:
            symbols_to_scan = list(self.watchlist.keys())

        for symbol in symbols_to_scan:
            if self._stop_flag: return
            self._process_symbol(symbol, usdt)

    def _count_open_positions(self) -> int:
        with self._lock:
            return sum(1 for s in self.watchlist.values() if s.get("direction") in ("BUY", "SELL"))

    def _process_symbol(self, symbol: str, usdt_balance: float) -> None:
        blacklist = ['USDC', 'FDUSD', 'TUSD', 'BUSD', 'DAI', 'EUR', 'USDP', 'XAUT']
        bad_suffixes = ['UPUSDT', 'DOWNUSDT', 'BULLUSDT', 'BEARUSDT']
        if any(stable in symbol for stable in blacklist):
            return
        if symbol in bad_suffixes:
            return
        base = symbol.replace('USDT', '')
        if not base.isascii() or not base.isalnum():
            return

        with self._lock:
            if symbol not in self.watchlist:
                return
            coin_data = dict(self.watchlist[symbol])
            if not coin_data["auto_active"]:
                return
            interval = coin_data.get("interval", self.cfg.interval)

        try:
            # ================================================================
            # LUỒNG 1: QUẢN LÝ LỆNH ĐANG MỞ (TRAILING & CẮT LỖ)
            # ================================================================
            if self.binance.has_open_position(symbol):
                current_price = self.binance.get_current_price(symbol)
                entry_price = coin_data.get("entry", 0) or current_price
                direction = coin_data.get("direction", "BUY")
                tp_val = coin_data.get("tp", 0)
                sl_val = coin_data.get("sl", 0)
                max_seen = coin_data.get("max_price_seen", current_price)
                min_seen = coin_data.get("min_price_seen", current_price)

                pnl_data = self.binance.get_position_pnl(symbol, entry_price, 1, direction)
                with self._lock:
                    self.watchlist[symbol]["last_pnl"] = pnl_data["pnl_usdt"]
                    # Cập nhật đỉnh/đáy để tính trailing
                    self.watchlist[symbol]["max_price_seen"] = max(max_seen, current_price)
                    self.watchlist[symbol]["min_price_seen"] = min(min_seen, current_price)
                    self.update_row_signal.emit(symbol, "Đang giữ lệnh", {
                        "entry": entry_price, "tp": tp_val, "sl": sl_val, "direction": direction,
                        "pnl_usdt": pnl_data["pnl_usdt"], "pnl_pct": pnl_data["pnl_pct"],
                    })

                # ✅ FIX 1: NẾU MẤT SL CỨNG TRÊN SÀN -> ĐẶT LẠI NGAY (CHỈ 1 LẦN)
                if sl_val <= 0:
                    risk_pct = 0.025 # 2.5%
                    new_sl = entry_price * (1 - risk_pct) if direction == "BUY" else entry_price * (1 + risk_pct)
                    self.log_signal.emit("WARN", f"{symbol}: MẤT SL TRÊN SÀN! Đang tự động đặt lại SL tại {new_sl:.4f}")
                    try:
                        # Hủy lệnh TP cũ (nếu có) và đặt lại cặp OCO mới
                        self.binance.cancel_open_orders(symbol)
                        self.binance.place_oco_sell(symbol, coin_data.get("qty_bought", 0), tp_val, new_sl, new_sl, direction)
                        with self._lock:
                            self.watchlist[symbol]["sl"] = new_sl
                        sl_val = new_sl # Cập nhật lại biến local để check
                    except Exception as e:
                        self.log_signal.emit("ERROR", f"{symbol}: Không thể đặt lại SL: {e}")

                # Kiểm tra chạm SL
                is_sl_hit = False
                if direction == "BUY":
                    if current_price <= sl_val: is_sl_hit = True
                else:
                    if current_price >= sl_val: is_sl_hit = True

                if is_sl_hit:
                    self.log_signal.emit("WARN", f"{symbol}: Chạm SL. Đóng khẩn cấp!")
                    self._close_position_market(symbol, direction)
                    return

                # ✅ FIX 2: SOFT TRAILING (Cập nhật SL thật trên sàn thay vì đóng lệnh)
                original_risk = coin_data.get("original_risk", abs(entry_price - sl_val))
                if original_risk > 0 and tp_val > 0:
                    if direction == "BUY":
                        current_rr = (current_price - entry_price) / original_risk
                    else:
                        current_rr = (entry_price - current_price) / original_risk

                    # Nếu lời >= 1.0 R -> Bắt đầu trailing
                    if current_rr >= getattr(self.cfg, 'soft_trailing_activation_rr', 1.0):
                        if direction == "BUY":
                            drop_from_peak = (max_seen - current_price) / max_seen
                            # Nếu rớt quá mức cho phép, dời SL lên sát giá hiện tại để khóa lời
                            if drop_from_peak >= getattr(self.cfg, 'soft_trailing_drop_pct', 0.01):
                                new_trailing_sl = current_price * 0.998 # Khóa lời ở mức 99.8% giá hiện tại
                                if new_trailing_sl > sl_val: # Chỉ dời lên, không dời xuống
                                    self.log_signal.emit("SUCCESS", f"{symbol}: Trailing mềm! Dời SL lên {new_trailing_sl:.4f}")
                                    try:
                                        self.binance.cancel_open_orders(symbol)
                                        self.binance.place_oco_sell(symbol, coin_data.get("qty_bought", 0), tp_val, new_trailing_sl, new_trailing_sl, direction)
                                        with self._lock:
                                            self.watchlist[symbol]["sl"] = new_trailing_sl
                                    except Exception as e:
                                        self.log_signal.emit("ERROR", f"{symbol}: Lỗi dời SL Trailing: {e}")
                        else:
                            rise_from_trough = (current_price - min_seen) / min_seen
                            if rise_from_trough >= getattr(self.cfg, 'soft_trailing_drop_pct', 0.01):
                                new_trailing_sl = current_price * 1.002 # Khóa lời ở mức 100.2% giá hiện tại
                                if new_trailing_sl < sl_val:
                                    self.log_signal.emit("SUCCESS", f"{symbol}: Trailing mềm! Dời SL xuống {new_trailing_sl:.4f}")
                                    try:
                                        self.binance.cancel_open_orders(symbol)
                                        self.binance.place_oco_sell(symbol, coin_data.get("qty_bought", 0), tp_val, new_trailing_sl, new_trailing_sl, direction)
                                        with self._lock:
                                            self.watchlist[symbol]["sl"] = new_trailing_sl
                                    except Exception as e:
                                        self.log_signal.emit("ERROR", f"{symbol}: Lỗi dời SL Trailing: {e}")

                return

            # ================================================================
            # LUỒNG 2: TÌM LỆNH MỚI
            # ================================================================
            if coin_data.get("direction") in ["BUY", "SELL"]:
                final_pnl = coin_data.get("last_pnl", 0.0)
                old_dir = coin_data.get("direction")
                self.log_signal.emit("WARN", f"{symbol}: Lệnh {old_dir} đã đóng hoàn toàn. Xóa khỏi danh sách.")
                
                self.binance.cancel_open_orders(symbol)
                self.trade_closed_signal.emit(symbol, old_dir, {"pnl": final_pnl, "entry": coin_data.get("entry", 0.0)})
                with self._lock:
                    if symbol in self.watchlist: del self.watchlist[symbol]
                self.remove_coin_signal.emit(self.cfg.market_type, symbol)
                return

            cooldown_until = coin_data.get("cooldown_until", 0)
            if cooldown_until > 0:
                if time.time() < cooldown_until:
                    remain = int((cooldown_until - time.time()) / 60) + 1
                    self.update_row_signal.emit(symbol, f"Tạm nghỉ ({remain}p)", {})
                    return
                else:
                    with self._lock: self.watchlist[symbol]["cooldown_until"] = 0

            if self._count_open_positions() >= self.cfg.max_open_positions:
                return

            try:
                current_price_check = self.binance.get_current_price(symbol)
                if current_price_check < 0.01:
                    self.remove_coin_signal.emit(self.cfg.market_type, symbol)
                    return
            except Exception: pass

            htf_map = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1w"}
            df_htf = self.binance.get_ohlcv(symbol, htf_map.get(interval, "4h"), 250, for_chart=True)
            df = self.binance.get_ohlcv(symbol, interval, 250, for_chart=True)
            
            if df_htf.empty or len(df_htf) < 200 or df.empty or len(df) < 201: return

            df_htf = TechnicalIndicatorCalculator.enrich(df_htf.iloc[:-1])
            df = TechnicalIndicatorCalculator.enrich(df.iloc[:-1])

            regime_htf = TechnicalIndicatorCalculator.get_market_regime(df_htf, cfg=self.cfg)
            regime_ltf = TechnicalIndicatorCalculator.get_market_regime(df, cfg=self.cfg)

            buy_score, sell_score, used_strategy = 0, 0, ""
            base_min = self.cfg.min_signal_score

            if regime_htf == "UPTREND":
                if regime_ltf == "UPTREND":
                    score = TechnicalIndicatorCalculator.get_signal_score(df, "BREAKOUT", "BUY", self.cfg)
                    if score >= max(base_min + 10, 70): buy_score, used_strategy = score, "BREAKOUT"
                elif regime_ltf == "DOWNTREND":
                    score = TechnicalIndicatorCalculator.get_signal_score(df, "PULLBACK", "BUY", self.cfg)
                    last_candle = df.iloc[-1]
                    body = abs(last_candle["close"] - last_candle["open"])
                    lower_wick = min(last_candle["open"], last_candle["close"]) - last_candle["low"]
                    is_confirmed = (lower_wick > body * 1.5) or (last_candle["rsi"] < 35)
                    if score >= base_min and is_confirmed: 
                        buy_score, used_strategy = score, "PULLBACK"

            elif regime_htf == "DOWNTREND" and self.cfg.market_type == "FUTURE":
                if regime_ltf == "DOWNTREND":
                    score = TechnicalIndicatorCalculator.get_signal_score(df, "BREAKOUT", "SELL", self.cfg)
                    if score >= max(base_min + 10, 70): sell_score, used_strategy = score, "BREAKDOWN"
                elif regime_ltf == "UPTREND":
                    score = TechnicalIndicatorCalculator.get_signal_score(df, "PULLBACK", "SELL", self.cfg)
                    last_candle = df.iloc[-1]
                    body = abs(last_candle["close"] - last_candle["open"])
                    upper_wick = last_candle["high"] - max(last_candle["open"], last_candle["close"])
                    is_confirmed = (upper_wick > body * 1.5) or (last_candle["rsi"] > 65)
                    if score >= base_min and is_confirmed:
                        sell_score, used_strategy = score, "PULLBACK_SHORT"

            elif regime_htf == "RANGING":
                if regime_ltf == "RANGING":
                    score_buy = TechnicalIndicatorCalculator.get_signal_score(df, "RANGING", "BUY", self.cfg)
                    if score_buy >= base_min + 5: buy_score, used_strategy = score_buy, "MEAN_REVERSION"
                    if self.cfg.market_type == "FUTURE":
                        score_sell = TechnicalIndicatorCalculator.get_signal_score(df, "RANGING", "SELL", self.cfg)
                        if score_sell >= base_min + 5 and score_sell > buy_score:
                            sell_score, used_strategy = score_sell, "MEAN_REVERSION"
                            buy_score = 0

            direction, final_score = None, 0
            if buy_score >= self.cfg.min_signal_score and used_strategy:
                direction, final_score = "BUY", buy_score
            elif sell_score >= self.cfg.min_signal_score and used_strategy:
                direction, final_score = "SELL", sell_score
            else:
                self.update_row_signal.emit(symbol, f"Chờ (Mua: {buy_score} | Bán: {sell_score})", {})
                return

            entry = self.binance.get_current_price(symbol)
            last_closed_price = df["close"].iloc[-1]

            if abs(entry - last_closed_price) / last_closed_price > 0.015:
                self.log_signal.emit("WARN", f"{symbol}: Giá chạy quá xa nến tín hiệu. Xóa khỏi danh sách.")
                with self._lock:
                    if symbol in self.watchlist: del self.watchlist[symbol]
                self.remove_coin_signal.emit(self.cfg.market_type, symbol)
                return

            final_amount = coin_data.get("manual_amount") or self.cfg.fixed_amount
            current_free = self.binance.get_free_usdt()
            if current_free < 5.0: return
            if final_amount > current_free * 0.9: 
                final_amount = current_free * 0.9
            if final_amount < 5.0: return

            calc_strategy = "PULLBACK" if "PULLBACK" in used_strategy else ("RANGING" if used_strategy == "MEAN_REVERSION" else "BREAKOUT")
            plan = TechnicalIndicatorCalculator.calculate_sl_tp(
                df, entry, calc_strategy, direction, cfg=self.cfg
            )

            if plan.get("skip", False):
                self.log_signal.emit("WARN", f"{symbol}: SL quá rộng. Xóa khỏi danh sách.")
                with self._lock:
                    if symbol in self.watchlist: del self.watchlist[symbol]
                self.remove_coin_signal.emit(self.cfg.market_type, symbol)
                return

            try:
                price_deviation = abs(entry - last_closed_price) / last_closed_price
                if price_deviation > 0.05:
                    self.log_signal.emit("WARN", f"{symbol}: Giá realtime đã chạy xa ({price_deviation*100:.2f}%). BỎ QUA.")
                    return
            except Exception as e:
                self.log_signal.emit("WARN", f"{symbol}: Lỗi kiểm tra realtime - {e}")
                return

            order = self.binance.market_buy_by_quote(symbol, final_amount, direction)
            if not order: return

            actual_entry = entry
            if order.get("fills"):
                total_qty = sum(float(f["qty"]) for f in order["fills"])
                if total_qty > 0:
                    actual_entry = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / total_qty
            elif float(order.get("avgPrice", 0)) > 0: actual_entry = float(order.get("avgPrice"))
            elif float(order.get("price", 0)) > 0: actual_entry = float(order.get("price"))

            is_invalid = False
            if direction == "BUY":
                if actual_entry <= plan["stop_price"] or actual_entry >= plan["take_profit"]: is_invalid = True
            else:
                if actual_entry >= plan["stop_price"] or actual_entry <= plan["take_profit"]: is_invalid = True
                    
            if is_invalid:
                self.log_signal.emit("ERROR", f"{symbol}: Giá khớp {actual_entry:.6f} đã vượt SL/TP. Hủy lệnh khẩn cấp!")
                self._close_position_market(symbol, direction)
                return

            if self.cfg.market_type == "FUTURE":
                qty = float(order.get("executedQty", 0)) or ((final_amount * self.cfg.leverage) / actual_entry)
            else:
                qty = float(order.get("executedQty", 0)) or (final_amount / actual_entry)

            with self._lock:
                self.watchlist[symbol].update({
                    "status": f"Đã vào {direction}", "entry": actual_entry,
                    "tp": plan["take_profit"], "sl": plan["stop_price"], "direction": direction,
                    "original_risk": abs(actual_entry - plan["stop_price"]),
                    "atr": plan.get("atr", actual_entry * 0.02), "qty_bought": qty, "last_pnl": 0.0,
                    "max_price_seen": actual_entry, "min_price_seen": actual_entry # ✅ BỔ SUNG CHO TRAILING
                })
                self.trade_signal.emit(symbol, direction, {
                    "qty": qty, "price": actual_entry, "entry": actual_entry,
                    "tp": plan["take_profit"], "sl": plan["stop_price"],
                    "direction": direction, "amount": final_amount,
                })

            ok = self._place_oco_with_retry(
                symbol, qty, tp=plan["take_profit"], sl=plan["stop_price"], sl_limit=plan["stop_limit"], direction=direction,
            )

            if ok:
                rr2 = abs(plan["take_profit"] - actual_entry) / max(abs(actual_entry - plan["stop_price"]), 1e-9)
                self.log_signal.emit(
                    "SUCCESS",
                    f"[{self.cfg.market_type}] {symbol}: Vào {direction} @ {actual_entry:.6f} | "
                    f"SL={plan['stop_price']:.6f} ({plan['risk_pct']:.2f}%) | "
                    f"TP={plan['take_profit']:.6f} (RR≈{rr2:.2f})"
                )
            else:
                self.log_signal.emit("ERROR", f"{symbol}: KHÔNG đặt được SL/TP! Đóng lệnh khẩn cấp...")
                self._close_position_market(symbol, direction)

        except Exception as e:
            self.log_signal.emit("ERROR", f"{symbol}: Lỗi xử lý – {e}")

    def _place_oco_with_retry(self, symbol: str, qty: float, tp: float, sl: float, sl_limit: float, direction: str, max_retry: int = 2) -> bool:
        for attempt in range(max_retry):
            try:
                self.binance.cancel_open_orders(symbol)
                time.sleep(0.3)
                result = self.binance.place_oco_sell(symbol, qty, tp, sl, sl_limit, direction)
                if result: return True
            except Exception as e:
                self.log_signal.emit("WARN", f"{symbol}: Lỗi đặt OCO lần {attempt+1}: {e}")
            time.sleep(1)
        return False

    def _get_actual_qty(self, symbol: str, max_qty: float = None) -> float:
        try:
            if self.cfg.market_type == "FUTURE":
                positions = self.binance.client.futures_position_information(symbol=symbol)
                for p in positions:
                    if p['symbol'] == symbol:
                        return abs(float(p['positionAmt']))
                return 0.0
            else:
                base = symbol.replace("USDT", "")
                bal = self.binance.client.get_asset_balance(asset=base)
                if bal:
                    available = float(bal['free']) + float(bal['locked'])
                    if max_qty is not None and max_qty > 0:
                        return min(available, max_qty)
                    return available
                return 0.0
        except Exception as e:
            log.warning(f"_get_actual_qty lỗi {symbol}: {e}")
            return 0.0

    def _close_position_market(self, symbol: str, current_direction: str):
        try:
            self.log_signal.emit("INFO", f"{symbol}: Bắt đầu đóng vị thế khẩn cấp...")

            # ✅ FIX P1: Lấy PnL + close_price TRƯỚC khi đóng
            with self._lock:
                entry_hint = self.watchlist.get(symbol, {}).get("entry", 0)
            try:
                pre_pnl = self.binance.get_position_pnl(symbol, entry_hint, 1, current_direction)
                actual_pnl = pre_pnl.get("pnl_usdt", 0.0)
            except Exception:
                actual_pnl = 0.0
            try:
                close_price = self.binance.get_current_price(symbol)
            except Exception:
                close_price = 0.0

            try:
                self.binance.cancel_open_orders(symbol)
                time.sleep(0.3)
            except Exception:
                pass

            with self._lock:
                qty_bought = self.watchlist.get(symbol, {}).get("qty_bought", 0)

            actual_qty = self._get_actual_qty(
                symbol, max_qty=qty_bought if qty_bought > 0 else None
            )

            close_qty = 0.0
            if self.cfg.market_type == "FUTURE":
                close_qty = actual_qty
            else:
                if qty_bought > 0:
                    close_qty = min(qty_bought, actual_qty)
                else:
                    close_qty = actual_qty

            if close_qty > 0:
                f = self.binance._get_filters(symbol)
                qty_str = self.binance._format_value(close_qty, f["step_size_str"])
                side = "SELL" if current_direction == "BUY" else "BUY"

                if self.cfg.market_type == "FUTURE":
                    self.binance.client.futures_create_order(
                        symbol=symbol, side=side, type="MARKET", quantity=qty_str
                    )
                else:
                    self.binance.client.order_market_sell(symbol=symbol, quantity=qty_str)
                self.log_signal.emit("SUCCESS", f"{symbol}: Đã đóng vị thế Market ({qty_str}).")

            try:
                self.binance.cancel_open_orders(symbol)
            except Exception:
                pass

            # ✅ FIX: Xóa hẳn khỏi watchlist để giải phóng slot ngay lập tức
            with self._lock:
                if symbol in self.watchlist:
                    del self.watchlist[symbol] 
            
            # ✅ Bắn tín hiệu xóa dòng khỏi bảng giao diện
            self.remove_coin_signal.emit(self.cfg.market_type, symbol)
            
            # (Tùy chọn) Bắn tín hiệu ghi nhận lịch sử PnL
            self.trade_closed_signal.emit(symbol, current_direction, {
                "pnl": actual_pnl, "entry": entry_hint, "close_price": close_price
            })

            # ✅ FIX: emit kèm close_price
            self.trade_closed_signal.emit(symbol, current_direction, {
                "pnl": actual_pnl, "entry": entry_hint, "close_price": close_price
            })

        except Exception as e:
            self.log_signal.emit("ERROR", f"{symbol}: Lỗi đóng lệnh - {e}")

class DivergenceScannerThread(QThread):
    log_signal = pyqtSignal(str, str)
    result_signal = pyqtSignal(list)

    def __init__(self, cfg: Config, market_type: str = "FUTURE"):
        super().__init__()
        self.cfg = copy.deepcopy(cfg)
        self.cfg.market_type = market_type
        self.binance = BinanceDataService(self.cfg)

    def run(self):
        self.log_signal.emit("INFO", "Đang quét CVD Divergence tìm kèo đảo chiều...")
        try:
            info = self.binance.client.futures_exchange_info() if self.cfg.market_type == "FUTURE" else self.binance.client.get_exchange_info()
            
            blacklist = ['USDC', 'FDUSD', 'TUSD', 'BUSD', 'DAI', 'EUR', 'USDP', 'XAUT']
            bad_suffixes = ['UPUSDT', 'DOWNUSDT', 'BULLUSDT', 'BEARUSDT']
            valid_symbols = []
            for s in info['symbols']:
                if s['status'] == 'TRADING' and s['symbol'].endswith('USDT'):
                    base_asset = s.get('baseAsset', '')
                    if any(stable in base_asset for stable in blacklist):
                        continue
                    if s['symbol'] in bad_suffixes:
                        continue
                    if not base_asset.isascii() or not base_asset.isalnum():
                        continue
                    valid_symbols.append(s['symbol'])

            tickers = self.binance.public_client.get_ticker()
            ticker_dict = {t['symbol']: float(t['quoteVolume']) for t in tickers if t['symbol'].endswith('USDT')}

            valid_symbols = [s for s in valid_symbols if ticker_dict.get(s, 0) > 50_000_000]
            valid_symbols.sort(key=lambda sym: ticker_dict.get(sym, 0), reverse=True)
            top_symbols = valid_symbols

            results = []
            for symbol in top_symbols:
                try:
                    df = self.binance.get_ohlcv(symbol, self.cfg.interval, 100, for_chart=True)
                    if df.empty or len(df) < 30: continue
                    df = df.iloc[:-1]
                    df = TechnicalIndicatorCalculator.enrich(df)

                    div_type = TechnicalIndicatorCalculator.check_divergence(df, lookback=20)
                    if div_type != "NONE":
                        results.append((symbol, div_type))
                except: continue

            self.result_signal.emit(results)
        except Exception as e:
            self.log_signal.emit("ERROR", f"Lỗi quét Divergence: {e}")
            self.result_signal.emit([])