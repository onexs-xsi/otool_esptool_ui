"""Shared QSS stylesheets for OTool Esptool UI.

Both main_window and efuse_dialog share a common visual language.
This module defines the shared base styles; each window appends
its own specific overrides.
"""

# ── 共享基础样式 ──────────────────────────────────────────────────────────────
# 覆盖：背景、文字、输入框、按钮基础态、通用 label 对象名

BASE_STYLESHEET = """
/* ── 全局 ─────────────────────────────────────────────── */
QWidget {
    background: #f0f2f5;
    color: #1a2333;
    font-size: 13px;
}

/* ── 标签通用 ─────────────────────────────────────────── */
QLabel#sectionTitle {
    font-weight: 700;
    color: #1a2333;
}
QLabel#configLabel {
    color: #6b7a94;
    font-weight: 600;
}
QLabel#portBadge {
    background: #e8edf7;
    border: 1px solid #c5cfe8;
    border-radius: 4px;
    padding: 3px 10px;
    color: #2560e0;
    font-weight: 600;
    font-size: 12px;
}
QLabel#deviceSummary {
    color: #6b7a94;
    font-size: 12px;
}

/* ── 输入框 ───────────────────────────────────────────── */
QLineEdit {
    background: #f8f9fb;
    border: 1px solid #dde1ea;
    border-radius: 7px;
    padding: 5px 10px;
    color: #1a2333;
}
QLineEdit:focus {
    border: 1.5px solid #2560e0;
    background: #ffffff;
}

/* ── 按钮基础 ─────────────────────────────────────────── */
QPushButton {
    background: #f0f2f5;
    border: 1px solid #d0d5df;
    border-radius: 7px;
    padding: 5px 12px;
    color: #374151;
    font-weight: 600;
}
QPushButton:hover {
    background: #e4e8f0;
}
QPushButton#primaryButton {
    background: #2560e0;
    border: 1px solid #1a4db5;
    color: #ffffff;
}
QPushButton#primaryButton:hover {
    background: #1a4db5;
}
QPushButton#primaryButton:disabled {
    background: #f5f6f8;
    color: #b0b8cd;
    border-color: #e8eaef;
}
QPushButton#dangerButton {
    background: #fff5f5;
    border: 1px solid #e53935;
    color: #c62828;
}
QPushButton#dangerButton:hover {
    background: #fee2e2;
}
QPushButton#dangerButton:disabled {
    background: #f5f6f8;
    color: #b0b8cd;
    border-color: #e8eaef;
}
QPushButton:disabled {
    background: #f5f6f8;
    color: #b0b8cd;
    border-color: #e8eaef;
}
"""
