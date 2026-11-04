// D2, spec §4.7: safe initial focus and plain meeting title.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

AvDialog {
    id: root
    objectName: "meetingDeleteDialog"
    property var bridge: null
    property string meetingId: ""
    property string meetingTitle: ""
    readonly property var texts: bridge && bridge.deleteTexts !== undefined ? bridge.deleteTexts : ({})
    parent: Overlay.overlay
    brandHeader: true
    heading: texts.heading || ""
    iconName: "trash"
    message: (texts.prefix || "") + meetingTitle + (texts.suffix || "")
        + "\n\n" + (texts.text || "")
    cancelText: texts.cancel || ""
    confirmText: texts.confirm || ""
    confirmVariant: "danger"
    safeConfirmation: true
    onConfirmed: if (bridge) bridge.confirmDelete(meetingId)
}
