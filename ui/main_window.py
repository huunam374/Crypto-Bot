import logging
from datetime import datetime
from typing import Optional
import copy

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QFormLayout, QLineEdit, QCheckBox, QPushButton, QLabel,
    QTextEdit, QTableWidget, QTableWidgetItem, QDoubleSpinBox,
    QSpinBox, QSplitter, QMessageBox, QHeaderView, QComboBox, 
    QCompleter, QDialog, QInputDialog, QScrollArea, QTabWidget,
    QSizePolicy
)
from PyQt6.QtCore import QTimer, Qt, QRectF, QPointF, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor, QPicture, QPainter, QPen, QBrush

import pyqtgraph as pg

from config import Config
from core.trading_worker import TradingWorker, CoinScannerThread, DivergenceScannerThread
from services.binance_service import BinanceDataService
from core.indicators import TechnicalIndicatorCalculator

log = logging.getLogger(__name__)


class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data):
        pg.GraphicsObject.__init__(self)
        self.data = data  
        self.generatePicture()

    def generatePicture(self):
        self.picture = QPicture()
        p = QPainter(self.picture)
        p.setPen(pg.mkPen('w'))
        w = 0.35 
        for (t, open, high, low, close) in self.data:
            if close > open: p.setBrush(pg.mkBrush('#0ecb81'))
            else: p.setBrush(pg.mkBrush('#f6465d'))
            p.drawLine(QPointF(t, low), QPointF(t, high))
            p.drawRect(QRectF(t - w, open, w * 2, close - open))
        p.end()

    def paint(self, p, *args): p.drawPicture(0, 0, self.picture)
    def boundingRect(self): return QRectF(self.picture.boundingRect())

class ChartLoader(QThread):
    """✅ FIX P0: Tải dữ liệu chart trong thread phụ, không block UI."""
    done = pyqtSignal(object, str, str)   # df, symbol, interval

    def __init__(self, binance, symbol, interval, limit=1000):
        super().__init__()
        self.binance = binance
        self.symbol = symbol
        self.interval = interval
        self.limit = limit

    def run(self):
        try:
            df = self.binance.get_ohlcv(self.symbol, self.interval, self.limit, for_chart=True)
            if df is None or df.empty:
                self.done.emit(None, self.symbol, self.interval)
                return
            df = df.sort_index().dropna()
            df = df[(df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0) & (df['close'] > 0)]
            if df.empty:
                self.done.emit(None, self.symbol, self.interval)
                return
            df = TechnicalIndicatorCalculator.enrich(df)
            self.done.emit(df, self.symbol, self.interval)
        except Exception:
            self.done.emit(None, self.symbol, self.interval)


