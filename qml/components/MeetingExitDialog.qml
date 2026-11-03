// T-478 exit during a recording: design/meetings/spec.md §4.6, dialog §11.1,
// mockup 04-recording.html «7 · Выход во время записи». Modal only to the
// settings window: W2 and the tray stay usable while it is open.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

AvDialog {
    id: root
    objectName: "meetingExitDialog"
    property var texts: ({})
    readonly property real glyph: 22
    brandHeader: true
    heading: texts.header || ""
    message: texts.title || ""
    cancelText: texts.cancel || ""
    confirmText: texts.confirm || ""
    // Focus starts on «Отмена»; Enter there never stops the recording.
    safeConfirmation: true

    contentItem: Flickable {
        objectName: "dialogBody"
        Accessible.role: Accessible.Dialog
        Accessible.name: root.message
        Accessible.description: detail.text
        implicitHeight: Theme.dialogBodyPaddingTop
            + Math.max(root.glyph, column.height) + Theme.dialogBodyPaddingBottom
        contentHeight: implicitHeight
        contentWidth: width
        clip: true
        interactive: false

        Icon {
            id: warning
            x: Theme.dialogBodyPaddingX
            y: Theme.dialogBodyPaddingTop
            size: root.glyph
            name: "alert"
            color: Theme.warningInk
        }

        Column {
            id: column
            x: warning.x + root.glyph + Theme.dialogBodyGap
            y: Theme.dialogBodyPaddingTop
            width: Math.max(0, root.width - x - Theme.dialogBodyPaddingX)
            spacing: 4
            Text {
                objectName: "meetingExitTitle"
                width: parent.width
                text: root.message
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.notificationTitleSize
                font.weight: Font.Medium
                color: Theme.fg
            }
            Text {
                id: detail
                objectName: "meetingExitText"
                width: parent.width
                text: root.texts.text || ""
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                font.family: Theme.fontUi
                font.pixelSize: 13
                color: Theme.fgSecondary
            }
        }
    }
}
