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
        leftPadding: 2  // §3.1: отступ заголовка группы 0 0 5 2
    }

    Rectangle {
        id: card

        property alias rowData: column.data

        width: root.width
        height: column.implicitHeight
        radius: Theme.cardRadius
        color: Theme.bgSurface
        border.width: Theme.cardBorder
        border.color: Theme.border
        clip: true

        Column {
            id: column
            width: parent.width
            anchors.centerIn: parent
        }
    }
}