class ChartDialog(QDialog):
    def __init__(self, parent, binance_ui, initial_symbol, symbol_list=None):
        super().__init__(parent)
        self.binance_ui = binance_ui
        self.setWindowTitle("Xem chi tiết biểu đồ (1000 nến)")
        self.resize(1200, 800)
        self.setStyleSheet("background-color: #181a20; color: white;")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        top_bar = QHBoxLayout()

        self.combo = QComboBox()
        self.combo.setEditable(True)

        # ✅ FIX P0: Không gọi get_exchange_info() ở đây
        if symbol_list:
            self.combo.addItems(symbol_list)
        else:
            self.combo.addItems([initial_symbol])

        self.combo.setCurrentText(initial_symbol)
        self.combo.setStyleSheet("padding: 5px; font-size: 14px;")
        self.combo.currentTextChanged.connect(self.load_chart)

        self.combo_tf = QComboBox()
        self.combo_tf.addItems(["1h", "15m", "4h", "1d"])
        self.combo_tf.setStyleSheet("padding: 5px; font-size: 14px;")
        self.combo_tf.currentTextChanged.connect(self.load_chart)

        self.chk_sonic = QCheckBox("Sonic R"); self.chk_sonic.setChecked(False)
        self.chk_bb = QCheckBox("Bollinger"); self.chk_bb.setChecked(False)
        self.chk_vol = QCheckBox("Volume"); self.chk_vol.setChecked(False)

        self.chk_sonic.stateChanged.connect(self.load_chart)
        self.chk_bb.stateChanged.connect(self.load_chart)
        self.chk_vol.stateChanged.connect(self.load_chart)

        btn_refresh = QPushButton("Tải lại")
        btn_refresh.clicked.connect(self.load_chart)
        btn_refresh.setStyleSheet("background: #3b82f6; color: white; padding: 5px; font-weight: bold;")

        top_bar.addWidget(QLabel("Chọn Coin:"))
        top_bar.addWidget(self.combo, stretch=2)
        top_bar.addWidget(QLabel("Khung giờ:"))
        top_bar.addWidget(self.combo_tf, stretch=1)
        top_bar.addWidget(self.chk_sonic)
        top_bar.addWidget(self.chk_bb)
        top_bar.addWidget(self.chk_vol)
        top_bar.addWidget(btn_refresh)
        layout.addLayout(top_bar)

        self.plot_widget = pg.PlotWidget(background='#181a20')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.showAxis('right')
        self.plot_widget.hideAxis('left')
        layout.addWidget(self.plot_widget)

        self._loader = None
        self.load_chart()

    def load_chart(self):
        symbol = self.combo.currentText().strip().upper()
        interval = self.combo_tf.currentText()
        if not symbol: return

        if self._loader is not None and self._loader.isRunning():
            return

        self.plot_widget.setTitle(f"<span style='color:#fcd535;font-size:16px;'>Đang tải {symbol} - {interval}...</span>")

        # ✅ FIX P0: dùng ChartLoader (thread phụ)
        self._loader = ChartLoader(self.binance_ui, symbol, interval, 1000)
        self._loader.done.connect(self._on_data)
        self._loader.start()

    def _on_data(self, df, symbol, interval):
        if df is None or df.empty:
            self.plot_widget.setTitle(f"<span style='color:red;'>Coin {symbol} không có dữ liệu.</span>")
            return
        try:
            self.plot_widget.clear()
            self.plot_widget.getAxis('bottom').setTicks(None)

            data, time_labels = [], []
            x_indices = []
            min_price = float('inf')
            max_price = float('-inf')

            for i, row in df.iterrows():
                o, h, l, c = float(row['open']), float(row['high']), float(row['low']), float(row['close'])
                data.append((len(data), o, h, l, c))
                x_indices.append(len(data)-1)
                time_labels.append((len(data)-1, row.name.strftime("%d/%m %H:%M")))
                min_price = min(min_price, l)
                max_price = max(max_price, h)

            if not data: return

            self.plot_widget.addItem(CandlestickItem(data))

            if self.chk_sonic.isChecked() and 'ema34' in df.columns:
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['ema34'].values, pen=pg.mkPen('#22c55e', width=1.5)))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['ema89'].values, pen=pg.mkPen('#f59e0b', width=1.5)))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['ema200'].values, pen=pg.mkPen('#ef4444', width=1.5)))

            if self.chk_bb.isChecked() and 'bb_upper' in df.columns:
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['bb_upper'].values, pen=pg.mkPen('#94a3b8', width=1, style=Qt.PenStyle.DashLine)))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['bb_mid'].values, pen=pg.mkPen('#cbd5e1', width=1)))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['bb_lower'].values, pen=pg.mkPen('#94a3b8', width=1, style=Qt.PenStyle.DashLine)))

            if self.chk_vol.isChecked() and 'volume' in df.columns:
                vol_max = df['volume'].max()
                price_range = max_price - min_price
                vol_heights = (df['volume'] / vol_max) * (price_range * 0.2) + min_price
                brushes = [pg.mkBrush('#0ecb81') if row['close'] >= row['open'] else pg.mkBrush('#f6465d') for _, row in df.iterrows()]
                bg = pg.BarGraphItem(x=x_indices, height=vol_heights - min_price, width=0.6, y0=min_price, brushes=brushes)
                self.plot_widget.addItem(bg)

            step = max(1, len(time_labels) // 8)
            ticks = [time_labels[i] for i in range(0, len(time_labels), step)]
            self.plot_widget.getAxis('bottom').setTicks([ticks])

            current_price = data[-1][4]
            self.plot_widget.addItem(pg.InfiniteLine(pos=current_price, angle=0, pen=pg.mkPen('#fcd535', width=1.5)))

            price_label = pg.TextItem(f"{current_price:,.4f}", color='#181a20', fill='#fcd535', anchor=(0, 0.5))
            price_label.setPos(len(data) + 10, current_price)
            self.plot_widget.addItem(price_label)

            self.plot_widget.setTitle(f"<span style='color:#e2e8f0;font-size:16px;'>{symbol} - {interval} (Giá: {current_price:,.4f})</span>")

            padding = (max_price - min_price) * 0.1
            if padding == 0: padding = max_price * 0.001
            self.plot_widget.setYRange(min_price - padding, max_price + padding)
            self.plot_widget.setXRange(-5, len(data) + 30, padding=0)

        except Exception as e:
            log.warning(f"Lỗi vẽ chart popup: {e}")

class MainWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.worker_spot: Optional[TradingWorker] = None
        self.worker_future: Optional[TradingWorker] = None
        self.scanner_thread: Optional[CoinScannerThread] = None
        self.div_scanner: Optional[DivergenceScannerThread] = None
        self.binance_ui = BinanceDataService(cfg)

        self.table_row_map_spot = {}
        self.table_row_map_future = {}
        self.active_trades = {}

        # ✅ FIX P0: cache danh sách symbol + chart loader
        self._all_symbols: list = []
        self._chart_loader: Optional[ChartLoader] = None

        self.setWindowTitle("Crypto Trading Bot")
        self.resize(1350, 780)
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._trigger_chart_refresh)
        self._refresh_timer.start(5000)

        self._auto_scan_timer = QTimer(self)
        self._auto_scan_timer.timeout.connect(self._run_fully_auto)
        self._auto_scan_timer.start(int(self.cfg.auto_scan_interval_min * 60 * 1000))

        self._cvd_timer = QTimer(self)
        self._cvd_timer.timeout.connect(self._auto_scan_cvd)
        self._cvd_timer.start(int(self.cfg.cvd_scan_interval_min * 60 * 1000))

        self._active_msgbox = None

        QTimer.singleShot(500, self._auto_load_all_binance_coins)

    def _count_active_positions(self) -> int:
        total = 0
        if self.worker_spot:
            total += sum(1 for s in self.worker_spot.watchlist.values() if s.get("direction") in ("BUY", "SELL"))
        if self.worker_future:
            total += sum(1 for s in self.worker_future.watchlist.values() if s.get("direction") in ("BUY", "SELL"))
        return total

    def _count_watchlist_entries(self) -> int:
        """Đếm tổng số symbol trong watchlist (dùng cho việc giới hạn slot thêm coin)."""
        total = 0
        if self.worker_spot: total += len(self.worker_spot.watchlist)
        if self.worker_future: total += len(self.worker_future.watchlist)
        return total

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)

        left_layout = QVBoxLayout()
        left_layout.addWidget(self._build_settings_group())
        left_layout.addWidget(self._build_account_group())
        left_layout.addWidget(self._build_control_group())
        
        self.potential_group = QGroupBox("🌟 Coin Tiềm Năng")
        pot_vbox = QVBoxLayout(self.potential_group)
        
        header_layout = QHBoxLayout()
        btn_clear_pot = QPushButton("❌ Xóa DS")
        btn_clear_pot.setStyleSheet("background:#dc2626;color:white;font-weight:bold;padding:2px 5px;border-radius:3px;font-size: 11px;")
        btn_clear_pot.setFixedWidth(70)
        btn_clear_pot.clicked.connect(self._clear_potential_layout)
        header_layout.addStretch()
        header_layout.addWidget(btn_clear_pot)
        pot_vbox.addLayout(header_layout)

        scroll_pot = QScrollArea()
        scroll_pot.setWidgetResizable(True)
        scroll_pot.setMinimumHeight(250) 
        scroll_pot.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.potential_widget = QWidget()
        self.potential_layout = QVBoxLayout(self.potential_widget)
        self.potential_layout.setContentsMargins(0, 0, 0, 0)
        self.potential_layout.setSpacing(2) 
        self.potential_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll_pot.setWidget(self.potential_widget)
        pot_vbox.addWidget(scroll_pot)
        left_layout.addWidget(self.potential_group)

        scroll_left = QScrollArea()
        scroll_left.setWidgetResizable(True)
        scroll_left.setStyleSheet("QScrollArea { border: none; }")
        w_left_content = QWidget()
        w_left_content.setLayout(left_layout)
        scroll_left.setWidget(w_left_content)
        scroll_left.setMinimumWidth(300)
        scroll_left.setMaximumWidth(350)

        middle_splitter = QSplitter(Qt.Orientation.Vertical)
        middle_splitter.addWidget(self._build_chart_group())
        
        self.tab_trades = QTabWidget()
        self.tab_trades.addTab(self._build_trade_table_group("SPOT"), "🪙 Spot")
        self.tab_trades.addTab(self._build_trade_table_group("FUTURE"), "🚀 Futures")
        self.tab_trades.addTab(self._build_history_table_group(), "🕰️ Lịch sử")
        middle_splitter.addWidget(self.tab_trades)
        
        middle_splitter.addWidget(self._build_log_group())
        middle_splitter.setSizes([350, 250, 150])

        w_middle = QWidget()
        w_middle.setLayout(QVBoxLayout())
        w_middle.layout().addWidget(middle_splitter)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(scroll_left)
        main_splitter.addWidget(w_middle)
        main_splitter.setSizes([320, 1130])
        root.addWidget(main_splitter)

    def _build_settings_group(self) -> QGroupBox:
        gb = QGroupBox("⚙️ Cài đặt chung")
        form = QFormLayout(gb)

        self.btn_api = QPushButton("🔑 Cài đặt API Key / Testnet")
        self.btn_api.setStyleSheet("background:#475569;color:white;font-weight:bold;padding:5px;")
        self.btn_api.clicked.connect(self._open_api_settings)
        form.addRow(self.btn_api)

        self.combo_mode = QComboBox()
        self.combo_mode.addItems([
            "1. Thủ công", 
            "2. Ful Auto"
        ])
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Chế độ hoạt động:", self.combo_mode)

        self.combo_timeframe = QComboBox()
        self.combo_timeframe.addItems(["1h", "15m", "4h", "1d"])
        self.combo_timeframe.currentTextChanged.connect(self._trigger_chart_refresh)

        self.inp_fixed_amount = QDoubleSpinBox()
        self.inp_fixed_amount.setRange(5, 100000)
        self.inp_fixed_amount.setValue(38)
        self.inp_fixed_amount.setSuffix(" USDT")
        
        self.inp_score = QSpinBox()
        self.inp_score.setRange(10, 100)
        self.inp_score.setValue(70)
        self.inp_score.setSuffix(" /điểm")
        
        self.inp_max_positions = QSpinBox()
        self.inp_max_positions.setRange(1, 50)
        self.inp_max_positions.setValue(getattr(self.cfg, 'max_open_positions', 5))
        self.inp_max_positions.setSuffix(" lệnh")

        self.inp_leverage = QSpinBox()
        self.inp_leverage.setRange(1, 20)
        self.inp_leverage.setValue(self.cfg.leverage)
        self.inp_leverage.setPrefix("x")

        form.addRow("Khung giờ:", self.combo_timeframe)
        form.addRow("Số tiền mỗi lệnh:", self.inp_fixed_amount)
        form.addRow("Điểm tín hiệu tối thiểu:", self.inp_score)
        form.addRow("Số lệnh tối đa (Full Auto):", self.inp_max_positions)
        form.addRow("Đòn bẩy (Future):", self.inp_leverage)

        self.chk_testnet = QCheckBox("Dùng Binance Testnet"); self.chk_testnet.setChecked(self.cfg.testnet)
        self.chk_run_spot = QCheckBox("Bật Spot Bot"); self.chk_run_spot.setChecked(True)
        self.chk_run_future = QCheckBox("Bật Futures Bot"); self.chk_run_future.setChecked(True)

        form.addRow("", self.chk_testnet)
        form.addRow("", self.chk_run_spot)
        form.addRow("", self.chk_run_future)

        self._on_mode_changed(0)
        return gb

    def _on_mode_changed(self, index):
        if index == 0:
            self.inp_fixed_amount.show()
            self.inp_max_positions.hide()
            self.inp_leverage.setEnabled(True)
            self.inp_leverage.setStyleSheet("")
        else:
            self.inp_fixed_amount.show()
            self.inp_max_positions.show()
            self.inp_leverage.setEnabled(True) 
            self.inp_leverage.setStyleSheet("") 
            
    def _build_chart_group(self) -> QGroupBox:
        gb = QGroupBox("📈 Đồ thị nến")
        v = QVBoxLayout(gb)
        
        top_layout = QHBoxLayout()
        self.combo_symbols = QComboBox()
        self.combo_symbols.setEditable(True)
        self.combo_symbols.addItems(["BTCUSDT", "ETHUSDT"])
        completer = self.combo_symbols.completer()
        completer.setFilterMode(Qt.MatchFlag.MatchContains) 
        self.combo_symbols.activated.connect(self._fetch_and_draw_chart)
        
        self.chk_sonic = QCheckBox("Sonic R"); self.chk_sonic.setChecked(False)
        self.chk_bb = QCheckBox("Bollinger"); self.chk_bb.setChecked(False)
        self.chk_vol = QCheckBox("Volume"); self.chk_vol.setChecked(False)
        
        self.chk_sonic.stateChanged.connect(self._trigger_chart_refresh)
        self.chk_bb.stateChanged.connect(self._trigger_chart_refresh)
        self.chk_vol.stateChanged.connect(self._trigger_chart_refresh)

        self.btn_potential = QPushButton("🔍 Tìm Coin Tiềm Năng")
        self.btn_potential.setStyleSheet("background:#8b5cf6;color:white;font-weight:bold;padding:5px;")
        self.btn_potential.clicked.connect(self._start_finding_coins)
        
        btn_expand = QPushButton("⛶ Đồ thị")
        btn_expand.setStyleSheet("background:#f59e0b;color:white;font-weight:bold;padding:5px;")
        btn_expand.clicked.connect(self._open_big_chart)

        self.btn_add_trade = QPushButton("🎯 Đưa vào Trade")
        self.btn_add_trade.setStyleSheet("background:#3b82f6;color:white;font-weight:bold;padding:5px;")
        self.btn_add_trade.clicked.connect(self._add_coin_to_trade)

        top_layout.addWidget(QLabel("Coin:"))
        top_layout.addWidget(self.combo_symbols, stretch=1)
        top_layout.addWidget(self.chk_sonic)
        top_layout.addWidget(self.chk_bb)
        top_layout.addWidget(self.chk_vol)
        top_layout.addWidget(self.btn_potential)
        top_layout.addWidget(btn_expand)
        top_layout.addWidget(self.btn_add_trade)
        v.addLayout(top_layout)

        pg.setConfigOptions(antialias=True)
        self.plot_widget = pg.PlotWidget(background='#181a20')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2) 
        self.plot_widget.showAxis('right')
        self.plot_widget.hideAxis('left')
        
        self.plot_widget.setMouseTracking(True) 

        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#cbd5e1', width=1, style=Qt.PenStyle.DashLine))
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('#cbd5e1', width=1, style=Qt.PenStyle.DashLine))
        self.v_line.setZValue(100)
        self.h_line.setZValue(100)
        self.plot_widget.addItem(self.v_line, ignoreBounds=True)
        self.plot_widget.addItem(self.h_line, ignoreBounds=True)

        self.label_x = pg.TextItem("", color='#181a20', fill='#cbd5e1', anchor=(0.5, 0))
        self.label_y = pg.TextItem("", color='#181a20', fill='#cbd5e1', anchor=(0, 0.5))
        self.label_x.setZValue(101)
        self.label_y.setZValue(101)
        self.plot_widget.addItem(self.label_x, ignoreBounds=True)
        self.plot_widget.addItem(self.label_y, ignoreBounds=True)

        self.plot_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)

        v.addWidget(self.plot_widget)
        return gb

    def _build_trade_table_group(self, market_type: str) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        tbl = QTableWidget(0, 13)
        tbl.setHorizontalHeaderLabels([
            "Coin", "Trạng thái", "Khung giờ", "Auto Trade", "Buy/Sell", "Vốn", "Giá Vào", "TP", "SL", "PnL (USDT)", "PnL (%)", "Hành động", "Chi tiết"
        ])
        
        header = tbl.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        
        tbl.setColumnWidth(0, 80)
        tbl.setColumnWidth(1, 120)
        tbl.setColumnWidth(2, 70)
        tbl.setColumnWidth(3, 90)
        tbl.setColumnWidth(4, 70)
        tbl.setColumnWidth(5, 80)
        tbl.setColumnWidth(6, 80)
        tbl.setColumnWidth(7, 80)
        tbl.setColumnWidth(8, 80)
        tbl.setColumnWidth(9, 80)
        tbl.setColumnWidth(10, 80)
        tbl.setColumnWidth(11, 90)
        tbl.setColumnWidth(12, 90)
        
        header.setStretchLastSection(True)

        tbl.verticalHeader().setVisible(False)
        tbl.setMinimumHeight(100)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.cellClicked.connect(lambda r, c, m=market_type: self._on_table_coin_clicked(r, c, m))
        tbl.cellDoubleClicked.connect(lambda r, c, m=market_type: self._on_table_double_click(r, c, m))
        
        v.addWidget(tbl)
        
        if market_type == "SPOT": self.tbl_spot = tbl
        else: self.tbl_future = tbl
        return w    
  
    def _build_account_group(self) -> QGroupBox:
        gb = QGroupBox("💰 Tài khoản")
        v = QVBoxLayout(gb)
        self.lbl_balance = QLabel("Số dư USDT: —")
        self.lbl_balance.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        v.addWidget(self.lbl_balance)
        return gb

    def _build_control_group(self) -> QGroupBox:
        gb = QGroupBox("🎮 Điều khiển")
        v = QVBoxLayout(gb)
        self.btn_start = QPushButton("▶️ BẮT ĐẦU BOT")
        self.btn_start.setStyleSheet("QPushButton{background:#16a34a;color:white;font-weight:bold;padding:10px;border-radius:6px;} QPushButton:hover{background:#15803d;}")
        self.btn_stop = QPushButton("⏹️ DỪNG BOT")
        self.btn_stop.setStyleSheet("QPushButton{background:#dc2626;color:white;font-weight:bold;padding:10px;border-radius:6px;} QPushButton:hover{background:#b91c1c;}")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._start_bot)
        self.btn_stop.clicked.connect(self._stop_bot)
        v.addWidget(self.btn_start); v.addWidget(self.btn_stop)
        return gb

    def _build_log_group(self) -> QGroupBox:
        gb = QGroupBox("📜 Nhật ký hoạt động")
        v = QVBoxLayout(gb)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Consolas", 10))
        self.txt_log.setStyleSheet("background:#0f172a;color:#e2e8f0;")
        self.txt_log.setMinimumHeight(80)
        v.addWidget(self.txt_log)
        return gb

    def _open_api_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Cài đặt API Key (Spot & Future)")
        dialog.resize(500, 320)
        layout = QFormLayout(dialog)

        inp_spot_key = QLineEdit(self.cfg.api_key); inp_spot_key.setEchoMode(QLineEdit.EchoMode.Password)
        inp_spot_secret = QLineEdit(self.cfg.api_secret); inp_spot_secret.setEchoMode(QLineEdit.EchoMode.Password)
        inp_fut_key = QLineEdit(self.cfg.api_key_future); inp_fut_key.setEchoMode(QLineEdit.EchoMode.Password)
        inp_fut_secret = QLineEdit(self.cfg.api_secret_future); inp_fut_secret.setEchoMode(QLineEdit.EchoMode.Password)

        layout.addRow(QLabel("<b style='color:#3b82f6'>--- SPOT API KEY ---</b>"))
        layout.addRow("API Key:", inp_spot_key)
        layout.addRow("API Secret:", inp_spot_secret)
        
        layout.addRow(QLabel("<br><b style='color:#f59e0b'>--- FUTURES API KEY ---</b>"))
        layout.addRow("API Key:", inp_fut_key)
        layout.addRow("API Secret:", inp_fut_secret)

        btn_box = QHBoxLayout()
        btn_save = QPushButton("💾 Lưu cài đặt"); btn_save.setStyleSheet("background:#16a34a;color:white;padding:5px;font-weight:bold;")
        btn_cancel = QPushButton("Hủy"); btn_cancel.clicked.connect(dialog.reject)
        btn_save.clicked.connect(dialog.accept)
        btn_box.addWidget(btn_save); btn_box.addWidget(btn_cancel)
        layout.addRow(btn_box)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cfg.api_key = inp_spot_key.text().strip()
            self.cfg.api_secret = inp_spot_secret.text().strip()
            self.cfg.api_key_future = inp_fut_key.text().strip()
            self.cfg.api_secret_future = inp_fut_secret.text().strip()
            self.cfg.testnet = self.chk_testnet.isChecked()
            self.cfg.save()
            self.binance_ui = BinanceDataService(self.cfg)
            QMessageBox.information(self, "Thành công", "Đã lưu API Key.")

    def _trigger_chart_refresh(self):
        if self._chart_loader is not None and self._chart_loader.isRunning():
            return
        symbol = self.combo_symbols.currentText()
        if symbol:
            self._fetch_and_draw_chart()


    def _auto_load_all_binance_coins(self):
        # ✅ FIX P0: load danh sách coin trong thread phụ
        parent = self
        class _Loader(QThread):
            done = pyqtSignal(list)
            def run(self_inner):
                try:
                    info = parent.binance_ui.public_client.get_exchange_info()
                    symbols = [s['symbol'] for s in info['symbols']
                               if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
                    symbols.sort()
                    self_inner.done.emit(symbols)
                except Exception:
                    self_inner.done.emit([])

        self._symbol_loader = _Loader(self)
        def _on_done(symbols):
            if not symbols:
                return
            self._all_symbols = symbols
            self.combo_symbols.clear()
            self.combo_symbols.addItems(symbols)
            self.combo_symbols.setCurrentText("BTCUSDT")
            self._fetch_and_draw_chart()
        self._symbol_loader.done.connect(_on_done)
        self._symbol_loader.start()

    def _fetch_and_draw_chart(self):
        symbol = self.combo_symbols.currentText().strip().upper()
        interval = self.combo_timeframe.currentText()
        if not symbol: return

        # ✅ FIX P0: guard đang tải
        if self._chart_loader is not None and self._chart_loader.isRunning():
            return

        self.plot_widget.setTitle(f"<span style='color:#fcd535;font-size:14px;'>Đang tải biểu đồ {symbol}...</span>")

        self._chart_loader = ChartLoader(self.binance_ui, symbol, interval, 1000)
        self._chart_loader.done.connect(self._on_main_chart_data)
        self._chart_loader.start()

    def _on_main_chart_data(self, df, symbol, interval):
        if df is None or df.empty:
            self.plot_widget.setTitle(f"<span style='color:#f6465d;font-size:14px;'>Không có dữ liệu cho {symbol}</span>")
            return
        try:
            display_df = df.tail(200)
            self._update_chart_ui(symbol, display_df, interval)
        except Exception as e:
            log.error(f"Lỗi vẽ chart: {e}")
            self.plot_widget.setTitle(f"<span style='color:#f6465d;font-size:14px;'>Lỗi tải biểu đồ {symbol}</span>")

    def _update_chart_ui(self, symbol: str, df, interval: str):
        try:
            self.plot_widget.clear()

            self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#cbd5e1', width=1, style=Qt.PenStyle.DashLine))
            self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('#cbd5e1', width=1, style=Qt.PenStyle.DashLine))
            self.v_line.setZValue(100)
            self.h_line.setZValue(100)
            self.plot_widget.addItem(self.v_line, ignoreBounds=True)
            self.plot_widget.addItem(self.h_line, ignoreBounds=True)

            self.label_x = pg.TextItem("", color='#181a20', fill='#cbd5e1', anchor=(0.5, 0))
            self.label_y = pg.TextItem("", color='#181a20', fill='#cbd5e1', anchor=(0, 0.5))
            self.label_x.setZValue(101)
            self.label_y.setZValue(101)
            self.plot_widget.addItem(self.label_x, ignoreBounds=True)
            self.plot_widget.addItem(self.label_y, ignoreBounds=True)

            self.plot_widget.getAxis('bottom').setTicks(None)
            data, time_labels = [], []
            min_price = float('inf')
            max_price = float('-inf')
            x_indices = []
            
            for i, row in df.iterrows():
                o, h, l, c = float(row['open']), float(row['high']), float(row['low']), float(row['close'])
                data.append((len(data), o, h, l, c))
                x_indices.append(len(data)-1)
                time_labels.append((len(data)-1, row.name.strftime("%H:%M")))
                min_price = min(min_price, l)
                max_price = max(max_price, h)

            self.current_chart_times = [t[1] for t in time_labels]

            if not data: return
            
            self.plot_widget.addItem(CandlestickItem(data))
            ticks = [time_labels[i] for i in range(0, len(time_labels), max(1, len(time_labels)//6))]
            self.plot_widget.getAxis('bottom').setTicks([ticks])
            
            if self.chk_sonic.isChecked() and 'ema34' in df.columns:
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['ema34'].values, pen=pg.mkPen('#22c55e', width=1.5), name="EMA34"))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['ema89'].values, pen=pg.mkPen('#f59e0b', width=1.5), name="EMA89"))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['ema200'].values, pen=pg.mkPen('#ef4444', width=1.5), name="EMA200"))

            if self.chk_bb.isChecked() and 'bb_upper' in df.columns:
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['bb_upper'].values, pen=pg.mkPen('#94a3b8', width=1, style=Qt.PenStyle.DashLine)))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['bb_mid'].values, pen=pg.mkPen('#cbd5e1', width=1)))
                self.plot_widget.addItem(pg.PlotDataItem(x_indices, df['bb_lower'].values, pen=pg.mkPen('#94a3b8', width=1, style=Qt.PenStyle.DashLine)))

            if self.chk_vol.isChecked() and 'volume' in df.columns:
                vol_max = df['volume'].max()
                price_range = max_price - min_price
                vol_heights = (df['volume'] / vol_max) * (price_range * 0.2) + min_price
                
                brushes = [pg.mkBrush('#0ecb81') if row['close'] >= row['open'] else pg.mkBrush('#f6465d') for _, row in df.iterrows()]
                bg = pg.BarGraphItem(x=x_indices, height=vol_heights - min_price, width=0.6, y0=min_price, brushes=brushes)
                self.plot_widget.addItem(bg)

            current_price = data[-1][4] 
            price_line = pg.InfiniteLine(pos=current_price, angle=0, pen=pg.mkPen('#fcd535', width=1.5))
            self.plot_widget.addItem(price_line)
            
            price_label = pg.TextItem(f"{current_price:,.4f}", color='#181a20', fill='#fcd535', anchor=(0, 0.5))
            price_label.setPos(len(data) + 2, current_price) 
            self.plot_widget.addItem(price_label)

            self.plot_widget.setTitle(f"<span style='color:#e2e8f0;font-size:14px;'>{symbol} - {interval} (Giá: {current_price:,.4f})</span>")

            if hasattr(self, 'active_trades') and symbol in self.active_trades:
                trade = self.active_trades[symbol]
                entry = trade.get("entry", 0)
                tp = trade.get("tp", 0)
                sl = trade.get("sl", 0)
                direction = trade.get("direction", "BUY")

                if isinstance(entry, (int, float)) and entry > 0:
                    color = '#3b82f6' if direction == 'BUY' else '#f97316'
                    self.plot_widget.addItem(pg.InfiniteLine(pos=entry, angle=0, pen=pg.mkPen(color, style=Qt.PenStyle.DashLine, width=2)))
                                
                # ✅ Vẽ TP1 (xanh nhạt, nét nhỏ) — nằm giữa
                tp1 = trade.get("tp1", 0)
                if isinstance(tp1, (int, float)) and tp1 > 0:
                    self.plot_widget.addItem(pg.InfiniteLine(
                        pos=tp1, angle=0,
                        pen=pg.mkPen('#86efac', style=Qt.PenStyle.DashLine, width=1.5)
                    ))
                    tp1_label = pg.TextItem(f"TP1 {tp1:,.4f}", color='#181a20', fill='#86efac', anchor=(0, 0.5))
                    tp1_label.setPos(len(data) + 2, tp1)
                    self.plot_widget.addItem(tp1_label)

                # ✅ Vẽ TP2 (xanh đậm, nét đậm) — điểm chốt cuối
                if isinstance(tp, (int, float)) and tp > 0:
                    self.plot_widget.addItem(pg.InfiniteLine(
                        pos=tp, angle=0,
                        pen=pg.mkPen('#22c55e', style=Qt.PenStyle.DashLine, width=2)
                    ))
                    tp2_label = pg.TextItem(f"TP2 {tp:,.4f}", color='#181a20', fill='#22c55e', anchor=(0, 0.5))
                    tp2_label.setPos(len(data) + 2, tp)
                    self.plot_widget.addItem(tp2_label)
                
                if isinstance(sl, (int, float)) and sl > 0:
                    self.plot_widget.addItem(pg.InfiniteLine(pos=sl, angle=0, pen=pg.mkPen('#ef4444', style=Qt.PenStyle.DashLine, width=2)))

            padding_y = (max_price - min_price) * 0.1
            if padding_y == 0: padding_y = max_price * 0.001
            self.plot_widget.setYRange(min_price - padding_y, max_price + padding_y)
            self.plot_widget.setXRange(-2, len(data) + 10, padding=0) 
            
        except Exception as e:
            log.error(f"Lỗi vẽ nến chi tiết: {e}")   

    def _on_mouse_moved(self, evt):
        try:
            if self.plot_widget.sceneBoundingRect().contains(evt):
                mousePoint = self.plot_widget.getViewBox().mapSceneToView(evt)
                x_val = mousePoint.x()
                y_val = mousePoint.y()

                self.v_line.setPos(x_val)
                self.h_line.setPos(y_val)

                self.label_y.setText(f"{y_val:,.4f}")
                self.label_y.setPos(x_val, y_val)

                x_idx = int(round(x_val))
                if hasattr(self, 'current_chart_times') and 0 <= x_idx < len(self.current_chart_times):
                    time_str = self.current_chart_times[x_idx]
                    self.label_x.setText(time_str)
                    
                    y_min = self.plot_widget.getViewBox().viewRange()[1][0]
                    self.label_x.setPos(x_val, y_min)
        except Exception:
            pass

    def _open_big_chart(self):
        symbol = self.combo_symbols.currentText()
        dialog = ChartDialog(self, self.binance_ui, symbol, symbol_list=self._all_symbols or None)
        dialog.exec()

    def _open_big_chart_by_symbol(self, symbol):
        dialog = ChartDialog(self, self.binance_ui, symbol, symbol_list=self._all_symbols or None)
        dialog.exec()

    def _on_table_coin_clicked(self, row, column, market_type):
        if column == 0:
            tbl = self.tbl_spot if market_type == "SPOT" else self.tbl_future
            item = tbl.item(row, 0)
            if item:
                symbol = item.text().strip().upper()
                self.combo_symbols.setCurrentText(symbol)
                self._fetch_and_draw_chart()

    def _on_table_double_click(self, row, column, market_type):
        if column == 5:
            if self.combo_mode.currentIndex() != 0:
                self._show_msg(
                    QMessageBox.Icon.Warning,
                    "Không thể sửa",
                    "Chỉ sửa Vốn ở chế độ 'Thủ công'.",
                )
                return

            tbl = self.tbl_spot if market_type == "SPOT" else self.tbl_future
            symbol_item = tbl.item(row, 0)
            amount_item = tbl.item(row, 5)
            if symbol_item is None or amount_item is None:
                return
            symbol = symbol_item.text()
            try:
                current_val = float(amount_item.text())
            except (ValueError, TypeError):
                current_val = self.cfg.fixed_amount

            new_val, ok = QInputDialog.getDouble(
                self, "Điều chỉnh vốn",
                f"Nhập số vốn mới cho {symbol} (USDT):",
                current_val, 1.0, 100000.0, 2,
            )
            if ok:
                tbl.setItem(row, 5, QTableWidgetItem(f"{new_val:.2f}"))
                if market_type == "SPOT" and self.worker_spot:
                    self.worker_spot.update_coin_amount(symbol, new_val)
                if market_type == "FUTURE" and self.worker_future:
                    self.worker_future.update_coin_amount(symbol, new_val)

    def _start_finding_coins(self, is_auto=False):
        if self.scanner_thread is not None and self.scanner_thread.isRunning():
            if not is_auto:
                self._log("WARN", "Đang quét coin, vui lòng chờ vòng quét trước kết thúc.")
            return

        self.btn_potential.setEnabled(False)
        self.btn_potential.setText("⏳ Đang quét...")

        # ✅ FIX P1: chọn market_type đúng theo checkbox, không chỉ theo tab
        run_spot = self.chk_run_spot.isChecked()
        run_future = self.chk_run_future.isChecked()
        if run_future and not run_spot:
            mt = "FUTURE"
        elif run_spot and not run_future:
            mt = "SPOT"
        else:
            current_tab = self.tab_trades.currentIndex()
            mt = "SPOT" if current_tab == 0 else "FUTURE"

        self.scanner_thread = CoinScannerThread(self.cfg, is_auto=is_auto, market_type=mt)
        self.scanner_thread.log_signal.connect(self._log)

        if is_auto:
            self.scanner_thread.result_signal.connect(lambda coins: self._on_scan_finished(coins, is_auto=True))
        else:
            self.scanner_thread.result_signal.connect(self._on_scan_finished)

        self.scanner_thread.start()

    def _run_fully_auto(self):
        if self.combo_mode.currentIndex() != 1: return 
        
        if not (self.worker_spot or self.worker_future): return 

        current_positions = self._count_active_positions()
        
        if current_positions >= self.cfg.max_open_positions: 
            self._log("INFO", f"Đã đạt giới hạn {self.cfg.max_open_positions} lệnh. Tạm dừng tìm coin mới.")
            return

        self._log("INFO", "Đang tự động quét tìm coin tiềm năng...")
        self._start_finding_coins(is_auto=True)

    def _on_scan_finished(self, coins: list, is_auto=False):
        # ✅ XÓA SẠCH DỮ LIỆU CŨ TRƯỚC KHI HIỂN THỊ MỚI
        self._clear_potential_layout()
        
        self.btn_potential.setEnabled(True)
        self.btn_potential.setText("🔍 Tìm Coin Tiềm Năng")

        if not coins:
            self._log("WARN", "Không có coin nào đạt tiêu chí.")
            return

        self._log("SUCCESS", f"Tìm thấy {len(coins)} coin tiềm năng.")

        # Duyệt qua danh sách coin mới và vẽ lại
        for line in coins:
            row_w = QWidget()
            h = QHBoxLayout(row_w)
            h.setContentsMargins(2, 1, 2, 1)

            lbl = QLabel(line)
            # Sửa màu chữ thành ĐEN đậm để không bị mất trên nền sáng
            lbl.setStyleSheet("font-size:11px; color: #000000; font-weight: bold;") 
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

            symbol = line.split()[0].strip()
            btn = QPushButton("+")
            btn.setFixedWidth(26)
            btn.setStyleSheet("background:#16a34a;color:white;font-weight:bold;border-radius:3px;")
            btn.clicked.connect(lambda _, s=symbol: self._quick_add(s))

            h.addWidget(lbl, stretch=1)
            h.addWidget(btn)
            
            # Thêm vào layout
            self.potential_layout.addWidget(row_w)

        # Tự động add nếu Full Auto
        if is_auto:
            max_slots = self.cfg.max_open_positions - self._count_active_positions()
            added = 0
            for line in coins:
                if added >= max_slots:
                    break
                symbol = line.split()[0].strip()
                self._quick_add(symbol, silent=True)
                added += 1

    def _quick_add(self, symbol: str, silent: bool = False):
        """Thêm nhanh coin từ bảng tiềm năng vào watchlist."""
        if not silent and not symbol:
            return

        mt = "SPOT" if self.tab_trades.currentIndex() == 0 else "FUTURE"
        worker = self.worker_spot if mt == "SPOT" else self.worker_future
        if worker is None:
            self._log("WARN", "Bot chưa chạy. Nhấn BẮT ĐẦU BOT trước.")
            return

        # Giới hạn slot
        if self._count_watchlist_entries() >= self.cfg.max_open_positions * 2:
            if not silent:
                self._log("WARN", "Watchlist đã đầy.")
            return

        interval = self.combo_timeframe.currentText()
        worker.add_coin(symbol, interval)
        self._add_row_to_table(mt, symbol, interval)
        if not silent:
            self._log("INFO", f"Đã thêm {symbol} ({mt}, {interval}) vào watchlist.")

    def _view_potential_coin(self, symbol):
        self.combo_symbols.setCurrentText(symbol)
        self._fetch_and_draw_chart()

    def _trade_potential_coin(self, symbol):
        self.combo_symbols.setCurrentText(symbol)
        self._fetch_and_draw_chart()
        self._add_coin_to_trade()

    def _start_bot(self) -> None:
        mode_idx = self.combo_mode.currentIndex()
        
        self.cfg.trade_mode = ["FIXED", "FULL_AUTO"][mode_idx]
        
        self.cfg.fixed_amount = self.inp_fixed_amount.value()
        self.cfg.max_open_positions = self.inp_max_positions.value()
        self.cfg.min_signal_score = self.inp_score.value()
        self.cfg.interval = self.combo_timeframe.currentText()
        self.cfg.leverage = self.inp_leverage.value()
        self.cfg.testnet = self.chk_testnet.isChecked()
        self.cfg.save()

        run_spot = self.chk_run_spot.isChecked()
        run_future = self.chk_run_future.isChecked()

        if not run_spot and not run_future:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn ít nhất 1 chế độ (Spot hoặc Futures).")
            return

        # ✅ SỬA LỖI: Kiểm tra API Key linh hoạt, tự động bỏ qua sàn nếu thiếu Key
        has_spot_key = bool(self.cfg.api_key and self.cfg.api_secret)
        has_fut_key = bool(self.cfg.api_key_future and self.cfg.api_secret_future)

        if run_spot and not has_spot_key:
            self._log("WARN", "Không có API Key Spot. Tự động bỏ qua Spot Bot.")
            run_spot = False
            self.chk_run_spot.setChecked(False)

        if run_future and not has_fut_key:
            self._log("WARN", "Không có API Key Futures. Tự động bỏ qua Futures Bot.")
            run_future = False
            self.chk_run_future.setChecked(False)

        if not run_spot and not run_future:
            QMessageBox.warning(self, "Thiếu API Key", "Chưa nhập API Key hợp lệ cho sàn nào. Không thể bật Bot.")
            return

        try:
            if run_spot:
                cfg_spot = copy.deepcopy(self.cfg)
                cfg_spot.api_key = self.cfg.api_key
                cfg_spot.api_secret = self.cfg.api_secret
                self.worker_spot = TradingWorker(cfg_spot, "SPOT")
                self.worker_spot.log_signal.connect(self._log)
                self.worker_spot.balance_signal.connect(self._update_balance)
                self.worker_spot.trade_signal.connect(lambda sym, act, det: self._on_trade("SPOT", sym, act, det))
                self.worker_spot.update_row_signal.connect(lambda sym, st, det: self._update_trade_status("SPOT", sym, st, det))
                self.worker_spot.trade_closed_signal.connect(self._on_trade_closed)
                self.worker_spot.remove_coin_signal.connect(self._remove_coin_from_ui)
                self.worker_spot.start()

            if run_future:
                cfg_fut = copy.deepcopy(self.cfg)
                cfg_fut.api_key = self.cfg.api_key_future
                cfg_fut.api_secret = self.cfg.api_secret_future
                self.worker_future = TradingWorker(cfg_fut, "FUTURE")
                self.worker_future.log_signal.connect(self._log)
                self.worker_future.balance_signal.connect(self._update_balance)
                self.worker_future.trade_signal.connect(lambda sym, act, det: self._on_trade("FUTURE", sym, act, det))
                self.worker_future.update_row_signal.connect(lambda sym, st, det: self._update_trade_status("FUTURE", sym, st, det))
                self.worker_future.trade_closed_signal.connect(self._on_trade_closed)
                self.worker_future.remove_coin_signal.connect(self._remove_coin_from_ui)
                self.worker_future.start()

            if self.cfg.trade_mode == "FULL_AUTO":
                self._log("INFO", "Chế độ FULL_AUTO: Bắt đầu quét tìm coin tự động...")
                QTimer.singleShot(2000, self._run_fully_auto)

            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.combo_mode.setEnabled(False)
            
            self._log("SUCCESS", f"▶️ Đã khởi động Bot (Chế độ: {self.cfg.trade_mode})")
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", str(e))

    def _stop_bot(self) -> None:
        self._log("INFO", "Đang dừng Bot, vui lòng đợi...")
        
        if self.worker_spot:
            try:
                self.worker_spot.stop()
                self.worker_spot.wait(3000)
            except Exception as e:
                self._log("ERROR", f"Lỗi khi dừng Bot Spot: {e}")
            finally:
                self.worker_spot = None
            
        if self.worker_future:
            try:
                self.worker_future.stop()
                self.worker_future.wait(3000)
            except Exception as e:
                self._log("ERROR", f"Lỗi khi dừng Bot Futures: {e}")
            finally:
                self.worker_future = None
            
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.combo_mode.setEnabled(True)
        self.btn_api.setEnabled(True)
        
        self._log("WARN", "⏹️ Tất cả Bot đã dừng hoàn toàn.")


    def _add_coin_to_trade(self):
        current_tab = self.tab_trades.currentIndex() 
        symbol = self.combo_symbols.currentText().strip().upper()
        current_interval = self.combo_timeframe.currentText()
        default_amount = self.inp_fixed_amount.value()

        if not symbol: return

        # Kiểm tra giới hạn
        if self._count_watchlist_entries() >= self.cfg.max_open_positions:
            self._log("WARN", f"Đã đạt giới hạn {self.cfg.max_open_positions} coin theo dõi.")
            return

        # ✅ FIX: Cập nhật giao diện ngay lập tức để chống treo
        if current_tab == 0 and self.worker_spot:
            self.worker_spot.add_coin(symbol, current_interval, default_amount)
            self._add_row_to_table("SPOT", symbol, current_interval, default_amount)
            self._log("INFO", f"Đã thêm {symbol} vào danh sách theo dõi Spot.")
        elif current_tab == 1 and self.worker_future:
            self.worker_future.add_coin(symbol, current_interval, default_amount)
            self._add_row_to_table("FUTURE", symbol, current_interval, default_amount)
            self._log("INFO", f"Đã thêm {symbol} vào danh sách theo dõi Futures.")
        else:
            QMessageBox.warning(self, "Lỗi", "Bot của chế độ này chưa được bật!") 

    def _add_row_to_table(self, market_type: str, symbol: str, interval: str, amount: float = None):
        tbl = self.tbl_spot if market_type == "SPOT" else self.tbl_future
        if tbl is None:
            return

        row_map = self.table_row_map_spot if market_type == "SPOT" else self.table_row_map_future

        if symbol in row_map:
            return

        row = tbl.rowCount()
        tbl.insertRow(row)
        row_map[symbol] = row

        if amount is None:
            amount = self.cfg.fixed_amount

        tbl.setItem(row, 0, QTableWidgetItem(symbol))
        tbl.setItem(row, 1, QTableWidgetItem("Chờ duyệt"))
        tbl.setItem(row, 2, QTableWidgetItem(interval))
        tbl.setItem(row, 3, QTableWidgetItem("-"))
        tbl.setItem(row, 4, QTableWidgetItem("-"))

        if self.combo_mode.currentIndex() != 0:
            tbl.setItem(row, 5, QTableWidgetItem("Auto"))
        else:
            tbl.setItem(row, 5, QTableWidgetItem(f"{amount:.2f}"))

        for i in range(6, 11):
            tbl.setItem(row, i, QTableWidgetItem("-"))

        # Trạng thái auto hiện tại
        is_currently_auto = False
        if market_type == "SPOT" and self.worker_spot and symbol in self.worker_spot.watchlist:
            is_currently_auto = self.worker_spot.watchlist[symbol].get("auto_active", False)
        elif market_type == "FUTURE" and self.worker_future and symbol in self.worker_future.watchlist:
            is_currently_auto = self.worker_future.watchlist[symbol].get("auto_active", False)

        btn_auto = QPushButton("🟢 Đang Auto" if is_currently_auto else "🔴 Đang Tắt")
        if is_currently_auto:
            btn_auto.setStyleSheet("background:#16a34a;color:white;border-radius:4px;padding:2px;")
        else:
            btn_auto.setStyleSheet("background:#dc2626;color:white;border-radius:4px;padding:2px;")

        def toggle_auto():
            is_currently_active = "Đang Auto" in btn_auto.text()
            new_state = not is_currently_active

            if market_type == "SPOT" and self.worker_spot:
                self.worker_spot.toggle_auto(symbol, new_state)
            if market_type == "FUTURE" and self.worker_future:
                self.worker_future.toggle_auto(symbol, new_state)

            if new_state:
                btn_auto.setText("🟢 Đang Auto")
                btn_auto.setStyleSheet("background:#16a34a;color:white;border-radius:4px;padding:2px;")
                tbl.setItem(row, 1, QTableWidgetItem("Đang chờ tín hiệu..."))
            else:
                btn_auto.setText("🔴 Đang Tắt")
                btn_auto.setStyleSheet("background:#dc2626;color:white;border-radius:4px;padding:2px;")
                tbl.setItem(row, 1, QTableWidgetItem("Đã tắt Auto"))

        btn_auto.clicked.connect(toggle_auto)
        tbl.setCellWidget(row, 3, btn_auto)

        # Full Auto: bật auto ngay
        if self.combo_mode.currentIndex() == 1 and not is_currently_auto:
            toggle_auto()

        btn_cancel = QPushButton("Hủy lệnh")
        btn_cancel.setStyleSheet(
            "background:#ef4444;color:white;border-radius:4px;padding:2px;font-weight:bold;"
        )
        btn_cancel.clicked.connect(lambda: self._remove_coin_from_trade(market_type, symbol))
        tbl.setCellWidget(row, 11, btn_cancel)

        btn_view = QPushButton("Xem Chart")
        btn_view.setStyleSheet("background:#f59e0b;color:white;border-radius:4px;padding:2px;")
        btn_view.clicked.connect(lambda: self._open_big_chart_by_symbol(symbol))
        tbl.setCellWidget(row, 12, btn_view)

    def _open_big_chart_by_symbol(self, symbol):
        dialog = ChartDialog(self, self.binance_ui, symbol)
        dialog.exec()


    def _remove_coin_from_trade(self, market_type, symbol):
        reply = QMessageBox.question(
            self, 'Xác nhận Hủy',
            f'Bạn có chắc muốn ĐÓNG VỊ THẾ {symbol} trên sàn và HỦY theo dõi không?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.No:
            return

        try:
            direction = ""
            if market_type == "SPOT" and self.worker_spot and symbol in self.worker_spot.watchlist:
                direction = self.worker_spot.watchlist[symbol].get("direction", "")
                if direction:
                    self.worker_spot._close_position_market(symbol, direction)
            elif market_type == "FUTURE" and self.worker_future and symbol in self.worker_future.watchlist:
                direction = self.worker_future.watchlist[symbol].get("direction", "")
                if direction:
                    self.worker_future._close_position_market(symbol, direction)
        except Exception as e:
            self._log("ERROR", f"Lỗi đóng vị thế {symbol}: {e}")
            return

        # ✅ FIX P1: dùng đúng service theo market_type
        if market_type == "SPOT" and self.worker_spot:
            self.worker_spot.binance.cancel_open_orders(symbol)
        elif market_type == "FUTURE" and self.worker_future:
            self.worker_future.binance.cancel_open_orders(symbol)

        tbl = self.tbl_spot if market_type == "SPOT" else self.tbl_future
        row_map = self.table_row_map_spot if market_type == "SPOT" else self.table_row_map_future

        if market_type == "SPOT" and self.worker_spot: self.worker_spot.remove_coin(symbol)
        if market_type == "FUTURE" and self.worker_future: self.worker_future.remove_coin(symbol)

        if symbol in row_map:
            row = row_map[symbol]
            tbl.removeRow(row)
            del row_map[symbol]
            row_map.clear()
            for r in range(tbl.rowCount()):
                item = tbl.item(r, 0)
                if item:
                    row_map[item.text()] = r

        self._log("SUCCESS", f"Đã đóng vị thế và hủy theo dõi {symbol}")


    def _remove_coin_from_ui(self, market_type: str, symbol: str):
        try:
            if market_type == "SPOT" and self.worker_spot:
                self.worker_spot.remove_coin(symbol)
            elif market_type == "FUTURE" and self.worker_future:
                self.worker_future.remove_coin(symbol)
        except Exception as e:
            self._log("ERROR", f"Lỗi xóa {symbol}: {e}")

        tbl = self.tbl_spot if market_type == "SPOT" else self.tbl_future
        row_map = self.table_row_map_spot if market_type == "SPOT" else self.table_row_map_future

        if symbol in row_map:
            row = row_map[symbol]
            tbl.removeRow(row)
            del row_map[symbol]
            row_map.clear()
            for r in range(tbl.rowCount()):
                item = tbl.item(r, 0)
                if item:
                    row_map[item.text()] = r
        self._log("INFO", f"[{market_type}] Đã xóa {symbol} khỏi danh sách theo dõi.")

    def _update_trade_status(self, market_type: str, symbol: str, status: str, details: dict):
        tbl = self.tbl_spot if market_type == "SPOT" else self.tbl_future
        row_map = self.table_row_map_spot if market_type == "SPOT" else self.table_row_map_future

        if symbol not in row_map:
            interval = details.get("interval", self.cfg.interval)
            self._add_row_to_table(market_type, symbol, interval, self.cfg.fixed_amount)
            # Có thể _add_row_to_table đã thêm row nhưng row_map đã được update
            if symbol not in row_map:
                return

        row = row_map[symbol]
        tbl.setItem(row, 1, QTableWidgetItem(status))
        
        if "amount" in details and float(details["amount"]) > 0:
            tbl.setItem(row, 5, QTableWidgetItem(f"{float(details['amount']):.2f}"))
        
        direction = details.get("direction", "")
        if direction:
            try:
                entry_val = float(details.get("entry", 0) or 0)
                tp_val = float(details.get("tp", 0) or 0)
                sl_val = float(details.get("sl", 0) or 0)
                pnl_usdt = float(details.get("pnl_usdt", 0) or 0)
                pnl_pct = float(details.get("pnl_pct", 0) or 0)
            except (ValueError, TypeError):
                entry_val, tp_val, sl_val, pnl_usdt, pnl_pct = 0.0, 0.0, 0.0, 0.0, 0.0

            tbl.setItem(row, 4, QTableWidgetItem(direction))
            tbl.setItem(row, 6, QTableWidgetItem(f"{entry_val:.4f}" if entry_val > 0 else "-"))

            tp1_val = float(details.get("tp1", 0) or 0)
            if tp1_val > 0 and tp_val > 0:
                tp_display = f"{tp1_val:.4f} / {tp_val:.4f}"
            elif tp_val > 0:
                tp_display = f"{tp_val:.4f}"
            else:
                tp_display = "-"
            tbl.setItem(row, 7, QTableWidgetItem(tp_display))

            tbl.setItem(row, 8, QTableWidgetItem(f"{sl_val:.4f}" if sl_val > 0 else "-"))
            
            item_usdt = QTableWidgetItem(f"{pnl_usdt:+.2f} $")
            item_pct = QTableWidgetItem(f"{pnl_pct:+.2f} %")
            
            if pnl_usdt > 0:
                item_usdt.setForeground(QColor("#16a34a")); item_pct.setForeground(QColor("#16a34a"))
            elif pnl_usdt < 0:
                item_usdt.setForeground(QColor("#dc2626")); item_pct.setForeground(QColor("#dc2626"))
            else:
                item_usdt.setForeground(QColor("#64748b")); item_pct.setForeground(QColor("#64748b"))
                
            tbl.setItem(row, 9, item_usdt)
            tbl.setItem(row, 10, item_pct)

            text_color = QColor("#16a34a") if direction == "BUY" else QColor("#dc2626") 
            for col in [0, 4]:
                item = tbl.item(row, col)
                if item:
                    item.setForeground(text_color)
                    font = item.font(); font.setBold(True); item.setFont(font)
        else:
            for i in [4, 6, 7, 8]: 
                tbl.setItem(row, i, QTableWidgetItem("-"))
                
            item_usdt = QTableWidgetItem("-")
            item_pct = QTableWidgetItem("-")
            item_usdt.setForeground(QColor("#64748b"))
            item_pct.setForeground(QColor("#64748b"))
            
            tbl.setItem(row, 9, item_usdt)
            tbl.setItem(row, 10, item_pct)
            
            for col in [0, 4]:
                item = tbl.item(row, col)
                if item:
                    item.setForeground(QColor("#000000"))
                    font = item.font(); font.setBold(False); item.setFont(font)

    def _on_trade(self, market_type, symbol, action, details):
        direction = details.get("direction", "BUY")
        entry_val = float(details.get("entry", 0.0))
        tp_val = float(details.get("tp", 0.0))
        tp1_val = float(details.get("tp1", 0.0))
        sl_val = float(details.get("sl", 0.0))
        amount_val = float(details.get("amount", 0.0))
        status = "Đã mua (Long)" if direction == "BUY" else "Đã bán (Short)"
        full_details = {
            "direction": direction,
            "entry": entry_val,
            "tp": tp_val,
            "tp1": tp1_val,
            "sl": sl_val,
            "amount": amount_val,
            "pnl_usdt": 0.0,
            "pnl_pct": 0.0
        }
        self._update_trade_status(market_type, symbol, status, full_details)
        self._log("SUCCESS", f"[{market_type}] TRADE {action} {symbol} @ {entry_val}")


    def _on_trade_closed(self, symbol: str, action: str, details: dict):
        # ✅ FIX P0: dùng close_price do worker emit, không gọi API trên UI thread
        close_price = details.get("close_price", 0.0)
        pnl = details.get("pnl", 0.0)
        entry_price = details.get("entry", 0.0)

        result_text = "🟢 THẮNG" if pnl > 0 else "🔴 THUA"
        text_color = QColor("#16a34a") if pnl > 0 else QColor("#dc2626")
        dir_color = QColor("#16a34a") if action == "BUY" else QColor("#dc2626")

        row = self.tbl_history.rowCount()
        self.tbl_history.insertRow(row)

        self.tbl_history.setItem(row, 0, QTableWidgetItem(symbol))

        item_dir = QTableWidgetItem(action)
        item_dir.setForeground(dir_color)
        font = item_dir.font(); font.setBold(True); item_dir.setFont(font)
        self.tbl_history.setItem(row, 1, item_dir)

        self.tbl_history.setItem(row, 2, QTableWidgetItem(f"{entry_price:.4f}" if entry_price > 0 else "-"))
        self.tbl_history.setItem(row, 3, QTableWidgetItem(f"{close_price:.4f}" if close_price > 0 else "-"))

        item_pnl = QTableWidgetItem(f"{pnl:+.2f} $")
        item_pnl.setForeground(text_color)
        self.tbl_history.setItem(row, 4, item_pnl)

        item_result = QTableWidgetItem(result_text)
        item_result.setForeground(text_color)
        self.tbl_history.setItem(row, 5, item_result)

        self._log("WARN", f"{symbol}: Đã đóng lệnh {action}. Kết quả: {result_text} ({pnl:+.2f}$)")

    def _log(self, level: str, msg: str) -> None:
        color_map = {"INFO": "#94a3b8", "SUCCESS": "#22c55e", "WARN": "#f59e0b", "ERROR": "#ef4444", "AI": "#38bdf8"}
        color = color_map.get(level, "#e2e8f0")
        ts = datetime.now().strftime("%H:%M:%S")
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self.txt_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(f"[{ts}] [{level:<7}] {msg}\n", fmt)
        self.txt_log.setTextCursor(cursor); self.txt_log.ensureCursorVisible()

    def _update_balance(self, usdt: float) -> None:
        self.lbl_balance.setText(f"Số dư USDT: {usdt:,.2f}")

    def closeEvent(self, event) -> None:
        self._stop_bot()
        event.accept()

    def _build_history_table_group(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        tbl = QTableWidget(0, 6)
        tbl.setHorizontalHeaderLabels(["Coin", "Buy/Sell", "Giá Vào", "Giá Đóng", "PnL (USDT)", "Kết quả"])
        
        header = tbl.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v.addWidget(tbl)
        
        self.tbl_history = tbl
        return w

    def _start_divergence_scan(self):
        if self.div_scanner is not None and self.div_scanner.isRunning():
            return
        if not hasattr(self, 'btn_scan_div'):
            return  # UI này không được build trong _build_ui — bỏ qua

        self.btn_scan_div.setEnabled(False)
        self.btn_scan_div.setText("⏳ Đang quét...")
        
        if hasattr(self, 'alert_layout'):
            for i in reversed(range(self.alert_layout.count())): 
                widget = self.alert_layout.itemAt(i).widget()
                if widget: widget.setParent(None)

        mt = "SPOT" if self.tab_trades.currentIndex() == 0 else "FUTURE"
        self.div_scanner = DivergenceScannerThread(self.cfg, mt)
        self.div_scanner.log_signal.connect(self._log)
        self.div_scanner.result_signal.connect(self._on_div_scan_finished)
        self.div_scanner.start()

    def _auto_scan_cvd(self):
        # ✅ FIX #17: Guard
        if self.div_scanner is not None and self.div_scanner.isRunning():
            return
        mt = "SPOT" if self.tab_trades.currentIndex() == 0 else "FUTURE"
        self.div_scanner = DivergenceScannerThread(self.cfg, mt)
        self.div_scanner.log_signal.connect(self._log)
        self.div_scanner.result_signal.connect(self._on_cvd_scan_finished)
        self.div_scanner.start()

    def _on_cvd_scan_finished(self, results: list):
        if not results:
            return

        # ✅ FIX P2: xóa CVD cũ trước, tránh layout phình to
        if hasattr(self, '_cvd_widgets'):
            for w in self._cvd_widgets:
                try:
                    w.setParent(None)
                    w.deleteLater()
                except Exception:
                    pass
        self._cvd_widgets = []

        for symbol, div_type in results:
            self._add_to_potential_ui(symbol, div_type)

            if self.combo_mode.currentIndex() != 1:
                continue
            if not (self.worker_spot or self.worker_future):
                continue
            if self.worker_spot and symbol in self.worker_spot.watchlist:
                continue
            if self.worker_future and symbol in self.worker_future.watchlist:
                continue

            if self._count_active_positions() >= self.cfg.max_open_positions:
                self._log("INFO", f"CVD: Đã đạt giới hạn {self.cfg.max_open_positions} lệnh — chỉ hiển thị {symbol}.")
                continue

            self._trade_potential_coin(symbol)

    def _add_to_potential_ui(self, symbol, div_type):
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)
        
        lbl_coin = QLabel(symbol)
        lbl_coin.setStyleSheet("font-weight: bold; color: #38bdf8; font-size: 12px;")
        lbl_coin.setFixedWidth(65)

        color = "#22c55e" if div_type == "BULLISH" else "#ef4444"
        lbl_type = QLabel(div_type)
        lbl_type.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 10px;")
        lbl_type.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        
        btn_view = QPushButton("Xem")
        btn_view.setStyleSheet("background: #3b82f6; color: white; border-radius: 3px; font-size: 10px;")
        btn_view.setFixedSize(32, 20)
        btn_view.clicked.connect(lambda checked, s=symbol: self._view_potential_coin(s))
        
        btn_trade = QPushButton("Trade")
        btn_trade.setStyleSheet("background: #16a34a; color: white; border-radius: 3px; font-size: 10px;")
        btn_trade.setFixedSize(38, 20)
        btn_trade.clicked.connect(lambda checked, s=symbol: self._trade_potential_coin(s))

        row_layout.addWidget(lbl_coin)
        row_layout.addWidget(lbl_type)
        row_layout.addWidget(btn_view)
        row_layout.addWidget(btn_trade)
        
        container = QWidget()
        container.setLayout(row_layout)
        self.potential_layout.addWidget(container)
        # ✅ FIX P2: track để clear lần sau
        if not hasattr(self, '_cvd_widgets'):
            self._cvd_widgets = []
        self._cvd_widgets.append(container)

    def _show_msg(self, icon, title, text, timeout_ms=2500):
        if self._active_msgbox is not None:
            try:
                self._active_msgbox.close()
            except Exception:
                pass
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.show()
        self._active_msgbox = box
        QTimer.singleShot(timeout_ms, box.close)

    def _clear_potential_layout(self):
        while self.potential_layout.count():
            item = self.potential_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
