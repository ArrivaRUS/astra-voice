// design/spec.md §10.5; два вида шага 5 по готовности модели.
import QtQuick 2.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    readonly property string hotkey: root.bridge && root.bridge.hotkey ? root.bridge.hotkey : qsTr("Ctrl + Space")
    readonly property bool modelReady: root.bridge ? root.bridge.modelReady : false
    property string barHint: root.modelReady ? "" : qsTr("Модель ещё загружается")
    property bool skipEnabled: false

    implicitWidth: 580 // Макет шага 5: ширина содержимого.
    implicitHeight: root.modelReady ? preview.y + preview.height : note.y + note.height
    width: implicitWidth
    height: implicitHeight

    Text {
        id: heading
        width: root.width
        textFormat: Text.PlainText
        text: root.modelReady ? qsTr("Всё готово") : qsTr("Почти всё")
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
        textFormat: Text.PlainText
        text: root.modelReady
            ? qsTr("Осталось запомнить одно сочетание — остальное программа сделает сама.")
            : qsTr("Начать можно будет, как только загрузится модель. Окно можно закрыть — загрузка продолжится, мы сообщим, когда всё будет готово.")
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
        y: subtitle.y + subtitle.height + 16 // Макет: схлопнутые отступы подзаголовка и баннера.
        width: root.width
        variant: root.modelReady ? "ok" : "info"
        iconName: root.modelReady ? "check" : "info"
        title: root.modelReady ? qsTr("Готово: зажмите %1 и говорите").arg(root.hotkey)
            : qsTr("Комбинация уже назначена: %1").arg(root.hotkey)
        body: root.modelReady
            ? qsTr("Программа свернётся в системный трей — значок слева от часов. Левый клик по значку открывает настройки, правый — меню с отменой и выбором модели.")
            : qsTr("Как только модель будет готова, зажмите её и говорите — текст появится там, где стоит курсор. Значок в трее покажет, что всё готово.")
    }

    // Макет шага 5: превью после баннера, отступы 14.
    Rectangle {
        id: preview
        visible: root.modelReady
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
                // Макет шага 5; §8.3: round(level * 20) даёт 7,12,18,20,14,9,15,11,6.
                levels: [0.35, 0.6, 0.9, 1, 0.7, 0.45, 0.75, 0.55, 0.3]
            }

            Text {
                id: previewCaption
                width: Math.min(250, Math.max(0,
                    preview.width - preview.padding * 2 - previewPill.pillWidth - previewRow.spacing))
                y: (previewRow.height - height) / 2
                text: qsTr("Так выглядит запись: пилюля у нижнего края экрана.")
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
