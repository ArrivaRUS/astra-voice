// Раздел «Модели» — референс design/refs/02-models-catalog.png (+ -dark),
// спека §5.1–§5.7. Карточка — это ВЫБОР, а не пульт: загрузка начинается по
// «Скачать выбранное», её ход показывает сквозная полоска внизу окна (§10.3).
// Пока каталог отдаёт одну запись; на двенадцать раздел расширит M6.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    readonly property var entries: root.settings ? root.settings.models : []
    readonly property bool hasSelection: root.settings !== null
        && root.settings.selectionSummary !== ""

    implicitHeight: installButton.y + installButton.height

    // Этих полей нет в контракте моста (docs/ui-bridge.md §3.6, §4.3): значения взяты
    // из каталога программы для рекомендованной записи — ровно как на шаге 2 мастера.
    // Как только мост начнёт отдавать vendor/metrics/tags — брать оттуда.
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

    Column {
        id: cards
        width: root.width
        spacing: Theme.modelCardMarginBottom

        Repeater {
            model: root.entries

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
                onToggleRequested: { if (root.settings) root.settings.toggleModel(modelData.id); }
                onRetryRequested: { if (root.settings) root.settings.retryModel(modelData.id); }
                onCancelRequested: { if (root.settings) root.settings.cancelDownloads(); }
                openFolderEnabled: root.settings !== null
                onOpenFolderRequested: { if (root.settings) root.settings.openModelsFolder(); }
            }
        }
    }

    Text {
        id: emptyNote
        y: cards.y + cards.height
        width: root.width
        visible: root.entries.length === 0
        height: visible ? implicitHeight : 0
        textFormat: Text.PlainText
        text: qsTr("Список моделей недоступен. Модель можно поставить из файла или папки.")
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSettingSubSize
        lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    // Строка итога и «Скачать выбранное» — под списком (§5.6).
    RowLayout {
        id: summary
        y: emptyNote.y + emptyNote.height + Theme.onboardingSummaryLineMarginTop
        width: root.width
        spacing: 9 // Макет шага 2: зазор между частями итога.

        Text {
            Layout.maximumWidth: summary.width
            text: root.hasSelection ? root.settings.selectionSummary
                : qsTr("Пока ничего не выбрано")
            textFormat: Text.PlainText
            color: root.settings && root.settings.selectionFits === false
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
            // Пока места хватает, показываем сколько его свободно; при нехватке
            // мост присылает объяснение, и оно важнее (§10.2).
            text: root.settings
                ? (root.settings.selectionMessage !== ""
                    ? root.settings.selectionMessage : root.settings.freeSpaceText)
                : ""
            textFormat: Text.PlainText
            color: root.settings && root.settings.selectionMessage !== ""
                ? Theme.onboardingSummaryLineColorWarn : Theme.onboardingSummaryLineFreeColor
            font.family: Theme.fontUi
            font.pixelSize: Theme.onboardingSummaryLineSize
            renderType: Text.NativeRendering
            wrapMode: Text.WordWrap
        }

        Item { Layout.fillWidth: true }

        AvButton {
            text: qsTr("Скачать выбранное")
            variant: "primary"
            iconName: "down"
            // Пока ничего не отмечено или выбор не помещается — качать нечего (§5.6).
            enabled: root.hasSelection && root.settings.selectionFits
            Layout.alignment: Qt.AlignVCenter
            onClicked: { if (root.settings) root.settings.startSelectedDownloads(); }
        }
    }

    AvButton {
        id: installButton
        y: summary.y + summary.height + 10 // spec §10.2: отступ кнопки.
        iconName: "folder"
        text: qsTr("Установить из файла или папки…")
        enabled: root.settings !== null
        onClicked: { if (root.settings) root.settings.pickInstallPath(); }
    }
}
