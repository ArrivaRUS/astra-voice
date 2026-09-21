// design/spec.md §10.2: выбор моделей.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    property string barHint: root.bridge && root.bridge.selectionFits === false
        ? qsTr("Освободите место или выберите модель полегче")
        : root.bridge && root.bridge.canContinueFromModel === false && root.bridge.selectionFits === true
            ? qsTr("Выберите хотя бы одну модель — без неё диктовка не работает") : ""
    property bool skipEnabled: false

    implicitWidth: Theme.onboardingStep2ContentW
    implicitHeight: installButton.y + installButton.height
    width: implicitWidth
    height: implicitHeight

    // Этих полей нет в контракте моста (docs/ui-bridge.md 4.3): значения взяты
    // из каталога программы для рекомендованной записи. Как только мост начнёт
    // отдавать vendor/metrics/tags — брать оттуда.
    function catalogDetails(entry) {
        if (entry.recommended !== true)
            return { vendor: "", metrics: [], tags: [] };
        return {
            vendor: qsTr("Сбер (GigaChat Team)"),
            metrics: [
                { label: qsTr("Качество"), fill: 0.90, text: qsTr("WER 7,60 %"), hasData: true },
                { label: qsTr("Скорость"), fill: 0.50, text: qsTr("42,5× быстрее речи"), hasData: true }
            ],
            tags: [qsTr("Только русский"), qsTr("с пунктуацией"), qsTr("MIT · Сбер"), qsTr("отечественная")]
        };
    }

    Text {
        id: heading
        width: root.width
        text: qsTr("Выберите модель распознавания")
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
        y: heading.height + 4 // Макет шага 2: отступ подзаголовка.
        width: root.width
        text: qsTr("Отметьте, что скачать. Рекомендуем русскую GigaAM: она расставляет знаки препинания сама. Загрузка начнётся, когда нажмёте «Продолжить».")
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSmallSize
        lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    Column {
        id: cards
        y: subtitle.y + subtitle.height + 14 // Макет шага 2: нижний отступ подзаголовка.
        width: root.width
        spacing: Theme.modelCardMarginBottom

        Repeater {
            model: root.bridge ? root.bridge.models : []

            OnboardingModelCard {
                required property var modelData
                readonly property var details: root.catalogDetails(modelData)

                width: cards.width
                modelId: modelData.id
                modelTitle: modelData.name
                purpose: modelData.description
                host: modelData.host
                recommended: modelData.recommended
                sizeText: modelData.sizeText
                ramText: modelData.ramText
                selected: modelData.selected
                badge: modelData.badge
                cardState: modelData.state
                message: modelData.message
                vendor: details.vendor
                metrics: details.metrics
                tags: details.tags
                onToggleRequested: { if (root.bridge) root.bridge.toggleModel(modelData.id); }
                onRetryRequested: { if (root.bridge) root.bridge.retryModel(modelData.id); }
                onCancelRequested: { if (root.bridge) root.bridge.cancelDownloads(); }
            }
        }
    }

    RowLayout {
        id: summary
        y: cards.y + cards.height + Theme.onboardingSummaryLineMarginTop
        width: root.width
        spacing: 9 // Макет шага 2: зазор между частями итога.

        Text {
            Layout.maximumWidth: summary.width
            text: root.bridge && root.bridge.selectionSummary !== ""
                ? root.bridge.selectionSummary : qsTr("Пока ничего не выбрано")
            textFormat: Text.PlainText
            color: root.bridge && root.bridge.selectionFits === false
                ? Theme.onboardingSummaryLineColorWarn : Theme.onboardingSummaryLineColor
            font.family: Theme.fontUi
            font.pixelSize: Theme.onboardingSummaryLineSize
            renderType: Text.NativeRendering
            wrapMode: Text.WordWrap
        }

        Rectangle {
            visible: selectionMessage.visible
            Layout.preferredWidth: 4
            Layout.preferredHeight: 4
            radius: 2
            color: Theme.fgFaint
        }

        Text {
            id: selectionMessage
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.maximumWidth: implicitWidth
            visible: text !== ""
            text: root.bridge ? root.bridge.selectionMessage : ""
            textFormat: Text.PlainText
            color: Theme.onboardingSummaryLineFreeColor
            font.family: Theme.fontUi
            font.pixelSize: Theme.onboardingSummaryLineSize
            renderType: Text.NativeRendering
            wrapMode: Text.WordWrap
        }

        Item { Layout.fillWidth: true }
    }

    AvButton {
        id: installButton
        y: summary.y + summary.height + 10 // spec §10.2: отступ кнопки.
        iconName: "folder"
        text: qsTr("Установить из файла или папки…")
        onClicked: { if (root.bridge) root.bridge.pickInstallPath(); }
    }
}
