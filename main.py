"""
main.py — Entry point của ứng dụng.
Chạy: python main.py
"""
import logging
import sys

from PyQt6.QtWidgets import QApplication

from config import Config
from ui.main_window import MainWindow


def setup_logging() -> None:
    """Logging toàn cục — mọi module dùng chung 1 format."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    setup_logging()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Load cấu hình từ file bot_config.json
    cfg = Config.load()
    
    # Khởi tạo và hiển thị cửa sổ chính
    win = MainWindow(cfg)
    win.show()
    
    # Chạy vòng lặp sự kiện của PyQt6
    sys.exit(app.exec())


if __name__ == "__main__":
    main()