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
    width: Math.min(implicitWidth, parent ? parent.step2AvailableWidth : implicitWidth)
    height: implicitHeight

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

                width: cards.width
                modelId: modelData.id
                modelTitle: modelData.name
                purpose: modelData.description
                host: modelData.host
                recommended: modelData.recommended
                sizeText: modelData.sizeText
                ramText: modelData.ramText
                ramMeasured: modelData.ramMeasured === true
                selected: modelData.selected
                badge: modelData.badge
                cardState: modelData.state
                message: modelData.message
                queuePosition: modelData.queuePosition !== undefined ? modelData.queuePosition : 0
                sourceText: modelData.sourceText !== undefined ? modelData.sourceText : ""
                failReason: modelData.failReason !== undefined ? modelData.failReason : ""
                canCancel: modelData.canCancel !== undefined ? modelData.canCancel
                    : modelData.state === "downloading" || modelData.state === "paused-no-space"
                canRetry: modelData.canRetry !== undefined ? modelData.canRetry
                    : modelData.state === "failed" || modelData.state === "paused-no-space"
                        || modelData.state === "sha-failed"
                canDequeue: modelData.canDequeue !== undefined ? modelData.canDequeue
                    : modelData.state === "queued"
                hint: modelData.hint !== undefined ? modelData.hint : ""
                hintKind: modelData.hintKind !== undefined ? modelData.hintKind : ""
                memoryShortage: modelData.memoryShortage === true
                canReinstall: modelData.canReinstall !== false
                vendor: modelData.vendor
                vendorShort: modelData.vendorShort !== undefined
                    ? modelData.vendorShort : modelData.vendor
                metrics: modelData.metrics
                tags: modelData.tags
                onToggleRequested: { if (root.bridge) root.bridge.toggleModel(modelData.id); }
                onRetryRequested: { if (root.bridge) root.bridge.retryModel(modelData.id); }
                onCancelRequested: { if (root.bridge) root.bridge.cancelModel(modelData.id); }
                onDequeueRequested: { if (root.bridge) root.bridge.dequeueModel(modelData.id); }
                openFolderEnabled: root.bridge !== null
                onOpenFolderRequested: { if (root.bridge) root.bridge.openModelsFolder(); }
            }
        }
    }

    RowLayout {
        id: summary
        y: cards.y + cards.height + Theme.onboardingSummaryLineMarginTop
        width: root.width
        spacing: 9 // Макет шага 2: зазор между частями итога.

        Text {
            objectName: "selectionSummary"
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
            objectName: "selectionMessage"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.maximumWidth: implicitWidth
            visible: text !== ""
            // Пока места хватает, показываем сколько его свободно; при нехватке
            // мост присылает объяснение, и оно важнее (§10.2).
            text: root.bridge
                ? (root.bridge.selectionMessage !== ""
                    ? root.bridge.selectionMessage : root.bridge.freeSpaceText)
                : ""
            textFormat: Text.PlainText
            color: root.bridge && root.bridge.selectionMessage !== ""
                ? Theme.onboardingSummaryLineColorWarn : Theme.onboardingSummaryLineFreeColor
            font.family: Theme.fontUi
            font.pixelSize: root.bridge && root.bridge.selectionMessage !== ""
                ? Theme.onboardingSummaryLineSize : Theme.onboardingSummaryLineFreeSize
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
