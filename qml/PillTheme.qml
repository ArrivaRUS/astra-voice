// СГЕНЕРИРОВАНО scripts/gen_theme.py из design/tokens.json 2.1.1 — НЕ ПРАВИТЬ РУКАМИ.
// Перегенерация: python3 scripts/gen_theme.py · проверка: python3 scripts/gen_theme.py --check

pragma Singleton
import QtQuick 2.15

QtObject {
    // ── color.fixed: рисуется поверх чужого рабочего стола, с темой НЕ меняется
    readonly property color appiconAccent: "#2FD9C4"
    readonly property color appiconBg: "#1B3A73"
    readonly property color appiconMark: "#F2F5FA"
    readonly property color notifyBg: "#1B2331"
    readonly property color notifyBorder: Qt.rgba(1, 1, 1, 0.12)
    readonly property color notifyBtnBg: Qt.rgba(1, 1, 1, 0.1)
    readonly property color notifyBtnPrimaryBg: "#6C93E8"
    readonly property color notifyBtnPrimaryFg: "#0B1220"
    readonly property color notifyFg: "#F2F5FA"
    readonly property color notifyFgMuted: "#8C97AC"
    readonly property color pillBarFlat: "#8C97AC"
    readonly property color pillBarLive: "#12B3A0"
    readonly property color pillBg: Qt.rgba(0.054902, 0.090196, 0.160784, 0.94)
    readonly property color pillCloseBg: Qt.rgba(1, 1, 1, 0.12)
    readonly property color pillCloseFg: "#C4CDDC"
    readonly property color pillDone: "#4FBF88"
    readonly property color pillError: "#F0645F"
    readonly property color pillFg: "#F2F5FA"
    readonly property color pillIconMuted: "#C4CDDC"
    readonly property color pillIconNeutral: "#8C97AC"
    readonly property color pillProcessing: "#E8A33A"
    readonly property color pillWarning: "#F2B559"
    readonly property color tooltipBg: "#0E1729"
    readonly property color tooltipFg: "#F2F5FA"

    // ── component.pill
    readonly property real pillBarAreaH: 20
    readonly property real pillBarGap: 3
    readonly property real pillBarHFlat: 4
    readonly property real pillBarHMax: 20
    readonly property real pillBarHMin: 3
    readonly property real pillBarRadius: 3
    readonly property real pillBarW: 6
    readonly property int pillBars: 9
    readonly property real pillBarsBlockW: 78
    readonly property real pillGap: 9
    readonly property real pillH: 36
    readonly property real pillMaxW: 320
    readonly property real pillMeasuredWListening: 187.6
    readonly property real pillMinW: 172
    readonly property real pillRadius: 18
}
