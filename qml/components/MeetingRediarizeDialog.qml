// §Р.4, variant A. The confirmation owns an immutable meeting/generation pair.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."

AvDialog {
    id: root
    objectName: "meetingRediarizeDialog"
    property var bridge: null
    property var current: ({})
    property string meetingId: ""
    property int expectedGeneration: -1
    property bool pendingStart: false
    property bool submitting: false
    property string warningTitle: ""
    property string warningText: ""
    property string warningCaption: ""
    readonly property var texts: bridge && bridge.diarizationRedoTexts !== undefined ? bridge.diarizationRedoTexts : ({})
    signal started()
    parent: Overlay.overlay
    brandHeader: true
    heading: texts.warningHeading || "Разделить заново"
    safeConfirmation: true
    width: 420
    iconName: "people"
    cancelText: texts.warningCancel || "Отмена"
    confirmText: texts.again || "Разделить заново"
    function validTarget() {
        return current.meetingId === meetingId && current.expectedGeneration === expectedGeneration
            && current.menuEnabled === true
    }
    function invalidate() { if (visible && !submitting && !validTarget()) close() }
    onCurrentChanged: invalidate()
    function showWarning(snapshot, origin) {
        meetingId = snapshot.meetingId
        expectedGeneration = snapshot.expectedGeneration
        warningTitle = snapshot.warningTitle || ""
        warningText = snapshot.warningText || ""
        warningCaption = snapshot.warningCaption || ""
        returnFocusItem = origin || null
        open()
    }
    function submit() {
        if (!validTarget() || !bridge) { close(); return }
        submitting = true
        var accepted = bridge.rediarizeMeeting(meetingId, expectedGeneration, true)
        pendingStart = accepted
        close()
        submitting = false
    }
    onClosed: if (pendingStart) {
        pendingStart = false
        Qt.callLater(function() { root.started() })
    }
    onOpened: Qt.callLater(function() {
        root.contentItem.contentY = 0
        cancel.focus = false
        root.focusWithReason(cancel, Qt.TabFocusReason)
    })
    function scrollWarning(key) {
        if (root.scrollBody(key)) return true
        var body = root.contentItem
        var end = Math.max(0, body.contentHeight - body.height)
        if (key === Qt.Key_Down) body.contentY = Math.min(end, body.contentY + 19.5)
        else if (key === Qt.Key_Up) body.contentY = Math.max(0, body.contentY - 19.5)
        else return false
        return true
    }
    function halfLeading(lineHeight, fontHeight) { return Math.max(0, (lineHeight - fontHeight) / 2) }
    contentItem: Flickable {
        implicitHeight: Theme.dialogBodyPaddingTop + body.height + Theme.dialogBodyPaddingBottom
        contentHeight: implicitHeight
        contentWidth: width
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}
        Accessible.role: Accessible.Dialog
        Accessible.name: root.heading
        Accessible.description: root.warningTitle + "\n" + root.warningText + "\n" + root.warningCaption
        Icon {
            x: Theme.dialogBodyPaddingX
            y: Theme.dialogBodyPaddingTop
            name: "people"; size: 22; color: Theme.warningInk
        }
        Column {
            id: body
            x: Theme.dialogBodyPaddingX + 22 + Theme.dialogBodyGap
            y: Theme.dialogBodyPaddingTop
            width: parent.width - x - Theme.dialogBodyPaddingX
            spacing: 8
            Item {
                width: parent.width
                height: warningHeading.lineCount * warningHeading.lineHeight * warningHeading.scale
                Text {
                    id: warningHeading
                    objectName: "meetingRedoWarningTitle"
                    y: -0.5 // NativeRendering origin correction, as in the inline row.
                    width: parent.width / scale
                    text: root.warningTitle
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: Theme.fg
                    font.family: Theme.fontUi
                    font.pixelSize: 14
                    font.weight: Font.Medium
                    // Qt5 rounds pixel sizes; preserve 13.5 px and its line box.
                    scale: 13.5 / 14
                    lineHeight: 21; lineHeightMode: Text.FixedHeight
                    // Custom line layout supplies the heading's leading; retain
                    // the half-pixel phase of subsequent scaled lines.
                    onLineLaidOut: if (line.number > 0) line.y += 0.5
                    transformOrigin: Item.TopLeft
                    renderType: Text.NativeRendering
                }
            }
            Text {
                id: warningNames
                objectName: "meetingRedoWarningNames"
                transform: Translate { y: root.halfLeading(19.5, warningNames.font.pixelSize) - 0.5 }
                width: parent.width
                text: root.warningText
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: Theme.fgSecondary
                font.family: Theme.fontUi
                font.pixelSize: 13
                lineHeight: 19.5; lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
            }
            Text {
                id: warningCaptionText
                objectName: "meetingRedoWarningCaption"
                transform: Translate { y: root.halfLeading(19.5, warningCaptionText.font.pixelSize) - 0.5 }
                width: parent.width
                text: root.warningCaption
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: Theme.fgSecondary
                font.family: Theme.fontUi
                font.pixelSize: 13
                lineHeight: 19.5; lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
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
                objectName: "meetingRedoDialogCancel"
                text: root.cancelText
                contentItem: RowLayout {
                    spacing: 0
                    Text {
                        objectName: "meetingRedoCancelLabel"
                        text: cancel.text; textFormat: Text.PlainText
                        font: cancel.font; color: cancel.fgColor
                        renderType: Text.NativeRendering
                        Layout.alignment: Qt.AlignVCenter
                    }
                }
                Keys.onPressed: event.accepted = root.scrollWarning(event.key)
                KeyNavigation.tab: confirm
                KeyNavigation.backtab: confirm
                Keys.onReturnPressed: root.reject()
                Keys.onEnterPressed: root.reject()
                Keys.onEscapePressed: root.reject()
                onClicked: root.reject()
            }
            AvButton {
                id: confirm
                objectName: "meetingRedoDialogConfirm"
                text: root.confirmText
                // Secondary surface and border, only its ink is destructive.
                contentItem: RowLayout {
                    spacing: 0
                    Text {
                        objectName: "meetingRedoConfirmLabel"
                        text: confirm.text; textFormat: Text.PlainText
                        font: confirm.font; color: Theme.dangerInk
                        renderType: Text.NativeRendering
                        Layout.alignment: Qt.AlignVCenter
                    }
                }
                Keys.onPressed: event.accepted = root.scrollWarning(event.key)
                KeyNavigation.tab: cancel
                KeyNavigation.backtab: cancel
                Keys.onReturnPressed: root.submit()
                Keys.onEnterPressed: root.submit()
                Keys.onEscapePressed: root.reject()
                onClicked: root.submit()
            }
        }
    }
}
