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
    implicitHeight: preview.y + preview.height
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

    // Макет design/mockups/final/08-onboarding-5-done.html: превью после баннера, отступы 14.
    Rectangle {
        id: preview
        readonly property real padding: 14

        y: note.y + note.height + 14
        width: root.width
        height: previewRow.height + padding * 2
        color: Theme.micLevelMeterBoxBg
        radius: Theme.micLevelMeterBoxRadius
        antialiasing: true

        // design/spec.md §8.2: размеры считаем по пилюле, внешнюю тень Canvas не обрезаем.
        Row {
            id: previewRow
            anchors.centerIn: parent
            height: Math.max(previewPill.pillHeight, previewCaption.height)
            spacing: 14

            Pill {
                id: previewPill
                width: pillWidth
                height: pillHeight
                y: (previewRow.height - height) / 2
                avState: "listening"
                freezeAnimations: true
                // Макет 08-onboarding-5-done.html; §8.3: round(level * 20) даёт 7,12,18,20,14,9,15,11,6.
                levels: [0.35, 0.6, 0.9, 1, 0.7, 0.45, 0.75, 0.55, 0.3]
            }

            Text {
                id: previewCaption
                width: Math.min(250, Math.max(0,
                    preview.width - preview.padding * 2 - previewPill.pillWidth - previewRow.spacing))
                y: (previewRow.height - height) / 2
                text: qsTr("Так выглядит запись: пилюля у нижнего края экрана. Она не забирает фокус и не появляется в Alt+Tab.")
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaptionSize
                lineHeight: Theme.fontCaptionSize * Theme.fontCaptionLineHeight
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }
        }
    }
}
