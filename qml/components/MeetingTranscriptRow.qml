// Строка таблицы реплик карточки A — design/meetings/tokens.md §3.3, spec §4.7/§7.
// Колонки 76 · 124 · остальное; строка-отметка — 76 · остальное на bgSurface2.
// Текст реплики — Text.PlainText (MN-1).
import QtQuick 2.15
import ".."

Item {
    id: root
    objectName: "meetingCardRow"

    property bool first: false
    property string kind: "utterance" // utterance | mark
    property string time: ""
    property string who: ""
    property string whoKind: ""
    property string text: ""
    property string iconName: ""
    property string accessibleName: ""

    readonly property bool isMark: kind === "mark"
    readonly property real lead: first ? 0 : Theme.transcriptRowDivider

    height: lead + Theme.transcriptRowPaddingY * 2
        + (isMark ? markText.height : Math.max(speech.height, whoText.height + Theme.transcriptWhoOffsetTop))
    Accessible.role: Accessible.ListItem
    Accessible.name: accessibleName

    Rectangle {
        width: parent.width
        height: Theme.transcriptRowDivider
        color: Theme.borderSoft
        visible: !root.first
    }
    Rectangle {
        y: root.lead
        width: parent.width
        height: parent.height - root.lead
        color: Theme.transcriptMarkBg
        visible: root.isMark
    }
    Rectangle {
        // Кольцо текущей реплики при работе с клавиатуры.
        y: root.lead
        width: parent.width
        height: parent.height - root.lead
        color: "transparent"
        border.width: Theme.focusWidth
        border.color: Theme.stateFocusRing
        visible: root.activeFocus
    }
    Text {
        x: Theme.transcriptRowPaddingX
        y: root.lead + Theme.transcriptRowPaddingY + Theme.transcriptTimeOffsetTop
        width: Theme.transcriptColTimeW
        text: root.time
        textFormat: Text.PlainText
        color: Theme.transcriptTimeFg
        font.family: Theme.fontMono
        font.pixelSize: Theme.transcriptTimeSize
        font.letterSpacing: Theme.transcriptTimeSize * Theme.fontMonoInlineTracking
        renderType: Text.NativeRendering
    }
    Text {
        id: whoText
        objectName: "meetingCardWho"
        visible: !root.isMark
        x: Theme.transcriptRowPaddingX + Theme.transcriptColTimeW + Theme.transcriptColGap
        y: root.lead + Theme.transcriptRowPaddingY + Theme.transcriptWhoOffsetTop
        width: Theme.transcriptColWhoW
        text: root.isMark ? "" : root.who
        textFormat: Text.PlainText
        elide: Text.ElideRight
        color: root.whoKind === "me" ? Theme.transcriptMeFg : Theme.fg
        font.family: Theme.fontUi
        font.pixelSize: Theme.transcriptWhoSize
        font.weight: Font.Medium
        renderType: Text.NativeRendering
    }
    Text {
        id: speech
        objectName: "meetingCardText"
        visible: !root.isMark
        x: whoText.x + Theme.transcriptColWhoW + Theme.transcriptColGap
        y: root.lead + Theme.transcriptRowPaddingY
        width: Math.max(0, root.width - x - Theme.transcriptRowPaddingX)
        text: root.isMark ? "" : root.text
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: Theme.fg
        font.family: Theme.fontUi
        font.pixelSize: Theme.transcriptTextSize
        lineHeight: Theme.transcriptTextSize * Theme.transcriptTextLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
    }
    Row {
        visible: root.isMark
        x: whoText.x
        y: root.lead + Theme.transcriptRowPaddingY
        width: Math.max(0, root.width - x - Theme.transcriptRowPaddingX)
        spacing: 4 // макет: пробел между глифом и текстом отметки.
        // Icon (Screen и Image) только у отметок: тысячи таких объектов в строках
        // реплик делали удаление таблицы квадратичным (секунды на 9000 строк).
        Loader {
            y: (Theme.transcriptMarkSize * Theme.transcriptTextLineHeight - height) / 2
            width: Theme.transcriptMarkIcon
            height: Theme.transcriptMarkIcon
            active: root.isMark
            sourceComponent: Icon {
                name: root.iconName
                size: Theme.transcriptMarkIcon
                color: Theme.transcriptMarkFg
            }
        }
        Text {
            id: markText
            objectName: "meetingCardMarkText"
            width: Math.max(0, parent.width - Theme.transcriptMarkIcon - parent.spacing)
            text: root.isMark ? root.text : ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Theme.transcriptMarkFg
            font.family: Theme.fontUi
            font.pixelSize: Theme.transcriptMarkSize
            lineHeight: Theme.transcriptMarkSize * Theme.transcriptTextLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }
    }
}
