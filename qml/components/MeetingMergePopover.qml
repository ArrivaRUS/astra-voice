// Необратимое объединение: spec §3.7, tokens §3.7.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."

Popup {
    id: root
    objectName: "meetingMergePopover"
    property var bridge: null
    property var source: ({})
    property var returnFocusItem: null
    property var ownerPanel: null
    property real anchorCenterY: 0
    readonly property real viewportMargin: Theme.focusOffset + Theme.focusWidth + Theme.cardBorder * 2
    height: implicitHeight
    function fitViewport() {
        if (!parent) return
        y = Math.round(Math.max(0, Math.min(anchorCenterY - height / 2, parent.height - height - viewportMargin)))
    }
    onHeightChanged: if (visible) Qt.callLater(fitViewport)
    Connections {
        target: root.parent
        function onHeightChanged() { if (root.visible) Qt.callLater(root.fitViewport) }
    }
    readonly property var texts: bridge && bridge.speakerTexts !== undefined ? bridge.speakerTexts : ({})
    readonly property var candidates: source.mergeCandidates || []
    signal merged()
    function txt(key) { return texts[key] || "" }
    width: Theme.mergePopoverW
    padding: Theme.mergePopoverPadding + Theme.cardBorder
    implicitHeight: Math.ceil(contentItem.implicitHeight + topPadding + bottomPadding)
    z: Theme.popoverZ
    modal: true
    dim: false
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    onOpened: {
        choice.currentIndex = 0
        fitViewport()
        Qt.callLater(function() { root.fitViewport(); cancel.focus = false; cancel.forceActiveFocus(Qt.TabFocusReason) })
    }
    onClosed: {
        if (ownerPanel) ownerPanel.highlightedSpeaker = ""
        if (returnFocusItem && returnFocusItem.visible) returnFocusItem.forceActiveFocus(Qt.TabFocusReason)
    }
    background: Rectangle {
        OverlaySurface {
            anchors.fill: parent
            surfaceWidth: parent.width
            surfaceHeight: parent.height
            surfaceRadius: Theme.popoverRadius
            borderWidth: Theme.cardBorder
            borderColor: Theme.border
            surfaceColor: Theme.bgSurface
            shadowX: Theme.shadowMenuX
            shadowY: Theme.shadowMenuY
            shadowRadius: Theme.shadowMenuRadius
            shadowColor: Theme.shadowMenuColor
            shadowSourceColor: Theme.bgSurface
        }
        radius: Theme.popoverRadius
        color: Theme.bgSurface
        border.width: Theme.cardBorder
        border.color: Theme.border
    }
    contentItem: Column {
        Accessible.role: Accessible.Dialog
        Accessible.name: root.source.mergeTitle || ""
        spacing: Theme.mergePopoverGap
        Row {
            width: parent.width
            spacing: Theme.speakerNumGap
            MeetingSpeakerNumber { num: root.source.num || ""; anchors.verticalCenter: parent.verticalCenter }
            Text {
                width: parent.width - (root.source.num ? Theme.speakerNumMinW + Theme.speakerNumGap : 0)
                text: root.source.mergeTitle || ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.mergePopoverTitleSize
                font.weight: Font.Medium
                lineHeight: Theme.mergePopoverTitleSize * Theme.fontBodyLineHeight
                lineHeightMode: Text.FixedHeight
                color: Theme.fg
                renderType: Text.NativeRendering
            }
        }
        Text {
            width: parent.width
            text: root.txt("mergeCaption")
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            font.family: Theme.fontUi
            font.pixelSize: Theme.mergePopoverTextSize
            lineHeight: Theme.mergePopoverTextSize * Theme.fontBodyLineHeight
            lineHeightMode: Text.FixedHeight
            color: Theme.fgMuted
            renderType: Text.NativeRendering
        }
        AvSelect {
            id: choice
            objectName: "meetingMergeTarget"
            width: parent.width
            model: root.candidates.map(function(p) { return p.label })
            itemDetails: root.candidates
            speakerNumbers: true
            Accessible.name: root.txt("mergeTarget")
        }
        Row {
            width: parent.width
            spacing: Theme.spaceStep
            Icon {
                y: (Theme.mergePopoverWarnSize * Theme.fontBodyLineHeight - height) / 2
                name: "alert"
                size: Theme.mergePopoverWarnIcon
                color: Theme.warningInk
            }
            Text {
                width: parent.width - Theme.mergePopoverWarnIcon - parent.spacing
                text: root.txt("irreversible")
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.mergePopoverWarnSize
                lineHeight: Theme.mergePopoverWarnSize * Theme.fontBodyLineHeight
                lineHeightMode: Text.FixedHeight
                color: Theme.warningInk
                renderType: Text.NativeRendering
            }
        }
        Row {
            anchors.right: parent.right
            spacing: Theme.mergePopoverGap
            AvButton {
                id: cancel
                objectName: "meetingMergeCancel"
                Keys.onReturnPressed: if (enabled) clicked()
                Keys.onEnterPressed: if (enabled) clicked()
                text: root.txt("cancel")
                small: true
                onClicked: root.close()
            }
            AvButton {
                objectName: "meetingMergeConfirm"
                Keys.onReturnPressed: if (enabled) clicked()
                Keys.onEnterPressed: if (enabled) clicked()
                text: root.txt("confirm")
                small: true
                variant: "primary"
                enabled: root.candidates.length > 0
                onClicked: {
                    if (root.bridge && root.bridge.mergeSpeakers(root.source.id, root.candidates[choice.currentIndex].id)) root.close()
                }
            }
        }
    }
}
