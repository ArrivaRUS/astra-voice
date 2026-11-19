// Итог операции из моста — 4 с, tokens §3.10.
import QtQuick 2.15
import ".."

Rectangle {
    id: root
    objectName: "meetingSpeakerToast"
    property var bridge: null
    property real maxWidth: parent ? parent.width - Theme.toastMaxWInset : 0
    property string message: ""
    visible: false
    width: Math.min(maxWidth, label.implicitWidth + Theme.toastIcon + Theme.toastGap + Theme.toastPaddingLeft + Theme.toastPaddingRight)
    height: Theme.toastH
    radius: Theme.toastRadius
    color: PillTheme.tooltipBg
    z: Theme.popoverZ + 1
    OverlaySurface {
        anchors.fill: parent
        z: -1
        surfaceWidth: parent.width
        surfaceHeight: parent.height
        surfaceRadius: Theme.toastRadius
        surfaceColor: PillTheme.tooltipBg
        shadowX: Theme.shadowToastX
        shadowY: Theme.shadowToastY
        shadowRadius: Theme.shadowToastRadius
        shadowColor: Theme.shadowToastColor
        shadowSourceColor: PillTheme.tooltipBg
    }
    Accessible.role: Accessible.AlertMessage
    Accessible.name: message
    Connections {
        target: root.bridge
        ignoreUnknownSignals: true
        function onSpeakerOperationFinished(code) {
            if (code === "merged" && root.bridge.speakerOperation.ok) {
                root.message = root.bridge.speakerOperation.text
                root.visible = true
                timer.restart()
            }
        }
    }
    Timer { id: timer; interval: Theme.durationMeetingToast; onTriggered: root.visible = false }
    Icon {
        x: Theme.toastPaddingLeft
        anchors.verticalCenter: parent.verticalCenter
        name: "check"
        size: Theme.toastIcon
        color: PillTheme.tooltipFg
    }
    Text {
        id: label
        x: Theme.toastPaddingLeft + Theme.toastIcon + Theme.toastGap
        width: parent.width - x - Theme.toastPaddingRight
        anchors.verticalCenter: parent.verticalCenter
        text: root.message
        textFormat: Text.PlainText
        elide: Text.ElideRight
        color: PillTheme.tooltipFg
        font.family: Theme.fontUi
        font.pixelSize: Theme.toastSize
        renderType: Text.NativeRendering
    }
}
