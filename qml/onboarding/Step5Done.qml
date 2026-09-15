// design/spec.md §10; шаг 5 версии 0.1, без настройки автозапуска.
import QtQuick 2.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    readonly property string hotkey: bridge && bridge.hotkey ? bridge.hotkey : qsTr("Ctrl + Space")
    property string barHint: ""
    property bool skipEnabled: false

    implicitWidth: 580 // Макет 08-onboarding-5-done.html: ширина содержимого.
    implicitHeight: note.y + note.height
    width: implicitWidth
    height: implicitHeight

    Text {
        id: heading
        width: root.width
        text: qsTr("Всё готово")
        color: Theme.fg
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontH2SectionSize
        font.weight: Font.Bold
        lineHeight: Theme.fontH2SectionSize * Theme.fontH2SectionLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    Text {
        id: subtitle
        y: heading.height + Theme.spaceStep
        width: root.width
        text: qsTr("Осталось запомнить одно сочетание — остальное программа сделает сама.")
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSmallSize
        lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    NoteBanner {
        id: note
        y: subtitle.y + subtitle.height + 14 // Макет 08-onboarding-5-done.html: нижний отступ подзаголовка.
        width: root.width
        variant: "ok"
        iconName: "check"
        title: qsTr("Готово: зажмите %1 и говорите").arg(root.hotkey)
        body: qsTr("Программа свернётся в системный трей — значок слева от часов. Левый клик по значку открывает настройки, правый — меню с отменой и выбором модели.")
    }
}
