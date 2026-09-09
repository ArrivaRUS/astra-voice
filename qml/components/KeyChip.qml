// Чип-клавиша («Ctrl + Space») — design/spec.md §4.7, макет `.key` (_base.py:100).
// CSS: border-radius 6 + border-bottom 2 px — нижняя грань читается как физическая клавиша
// и СКРУГЛЯЕТСЯ вместе с корпусом. В QML это два скруглённых слоя: нижний цвета `border`,
// верхний — фон чипа с отступом снизу на толщину грани (рисовать полоску поверх нельзя:
// она срезала бы нижние углы, дефект «нет скруглений» с живого прогона).
import QtQuick 2.15
import ".."

Rectangle {
    id: root

    property string text: ""

    implicitHeight: Theme.hotkeyChipHeight
    implicitWidth: label.implicitWidth + Theme.hotkeyChipPaddingX * 2
    radius: Theme.hotkeyChipRadius
    color: Theme.border
    antialiasing: true

    Rectangle {
        anchors.fill: parent
        anchors.bottomMargin: Theme.borderHotkeyKeyBottom
        radius: root.radius
        color: Theme.hotkeyChipBg
        antialiasing: true
    }

    Text {
        id: label
        text: root.text
        color: Theme.hotkeyChipFg
        font.family: Theme.fontMono
        font.pixelSize: Theme.fontHotkeyKeySize
        font.letterSpacing: Theme.fontHotkeyKeyTracking * Theme.fontHotkeyKeySize
        renderType: Text.NativeRendering
        verticalAlignment: Text.AlignVCenter
        // Центрируем в ВИДИМОЙ части корпуса — над нижней гранью 2 px, иначе текст
        // уезжает вверх (дефект живого прогона: центр глифов был на 1 px выше центра плашки).
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: -Theme.borderHotkeyKeyBottom / 2
    }
}
