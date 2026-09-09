// Пункт навигации сайдбара — design/spec.md §1.3.
// Высота 34,3; активный — фон primary; hover/pressed — ступень по поверхности sunk.
// disabled не бывает: все разделы всегда доступны.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

FocusScope {
    id: root

    property string title: ""
    property string iconName: ""
    property string counter: ""
    property bool current: false
    // «Отладка»: вес 400, размер 13, цвет fg-muted (§1.3).
    property bool muted: false

    signal activated()

    // Длинный пункт («Сеть и обновления») переносится и растит строку.
    implicitHeight: Math.max(Theme.sidebarItemH, label.implicitHeight + Theme.sidebarItemPaddingY * 2)
    height: implicitHeight
    activeFocusOnTab: true

    Keys.onReturnPressed: root.activated()
    Keys.onEnterPressed: root.activated()
    Keys.onSpacePressed: root.activated()

    Rectangle {
        id: bg
        anchors.fill: parent
        radius: Theme.sidebarItemRadius
        color: {
            if (root.current)
                return Theme.primary;
            if (mouse.pressed)
                return Theme.statePressedOnSurface2;
            if (mouse.containsMouse)
                return Theme.stateHoverOnSurface2;
            return "transparent";
        }

        Behavior on color {
            ColorAnimation {
                duration: Theme.durationHover
                easing.type: Easing.Bezier
                easing.bezierCurve: Theme.easingHover.concat([1, 1])
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.sidebarItemPaddingX
        anchors.rightMargin: Theme.sidebarItemPaddingX
        spacing: Theme.sidebarItemIconGap

        Icon {
            name: root.iconName
            size: Theme.sidebarItemIcon
            color: root.current ? Theme.primaryFg : (root.muted ? Theme.fgMuted : Theme.fgSecondary)
            Layout.alignment: Qt.AlignVCenter
        }

        Text {
            id: label
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            text: root.title
            color: root.current ? Theme.primaryFg : (root.muted ? Theme.fgMuted : Theme.fgSecondary)
            font.family: Theme.fontUi
            font.pixelSize: root.muted ? Theme.fontNavItemSubSize : Theme.fontNavItemSize
            font.weight: root.muted ? Font.Normal : Font.Medium
            renderType: Text.NativeRendering
            wrapMode: Text.WordWrap
        }

        Text {
            id: counterText
            text: root.counter
            visible: root.counter !== ""
            color: root.current ? Theme.sidebarCounterOnActive : Theme.fgMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontNavCounterSize
            renderType: Text.NativeRendering
            Layout.alignment: Qt.AlignVCenter
        }
    }

    Rectangle {
        anchors.fill: bg
        anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
        radius: Theme.focusRadius
        color: "transparent"
        border.width: Theme.focusWidth
        border.color: Theme.stateFocusRing
        visible: root.activeFocus
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        onClicked: {
            root.forceActiveFocus();
            root.activated();
        }
    }
}
