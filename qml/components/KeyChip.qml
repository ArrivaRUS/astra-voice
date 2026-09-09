// Чип-клавиша («Ctrl + Space») — design/spec.md §4.7.
// Нижняя граница 2 px читается как физическая клавиша; шрифт — PT Mono (сквозное правило 5).
import QtQuick 2.15
import ".."

Rectangle {
    id: root

    property string text: ""

    implicitHeight: Theme.hotkeyChipHeight
    implicitWidth: label.implicitWidth + Theme.hotkeyChipPaddingX * 2
    radius: Theme.hotkeyChipRadius
    color: Theme.hotkeyChipBg

    Text {
        id: label
        text: root.text
        color: Theme.hotkeyChipFg
        font.family: Theme.fontMono
        font.pixelSize: Theme.fontHotkeyKeySize
        font.letterSpacing: Theme.fontHotkeyKeyTracking * Theme.fontHotkeyKeySize
        renderType: Text.NativeRendering
        anchors.horizontalCenter: parent.horizontalCenter
        y: Theme.hotkeyChipPaddingTop
    }

    // Нижняя грань клавиши: 2 px `border` (component.hotkey-chip.border-bottom).
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: Theme.borderHotkeyKeyBottom
        radius: root.radius
        color: Theme.border
    }
}
