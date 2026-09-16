// Знак Astra Voice: три точки и строка. Правило бренда — цвет несёт ТОЛЬКО третья точка.
//
// Три РАЗНЫЕ геометрии, их нельзя путать (все из design/mockups/directions/_base.py):
//   logo — знак в интерфейсе, `mark()`: viewBox 100 × 51.7238, ширина знака = вся ширина;
//   tray — иконка состояния, `tray()` и design/spec.md §9.1: viewBox 22 × 22 со своей
//          мастер-геометрией (уменьшать логотип до 16–22 px нельзя, радиусы «плывут»).
//   appicon — плитка приложения, `_base.py:227 appicon()`: viewBox 64 × 64.
import QtQuick 2.15

Item {
    id: root

    property real size: 22
    property bool tray: false
    property bool appicon: false
    property color color: "#000000"
    property color tileColor: "transparent"
    // Третья точка: состояние (слушаю / распознаю / готово / ошибка) либо акцент бренда.
    property color accentColor: root.color

    readonly property real vbW: appicon ? 64 : tray ? 22 : 100
    readonly property real vbH: appicon ? 64 : tray ? 22 : 51.7238
    readonly property real k: size / vbW
    // cx, cy, r в координатах viewBox
    readonly property var dots: appicon
        ? [[9.2776, 24.88, 2.8776], [23.4642, 24.88, 3.9711], [35.4926, 24.88, 5.4674]]
        : tray
            ? [[3.4622, 8.4849, 1.2185], [9.4694, 8.4849, 1.6815], [14.5627, 8.4849, 2.3151]]
            : [[8.1233, 12.8619, 6.7694], [41.4966, 12.8619, 9.3418], [69.7928, 12.8619, 12.8619]]
    // x, y, w, h, rx
    readonly property var bar: appicon ? [5.12, 32.7474, 54.4, 8, 4]
        : tray ? [2, 12, 18, 4, 2] : [0, 31.7238, 100, 20, 10]

    implicitWidth: size
    implicitHeight: vbH * k
    width: implicitWidth
    height: implicitHeight

    Rectangle {
        visible: root.appicon
        width: 64 * root.k
        height: 64 * root.k
        radius: 14.08 * root.k
        color: root.tileColor
        antialiasing: true
    }

    Repeater {
        model: root.dots

        Rectangle {
            required property int index
            required property var modelData

            x: (modelData[0] - modelData[2]) * root.k
            y: (modelData[1] - modelData[2]) * root.k
            width: 2 * modelData[2] * root.k
            height: width
            radius: width / 2
            antialiasing: true
            color: index === 2 ? root.accentColor : root.color
        }
    }

    Rectangle {
        x: root.bar[0] * root.k
        y: root.bar[1] * root.k
        width: root.bar[2] * root.k
        height: root.bar[3] * root.k
        radius: root.bar[4] * root.k
        antialiasing: true
        color: root.color
    }
}
