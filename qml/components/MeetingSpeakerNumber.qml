// Нейтральный номер первого появления — tokens.md §3.2.
import QtQuick 2.15
import ".."

Rectangle {
    id: root
    property string num: ""
    visible: num !== ""
    width: Math.max(Theme.speakerNumMinW, digit.implicitWidth + Theme.speakerNumPaddingX * 2)
    height: Theme.speakerNumH
    radius: Theme.speakerNumRadius
    color: Theme.speakerNumBg
    border.width: Theme.speakerNumBorder
    border.color: Theme.speakerNumBorderColor
    Accessible.ignored: true
    Text {
        id: digit
        anchors.centerIn: parent
        anchors.verticalCenterOffset: Theme.speakerNumTextOffsetY
        text: root.num
        textFormat: Text.PlainText
        color: Theme.speakerNumFg
        font.family: Theme.fontMono
        font.pixelSize: Theme.speakerNumSize
        font.weight: Font.Bold // CSS 700 → Qt5 75, not a CSS numeric weight.
        renderType: Text.NativeRendering
    }
}
