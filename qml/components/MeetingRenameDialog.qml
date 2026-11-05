// D3, spec §4.7 and mockup 06: persist before closing; no invented error copy.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

AvDialog {
    id: root
    objectName: "meetingRenameDialog"
    property var bridge: null
    property string meetingId: ""
    property string meetingTitle: ""
    property bool pending: false
    readonly property var texts: bridge && bridge.renameTexts !== undefined ? bridge.renameTexts : ({})
    brandHeader: true
    parent: Overlay.overlay
    heading: texts.heading || ""
    cancelText: texts.cancel || ""
    confirmText: texts.confirm || ""
    width: Theme.meetingDialogRenameW
    safeConfirmation: true
    confirmEnabled: !pending && input.text.replace(/\s+/g, " ").trim().length > 0

    function focusInput() { if (visible) input.forceActiveFocus(Qt.TabFocusReason) }
    function submit() {
        if (!confirmEnabled || !bridge) return
        pending = true
        var result = bridge.renameMeeting(meetingId, input.text)
        if (result !== "pending") finish(result)
    }
    function finish(result) {
        if (!pending) return
        pending = false
        if (result === "saved") close()
        else Qt.callLater(focusInput)
    }
    Connections {
        target: root.bridge
        ignoreUnknownSignals: true
        function onRenameFinished(result) { root.finish(result) }
    }
    onAboutToShow: input.text = meetingTitle
    onOpened: Qt.callLater(function() { root.focusInput(); input.selectAll() })

    contentItem: Item {
        implicitHeight: Theme.dialogBodyPaddingTop + body.height + Theme.dialogBodyPaddingBottom
        Column {
            id: body
            x: Theme.dialogBodyPaddingX
            y: Theme.dialogBodyPaddingTop
            width: parent.width - Theme.dialogBodyPaddingX * 2
            spacing: Theme.fieldGap
            Text {
                objectName: "meetingRenameLabel"
                height: font.pixelSize * Theme.fontBodyLineHeight
                text: root.texts.label || ""
                textFormat: Text.PlainText
                color: Theme.fg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSelectSize
                renderType: Text.NativeRendering
            }
            // Same TextField styling as MeetingSettingField (§4.5), without settings commits.
            TextField {
                id: input
                objectName: "meetingRenameInput"
                width: parent.width
                // CSS field: 13 × 1.5 line + 7/7 padding + 1/1 border = 35.5 px.
                implicitHeight: font.pixelSize * Theme.fontBodyLineHeight
                    + 2 * Theme.fieldPaddingY + 2 * Theme.fieldBorder
                maximumLength: 120
                selectByMouse: true
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSelectSize
                color: Theme.fg
                selectionColor: Theme.selectionBg
                selectedTextColor: Theme.selectionFg
                leftPadding: Theme.fieldPaddingX
                rightPadding: Theme.fieldPaddingX
                topPadding: Theme.fieldPaddingY
                bottomPadding: Theme.fieldPaddingY
                Accessible.name: root.texts.label || ""
                KeyNavigation.tab: cancel
                KeyNavigation.backtab: save.enabled ? save : cancel
                Keys.onEscapePressed: root.reject()
                onAccepted: root.submit()
                background: Rectangle {
                    radius: Theme.buttonRadius
                    color: Theme.bgSurface
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
        }
    }
    footer: Item {
        implicitHeight: Theme.dialogFooterPaddingTop + buttons.height + Theme.dialogFooterPaddingBottom
        Row {
            id: buttons
            x: root.width - width - Theme.dialogFooterPaddingX
            y: Theme.dialogFooterPaddingTop
            spacing: Theme.dialogFooterGap
            AvButton {
                id: cancel
                objectName: "meetingRenameCancel"
                text: root.cancelText
                variant: "secondary"
                KeyNavigation.tab: save.enabled ? save : input
                KeyNavigation.backtab: input
                Keys.onEscapePressed: root.reject()
                Keys.onReturnPressed: root.reject()
                Keys.onEnterPressed: root.reject()
                onClicked: root.reject()
            }
            AvButton {
                id: save
                objectName: "meetingRenameSave"
                text: root.confirmText
                variant: "primary"
                enabled: root.confirmEnabled
                KeyNavigation.tab: input
                KeyNavigation.backtab: cancel
                Keys.onEscapePressed: root.reject()
                Keys.onReturnPressed: root.submit()
                Keys.onEnterPressed: root.submit()
                onClicked: root.submit()
            }
        }
    }
}
