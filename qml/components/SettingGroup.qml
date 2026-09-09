// Группа настроек — design/spec.md §3.1: серый CAPS-заголовок + карточка со строками.
// Карточка обрезает содержимое (clip), чтобы разделители не торчали за скругление.
import QtQuick 2.15
import ".."

Column {
    id: root

    property string title: ""

    default property alias rows: card.rowData

    spacing: Theme.spaceGroupCaptionGap

    Text {
        text: root.title
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontGroupCapsSize
        font.weight: Font.Medium
        font.capitalization: Font.AllUppercase
        font.letterSpacing: Theme.fontGroupCapsTracking * Theme.fontGroupCapsSize
        renderType: Text.NativeRendering
        verticalAlignment: Text.AlignVCenter
        leftPadding: 2  // §3.1: отступ заголовка группы 0 0 5 2
        lineHeight: Math.round(Theme.fontGroupCapsSize * Theme.fontGroupCapsLineHeight)
        lineHeightMode: Text.FixedHeight
        height: lineHeight
    }

    Rectangle {
        id: card

        property alias rowData: column.data

        width: root.width
        height: Math.round(column.implicitHeight)
        radius: Theme.cardRadius
        color: Theme.bgSurface
        antialiasing: true
        clip: true

        Column {
            id: column
            width: parent.width
            anchors.centerIn: parent
        }

        // Обводка рисуется ПОВЕРХ заливки карточки: в тёмной теме `border` — это
        // 10 % белого, и композит должен считаться от bg-surface, а не от фона окна
        // (иначе рамка получается на два тона темнее макета).
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            color: "transparent"
            border.width: Theme.cardBorder
            border.color: Theme.border
            antialiasing: true
        }
    }
}
