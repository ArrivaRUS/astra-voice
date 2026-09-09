// Сегментированный переключатель — design/spec.md §4.3, макет `.seg` (_base.py:97).
// CSS даёт `border-radius:7px; overflow:hidden` — заливка активного сегмента обрезается
// ПО СКРУГЛЕНИЮ. В QML `clip` режет по прямоугольнику, поэтому крайние сегменты скругляются
// сами (радиус рамки минус её толщина), а внутренний край выпрямляется накладкой того же цвета.
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
        antialiasing: true
        clip: true
        implicitWidth: row.implicitWidth + Theme.segmentedBorder * 2

        Row {
            id: row
            anchors.centerIn: parent

            Repeater {
                model: root.options

                // Цвет сегмента (и hover, и выбранный) меняется мгновенно — без вспышек.
                Rectangle {
                    id: segment

                    required property int index
                    required property string modelData

                    readonly property bool current: index === root.currentIndex
                    readonly property bool first: index === 0
                    readonly property bool last: index === root.options.length - 1

                    width: text.implicitWidth + Theme.segmentedItemPaddingX * 2
                    height: Theme.segmentedItemH
                    radius: (first || last) ? Theme.segmentedRadius - Theme.segmentedBorder : 0
                    antialiasing: true
                    color: current ? Theme.primary
                         : (mouse.containsMouse ? Theme.stateHoverOnSurface : "transparent")

                    // Внутренний край сегмента остаётся прямым — скругляется только внешний.
                    Rectangle {
                        width: segment.radius
                        height: parent.height
                        x: segment.first ? parent.width - width : 0
                        color: parent.color
                        visible: segment.radius > 0 && !(segment.first && segment.last)
                    }

                    Text {
                        id: text
                        anchors.centerIn: parent
                        text: segment.modelData
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSegmentedSize
                        font.weight: segment.current ? Font.Medium : Font.Normal
                        renderType: Text.NativeRendering
                        color: !root.enabled ? Theme.fgDisabled
                             : (segment.current ? Theme.primaryFg : Theme.segmentedItemFg)
                    }

                    MouseArea {
                        id: mouse
                        anchors.fill: parent
                        hoverEnabled: true
                        enabled: root.enabled
                        // Фокус мышью не берём: кольцо фокуса — только для клавиатуры (§1.3).
                        onClicked: root.currentIndex = segment.index
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
        antialiasing: true
        visible: root.activeFocus
    }
}
