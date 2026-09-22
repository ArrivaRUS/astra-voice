// Раздел, которого ещё нет: честная строка вместо пустого экрана.
// Пока раздел не сделан, переключение всё равно должно быть настоящим.
import QtQuick 2.15
import ".."

Text {
    property string note: qsTr("Появится в следующей версии")

    textFormat: Text.PlainText
    text: note
    color: Theme.fgMuted
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontSettingSubSize
    lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
    lineHeightMode: Text.FixedHeight
    renderType: Text.NativeRendering
    wrapMode: Text.WordWrap
}
