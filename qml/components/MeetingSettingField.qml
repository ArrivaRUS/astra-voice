// Meeting field row, design/meetings/tokens.md §2.5; no policy badges in v1.1.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

Item {
    id: root
    property string label: ""
    property string sub: ""
    property string value: ""
    property string placeholder: ""
    property bool rowEnabled: true
    property bool divider: true
    property bool stacked: true
    property bool mono: false
    property bool password: false
    property bool wrapText: false
    property string copyLabel: ""
    property var copyText: null
    property int maximumLength: 500
    property real fieldWidth: Theme.meetingSectionFieldAiW
    property string inputObjectName: objectName + "Input"
    property alias field: input
    signal committed(string value)
    implicitHeight: body.implicitHeight + Theme.cardRowPaddingY * 2 + (divider ? 1 : 0)
    height: implicitHeight
    Rectangle { width: parent.width; height: 1; color: Theme.borderSoft; visible: root.divider }
    Item {
        id: body
        x: Theme.cardRowPaddingX
        y: Theme.cardRowPaddingY + (root.divider ? 1 : 0)
        width: Math.max(0, parent.width - Theme.cardRowPaddingX * 2)
        implicitHeight: root.stacked ? caption.implicitHeight + Theme.meetingSectionRowFieldMarginTop + controls.implicitHeight
                                     : Math.max(caption.implicitHeight, controls.implicitHeight)
        Column {
            id: caption
            width: root.stacked ? body.width : Math.max(0, controls.x - Theme.statusbarGap)
            y: root.stacked ? 0 : Math.round((body.implicitHeight - implicitHeight) / 2)
            Text {
                width: parent.width
                text: root.label
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontBodySize
                color: root.rowEnabled ? Theme.fg : Theme.fgDisabled
                renderType: Text.NativeRendering
            }
            Text {
                width: parent.width
                text: root.sub
                visible: text !== ""
                height: visible ? implicitHeight : 0
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaptionSize
                color: root.rowEnabled ? Theme.fgMuted : Theme.fgDisabled
                renderType: Text.NativeRendering
            }
        }
        Item {
            id: controls
            width: root.stacked ? body.width : Math.min(root.fieldWidth, body.width)
            x: body.width - width
            y: root.stacked ? caption.implicitHeight + Theme.meetingSectionRowFieldMarginTop
                            : Math.round((body.implicitHeight - implicitHeight) / 2)
            implicitHeight: Math.max(input.height, root.copyText ? copyButton.height : 0)
            TextField {
                id: input
                objectName: root.inputObjectName
                anchors.left: parent.left
                anchors.right: root.copyText ? copyButton.left : parent.right
                anchors.rightMargin: root.copyText ? Theme.statusbarGap : 0
                height: root.wrapText ? Math.max(implicitHeight, contentHeight + topPadding + bottomPadding) : implicitHeight
                wrapMode: root.wrapText ? TextInput.Wrap : TextInput.NoWrap
                enabled: root.rowEnabled
                text: root.value
                placeholderText: root.placeholder
                maximumLength: root.maximumLength
                echoMode: root.password ? TextInput.Password : TextInput.Normal
                selectByMouse: true
                font.family: root.mono ? Theme.fontMono : Theme.fontUi
                font.pixelSize: Theme.fontSelectSize
                color: enabled ? Theme.fg : Theme.fgDisabled
                placeholderTextColor: Theme.fgMuted
                leftPadding: Theme.selectPaddingX
                rightPadding: Theme.selectPaddingX
                topPadding: 7
                bottomPadding: 7
                Accessible.name: root.label
                function showStartWhenUnfocused() {
                    if (!activeFocus) cursorPosition = 0
                }
                Component.onCompleted: Qt.callLater(showStartWhenUnfocused)
                onTextChanged: Qt.callLater(showStartWhenUnfocused)
                onActiveFocusChanged: if (!activeFocus) Qt.callLater(showStartWhenUnfocused)
                onEditingFinished: {
                    root.committed(text)
                    // Validation can restore the same effective default without a
                    // notify signal. Rebind after blur so rejected/empty input is
                    // replaced, while later policy updates still reach the field.
                    input.text = Qt.binding(function() { return root.value })
                    Qt.callLater(showStartWhenUnfocused)
                }
                background: Rectangle {
                    radius: Theme.buttonRadius
                    color: input.enabled ? Theme.bgSurface : Theme.bgSurface2
                    border.width: Theme.borderHairline
                    border.color: Theme.border
                    Rectangle {
                        anchors.fill: parent
                        anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                        radius: Theme.focusRadius
                        color: "transparent"
                        border.width: Theme.focusWidth
                        border.color: Theme.stateFocusRing
                        visible: input.activeFocus
                    }
                }
            }
            Loader {
                id: copyButton
                active: root.copyText !== null
                anchors.right: parent.right
                sourceComponent: AvButton {
                    objectName: root.objectName + "Copy"
                    small: true
                    hoverEnabled: true
                    iconName: copyTimer.running ? "check" : "copy"
                    Accessible.name: root.copyLabel
                    ToolTip.text: root.copyLabel
                    ToolTip.visible: hovered && visible
                    ToolTip.delay: 500
                    onClicked: if (root.copyText && root.copyText()) copyTimer.restart()
                    Timer { id: copyTimer; interval: 1200 }
                }
            }
        }
    }
}
