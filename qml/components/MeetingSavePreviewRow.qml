// D4: пометка после имени, текст PlainText; 08-marks §6.
import QtQuick 2.15
import ".."

Item {
    id: root
    objectName: "meetingSavePreviewRow"
    property var row: ({})
    property bool first: false
    readonly property bool isMark: row.kind === "mark"
    readonly property real lineH: Theme.mergePopoverTextSize * Theme.fontSmallLineHeight
    height: content.height + Theme.spaceStep * 2 + (first ? 0 : Theme.borderHairline)
    Accessible.role: Accessible.ListItem
    Accessible.name: row.accessibleName || ""
    Rectangle {
        visible: !root.first
        width: parent.width
        height: Theme.borderHairline
        color: Theme.borderSoft
    }
    Text {
        x: 0
        y: Theme.spaceStep
        width: Theme.transcriptColTimeW - Theme.transcriptRowPaddingX
        text: root.row.time || ""
        textFormat: Text.PlainText
        color: Theme.fgMuted
        font.family: Theme.fontMono
        font.pixelSize: Theme.participantsMinutesSize
        renderType: Text.NativeRendering
    }
    Flow {
        id: content
        x: Theme.transcriptColTimeW - Theme.transcriptRowPaddingX + Theme.mergePopoverGap
        y: Theme.spaceStep
        width: Math.max(0, parent.width - x)
        spacing: 0
        Item {
            visible: !root.isMark && !!root.row.num
            width: visible ? previewNumber.width + Theme.speakerNumGap : 0
            height: root.lineH
            MeetingSpeakerNumber {
                id: previewNumber
                objectName: "meetingSavePreviewNumber"
                num: root.row.num || ""
                anchors.verticalCenter: parent.verticalCenter
            }
        }
        Text {
            id: name
            visible: !root.isMark
            width: Math.min(implicitWidth, content.width)
            text: root.row.who || ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.row.whoKind === "me" ? Theme.transcriptMeFg : Theme.fg
            font.family: Theme.fontUi
            font.pixelSize: Theme.mergePopoverTextSize
            font.weight: Font.Medium
            lineHeight: root.lineH
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }
        Text {
            text: root.row.fileMark || ""
            textFormat: Text.PlainText
            color: Theme.fgMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.mergePopoverTextSize
            lineHeight: root.lineH
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }
        Text {
            text: root.isMark ? "" : ": "
            textFormat: Text.PlainText
            color: Theme.fg
            font.family: Theme.fontUi
            font.pixelSize: Theme.mergePopoverTextSize
            lineHeight: root.lineH
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }
        Repeater {
            model: (root.row.text || "").split(/\s+/)
            Text {
                width: Math.min(implicitWidth, content.width)
                text: modelData + " "
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: root.row.textMuted === true ? Theme.transcriptEchoFg : root.isMark ? Theme.fgMuted : Theme.fg
                font.family: Theme.fontUi
                font.pixelSize: Theme.mergePopoverTextSize
                lineHeight: root.lineH
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
            }
        }
    }
}
