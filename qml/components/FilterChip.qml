// Чип-фильтр — design/spec.md §4.6: паддинг 4 11, радиус 14, текст 12.5.
// Активный — заливка и граница primary, текст primary-fg, вес 500.
import QtQuick 2.15
import ".."

Rectangle {
    id: root

    property string label: ""
    property bool active: false

    signal clicked()

    implicitWidth: Math.ceil(caption.implicitWidth) + 2 * Theme.chipPaddingX
    implicitHeight: Math.ceil(caption.implicitHeight) + 2 * Theme.chipPaddingY
    radius: Theme.chipRadius
    color: root.active ? Theme.primary
        : mouse.pressed ? Theme.statePressedOnSurface
        : mouse.containsMouse ? Theme.stateHoverOnSurface : Theme.bgSurface
    antialiasing: true
    activeFocusOnTab: true

    Keys.onPressed: {
        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return
                || event.key === Qt.Key_Enter) {
            if (!event.isAutoRepeat)
                root.clicked();
            event.accepted = true;
        }
    }

    Text {
        id: caption
        anchors.centerIn: parent
        textFormat: Text.PlainText
        text: root.label
        color: root.active ? Theme.primaryFg : Theme.fgSecondary
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontChipSize
        font.weight: root.active ? Font.Medium : Font.Normal
        renderType: Text.NativeRendering
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            root.forceActiveFocus(Qt.MouseFocusReason);
            root.clicked();
        }
    }

    // Полупрозрачную обводку смешиваем с заливкой чипа, а не с фоном окна.
    Rectangle {
        anchors.fill: parent
        radius: parent.radius
        color: "transparent"
        border.width: Theme.borderHairline
        border.color: root.active ? Theme.primary : Theme.border
        antialiasing: false
    }

    // Кольцо фокуса: 2 px снаружи, зазор 2 (сквозное правило 1).
    Rectangle {
        anchors.fill: parent
        anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
        radius: Theme.focusRadius
        color: "transparent"
        border.width: Theme.focusWidth
        border.color: Theme.stateFocusRing
        antialiasing: true
        visible: root.activeFocus
    }
}
