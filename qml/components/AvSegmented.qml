// Сегментированный переключатель — design/spec.md §4.3.
// Высота 30,8; активный сегмент — фон primary; стрелки ← → переключают, кольцо фокуса на всём контроле.
import QtQuick 2.15
import ".."

FocusScope {
    id: root

    property var options: []
    property int currentIndex: 0

    implicitWidth: box.implicitWidth
    implicitHeight: Theme.segmentedHeight
    activeFocusOnTab: true

    Keys.onLeftPressed: if (currentIndex > 0) currentIndex -= 1
    Keys.onRightPressed: if (currentIndex < options.length - 1) currentIndex += 1

    Rectangle {
        id: box
        anchors.fill: parent
        radius: Theme.segmentedRadius
        color: Theme.bgSurface
        border.width: Theme.segmentedBorder
        border.color: Theme.border
        clip: true
        implicitWidth: row.implicitWidth + Theme.segmentedBorder * 2

        Row {
            id: row
            anchors.centerIn: parent

            Repeater {
                model: root.options

                Rectangle {
                    required property int index
                    required property string modelData

                    readonly property bool current: index === root.currentIndex

                    width: text.implicitWidth + Theme.segmentedItemPaddingX * 2
                    height: Theme.segmentedItemH
                    color: current ? Theme.primary
                         : (mouse.containsMouse ? Theme.stateHoverOnSurface : "transparent")

                    Behavior on color {
                        ColorAnimation {
                            duration: Theme.durationHover
                            easing.type: Easing.Bezier
                            easing.bezierCurve: Theme.easingHover.concat([1, 1])
                        }
                    }

                    Text {
                        id: text
                        anchors.centerIn: parent
                        text: parent.modelData
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSegmentedSize
                        font.weight: parent.current ? Font.Medium : Font.Normal
                        renderType: Text.NativeRendering
                        color: !root.enabled ? Theme.fgDisabled
                             : (parent.current ? Theme.primaryFg : Theme.segmentedItemFg)
                    }

                    MouseArea {
                        id: mouse
                        anchors.fill: parent
                        hoverEnabled: true
                        enabled: root.enabled
                        onClicked: {
                            root.currentIndex = parent.index;
                            root.forceActiveFocus();
                        }
                    }
                }
            }
        }
    }

    Rectangle {
        anchors.fill: box
        anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
        radius: Theme.focusRadius
        color: "transparent"
        border.width: Theme.focusWidth
        border.color: Theme.stateFocusRing
        visible: root.activeFocus
    }
}
