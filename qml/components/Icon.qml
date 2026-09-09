// Контурная иконка интерфейса.
// Контуры — собственные (НЕ Breeze), источник: design/mockups/directions/_base.py:261 (_IC),
// правила: design/tokens.json icon.* — viewBox 0 0 16 16, stroke 1.5, fill none,
// скруглённые концы и стыки, цвет = currentColor вызывающего.
import QtQuick 2.15
import QtQuick.Shapes 1.15

Item {
    id: root

    property string name: ""
    property real size: 16
    property color color: "#000000"
    property real strokeWidth: 1.5

    readonly property var paths: ({
        "chev": "M6.5 3.5L11 8l-4.5 4.5",
        "chevd": "M3.5 6.5L8 11l4.5-4.5",
        "check": "M3 8.4l3.4 3.3L13 4.6",
        "down": "M8 2.8v7.4m0 0l-3-3m3 3l3-3M2.8 13.2h10.4",
        "folder": "M2.2 4.4h4.2l1.3 1.7h6.1v7.5H2.2z",
        "refresh": "M13.2 8a5.2 5.2 0 1 1-1.6-3.7M13.4 2.6v2.9h-2.9",
        "file": "M4 2.2h5l3 3v8.6H4zM9 2.2v3.2h3",
        "shield": "M8 2.2l5 1.9v3.7c0 3-2.1 5.3-5 6.1-2.9-.8-5-3.1-5-6.1V4.1z",
        "lock": "M4.4 7.2h7.2v6H4.4zM5.9 7.2V5.4a2.1 2.1 0 0 1 4.2 0v1.8",
        "alert": "M8 2.6l6 10.8H2zM8 6.6v3.1M8 11.4v.1",
        "info": "M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM8 7.4v3.6M8 5.2v.1",
        "cog": "M8 10.1a2.1 2.1 0 1 0 0-4.2 2.1 2.1 0 0 0 0 4.2zM8 1.9v1.7M8 12.4v1.7M2.7 8h1.7M11.6 8h1.7M4.2 4.2l1.2 1.2M10.6 10.6l1.2 1.2M11.8 4.2l-1.2 1.2M5.4 10.6l-1.2 1.2",
        "globe": "M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM2.2 8h11.6M8 2a9 9 0 0 1 0 12A9 9 0 0 1 8 2z",
        "trash": "M3.4 4.6h9.2M6.2 4.6V3.2h3.6v1.4M4.6 4.6l.6 8.4h5.6l.6-8.4",
        "power": "M8 2.4v5.4M4.6 4.4a4.8 4.8 0 1 0 6.8 0",
        "search": "M7.2 12a4.8 4.8 0 1 0 0-9.6 4.8 4.8 0 0 0 0 9.6zM10.8 10.8L13.6 13.6",
        "x": "M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6",
        "chip": "M5.4 5.4h5.2v5.2H5.4zM6.6 2.6v2.8M9.4 2.6v2.8M6.6 10.6v2.8M9.4 10.6v2.8M2.6 6.6h2.8M2.6 9.4h2.8M10.6 6.6h2.8M10.6 9.4h2.8",
        "sliders": "M3 5h10M3 11h10M6.2 3.2v3.6M10.4 9.2v3.6",
        "out": "M6.2 3.2H3.2v9.6h9.6V9.8M9.4 2.8h3.8v3.8M13.2 2.8L7.6 8.4",
        "clock": "M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM8 4.7V8l2.4 1.5"
    })

    implicitWidth: size
    implicitHeight: size
    width: size
    height: size

    Shape {
        width: 16
        height: 16
        antialiasing: true
        transform: Scale { xScale: root.size / 16; yScale: root.size / 16 }

        ShapePath {
            strokeColor: root.color
            fillColor: "transparent"
            strokeWidth: root.strokeWidth
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            PathSvg { path: root.paths[root.name] !== undefined ? root.paths[root.name] : "" }
        }
    }
}
