from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout


class FlowLayout(QLayout):
    """Left-to-right wrapping flow layout."""

    def __init__(self, parent=None, h_spacing: int = 12, v_spacing: int = 12) -> None:
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items: list = []

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = eff.x()
        y = eff.y()
        line_height = 0
        for item in self._items:
            w = item.widget()
            if w is not None and not w.isVisible():
                continue
            hint = item.sizeHint()
            iw, ih = hint.width(), hint.height()
            next_x = x + iw + self._h_spacing
            if next_x - self._h_spacing > eff.right() and line_height > 0:
                x = eff.x()
                y += line_height + self._v_spacing
                next_x = x + iw + self._h_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, ih)
        return y + line_height - rect.y() + m.bottom()
