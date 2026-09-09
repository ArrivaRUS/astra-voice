// Знак Astra Voice: три точки и строка.
// Геометрия — мастер 22 px (design/spec.md §9.1, design/tokens.json component.tray.geometry-22):
// точки r 1.2185 / 1.6815 / 2.3151 на cy 8.4849, строка 2,12 18×4 rx 2.
// Правило бренда: цвет несёт ТОЛЬКО третья точка; строка и точки 1–2 — цвет текста.
import QtQuick 2.15

Item {
    id: root

    property real size: 22
    property color color: "#000000"
    // Цвет третьей точки: состояние (слушаю/распознаю/готово/ошибка). По умолчанию — как весь знак.
    property color accentColor: root.color

    readonly property real k: size / 22

    implicitWidth: size
    implicitHeight: size
    width: size
    height: size

    Rectangle {
        x: (3.4622 - 1.2185) * root.k
        y: (8.4849 - 1.2185) * root.k
        width: 2 * 1.2185 * root.k
        height: width
        radius: width / 2
        color: root.color
        antialiasing: true
    }

    Rectangle {
        x: (9.4694 - 1.6815) * root.k
        y: (8.4849 - 1.6815) * root.k
        width: 2 * 1.6815 * root.k
        height: width
        radius: width / 2
        color: root.color
        antialiasing: true
    }

    Rectangle {
        x: (14.5627 - 2.3151) * root.k
        y: (8.4849 - 2.3151) * root.k
        width: 2 * 2.3151 * root.k
        height: width
        radius: width / 2
        color: root.accentColor
        antialiasing: true
    }

    Rectangle {
        x: 2 * root.k
        y: 12 * root.k
        width: 18 * root.k
        height: 4 * root.k
        radius: 2 * root.k
        color: root.color
        antialiasing: true
    }
}
