from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMessageBox
from qgis.PyQt import QtWidgets


class CollapsibleBox(QtWidgets.QWidget):
    def __init__(self, text, details, title="Mergin error", parent=None):
        msg = QMessageBox()
        msg.setWindowTitle(title)
        msg.setTextFormat(Qt.RichText)
        msg.setText(text)
        msg.setIcon(QMessageBox.Warning)
        msg.setStandardButtons(QMessageBox.Close)
        msg.setDefaultButton(QMessageBox.Close)
        msg.setDetailedText(details)
        msg.exec_()
