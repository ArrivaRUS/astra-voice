import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

Rectangle {
    id: root
    property string text: ""
    property string iconName: ""
    property string tooltip: text
    property bool iconOnly: false
    property bool primary: false
    property bool keyboardMode: false
    signal clicked()
    implicitWidth: iconOnly ? PillTheme.pillMeetingBarIconBtn : content.width + PillTheme.pillMeetingBarBtnPaddingX * 2
    implicitHeight: PillTheme.pillMeetingBarBtnH
    width: implicitWidth
    height: implicitHeight
    radius: PillTheme.pillMeetingBarBtnRadius
    color: primary ? PillTheme.notifyBtnPrimaryBg : PillTheme.notifyBtnBg
    opacity: enabled ? 1 : 0.5
    activeFocusOnTab: keyboardMode && visible && enabled
    border.width: activeFocus ? 2 : 0
    border.color: PillTheme.pillFg
    Accessible.role: Accessible.Button
    Accessible.name: tooltip
    Accessible.onPressAction: if (enabled) clicked()
    Keys.onReturnPressed: clicked()
    Keys.onEnterPressed: clicked()
    Keys.onSpacePressed: clicked()
    Row {
        id: content
        anchors.centerIn: parent
        spacing: PillTheme.pillMeetingBarBtnGap
        Icon {
            name: root.iconName
            fill: name === "stop" || name === "play"
            // design/meetings/tokens.md §8.2: pause uses a 2.2 stroke.
            strokeWidth: name === "pause" ? 2.2 : 1.5
            visible: name !== ""
            width: PillTheme.pillMeetingBarBtnIcon
            height: width
            anchors.verticalCenter: parent.verticalCenter
            color: root.primary ? PillTheme.notifyBtnPrimaryFg : PillTheme.pillFg
        }
        Item {
            visible: !root.iconOnly
            width: label.implicitWidth * label.scale
            height: label.implicitHeight * label.scale
            Text {
                id: label
                textFormat: Text.PlainText
                text: root.text
                color: root.primary ? PillTheme.notifyBtnPrimaryFg : PillTheme.pillFg
                font.family: "PT Root UI"
                // Qt 5's pixelSize is integral; keep the 12.5 px token without rounding.
                font.pixelSize: PillTheme.pillMeetingBarSize * 2
                font.weight: root.primary ? Font.Medium : Font.Normal
                scale: 0.5
                transformOrigin: Item.TopLeft
            }
        }
    }
    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        onClicked: root.clicked()
    }
    ToolTip.visible: mouse.containsMouse && root.tooltip !== ""
    ToolTip.text: root.tooltip
    ToolTip.delay: 500
}
