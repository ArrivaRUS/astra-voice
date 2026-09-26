// Раздел «Модели» — референс design/refs/02-models-catalog.png (+ -dark),
// спека §5.1–§5.7. Карточка — это ВЫБОР, а не пульт: загрузка начинается по
// «Скачать выбранное», её ход показывает сквозная полоска внизу окна (§10.3).
// Кнопки остались только у установленной модели («Сделать рабочей», «Удалить»,
// «Обновить»); «Установить из файла или папки…» живёт в шапке раздела (§5.6).
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    readonly property var entries: root.settings ? root.settings.models : []
    readonly property bool hasSelection: root.settings !== null
        && root.settings.selectionSummary !== ""

    // Фильтр раздела: «Все языки» или только отечественные модели (§5.6).
    property bool domesticOnly: false

    Component.onDestruction: {
        removeDialog.close();
    }

    function requestRemoval(modelId) {
        for (var i = 0; i < root.entries.length; ++i) {
            var entry = root.entries[i];
            if (entry.id !== modelId)
                continue;
            if (entry.badge === "active")
                return;
            removeDialog.modelId = modelId;
            removeDialog.modelName = entry.name;
            removeDialog.modelSize = entry.sizeText;
            removeDialog.bridge = root.settings;
            removeDialog.heading = qsTr("Удалить %1?").arg(removeDialog.modelName);
            removeDialog.message = qsTr("С диска будет удалено %1 из папки моделей. Настройки и статистика останутся.").arg(removeDialog.modelSize);
            removeDialog.open();
            return;
        }
    }

    readonly property var shownEntries: {
        if (!root.domesticOnly)
            return root.entries;
        var result = [];
        for (var i = 0; i < root.entries.length; ++i) {
            if (root.entries[i].domestic === true)
                result.push(root.entries[i]);
        }
        return result;
    }
    readonly property var installedEntries: root.grouped(true)
    readonly property var availableEntries: root.grouped(false)

    // Установленные и доступные — две группы одного списка (§5.6).
    function grouped(installed) {
        var result = [];
        for (var i = 0; i < root.shownEntries.length; ++i) {
            if ((root.shownEntries[i].badge !== "") === installed)
                result.push(root.shownEntries[i]);
        }
        return result;
    }

    implicitHeight: content.height

    component GroupCaption: Text {
        textFormat: Text.PlainText
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontGroupCapsSize
        font.weight: Font.Medium
        font.capitalization: Font.AllUppercase
        font.letterSpacing: Theme.fontGroupCapsTracking * Theme.fontGroupCapsSize
        renderType: Text.NativeRendering
        leftPadding: 2 // §3.1: отступ заголовка группы 0 0 5 2.
        lineHeight: Math.round(Theme.fontGroupCapsSize * Theme.fontGroupCapsLineHeight)
        lineHeightMode: Text.FixedHeight
        height: lineHeight
    }

    // Инлайн-компонент не видит id окружающего файла, поэтому мост приходит
    // отдельным свойством: обе группы ставят одинаковые карточки.
    component CatalogCard: OnboardingModelCard {
        property var entry: ({})
        property var bridge: null
        property var removalSection: null

        modelId: entry.id !== undefined ? entry.id : ""
        modelTitle: entry.name !== undefined ? entry.name : ""
        purpose: entry.description !== undefined ? entry.description : ""
        host: entry.host !== undefined ? entry.host : ""
        recommended: entry.recommended === true
        sizeText: entry.sizeText !== undefined ? entry.sizeText : ""
        ramText: entry.ramText !== undefined ? entry.ramText : ""
        measurementMode: true
        ramMb: entry.ramMb !== undefined ? entry.ramMb : 0
        ramMeasured: entry.ramMeasured === true
        selected: entry.selected === true
        badge: entry.badge !== undefined ? entry.badge : ""
        cardState: entry.state !== undefined ? entry.state : "available"
        message: entry.message !== undefined ? entry.message : ""
        canCancel: entry.canCancel !== undefined ? entry.canCancel
            : entry.state === "downloading" || entry.state === "paused-no-space"
        canRetry: entry.canRetry !== undefined ? entry.canRetry
            : entry.state === "failed" || entry.state === "paused-no-space"
        canDequeue: entry.canDequeue !== undefined ? entry.canDequeue
            : entry.state === "queued"
        hint: entry.hint !== undefined ? entry.hint : ""
        hintKind: entry.hintKind !== undefined ? entry.hintKind : ""
        memoryShortage: entry.memoryShortage === true
        canReinstall: entry.canReinstall !== false
        canSwitchWithPause: entry.canSwitchWithPause !== undefined
            ? entry.canSwitchWithPause : false
        vendor: entry.vendor !== undefined ? entry.vendor : ""
        vendorShort: entry.vendorShort !== undefined ? entry.vendorShort : vendor
        metrics: entry.metrics !== undefined ? entry.metrics : []
        tags: entry.tags !== undefined ? entry.tags : []
        updateAvailable: entry.updateAvailable === true
        manageEnabled: bridge !== null
        openFolderEnabled: bridge !== null
        onToggleRequested: { if (bridge) bridge.toggleModel(entry.id); }
        onRetryRequested: { if (bridge) bridge.retryModel(entry.id); }
        onCancelRequested: { if (bridge) bridge.cancelModel(entry.id); }
        onDequeueRequested: { if (bridge) bridge.dequeueModel(entry.id); }
        onOpenFolderRequested: { if (bridge) bridge.openModelsFolder(); }
        onActivateRequested: { if (bridge) bridge.makeModelCurrent(entry.id); }
        onSwitchWithPauseRequested: { if (bridge) bridge.switchModelWithPause(entry.id); }
        onReinstallRequested: { if (bridge) bridge.reinstallModel(entry.id); }
        onRemoveRequested: { if (removalSection) removalSection.requestRemoval(entry.id); }
        onUpdateRequested: { if (bridge) bridge.updateModel(entry.id); }
    }

    Column {
        id: content
        width: root.width
        spacing: Theme.modelCardMarginBottom

        // Шапка списка: фильтр слева, счётчик справа (§5.6).
        RowLayout {
            id: filterRow
            objectName: "modelFilterRow"
            width: content.width
            spacing: Theme.chipGap

            FilterChip {
                objectName: "allLanguagesChip"
                label: qsTr("Все языки")
                active: !root.domesticOnly
                onClicked: root.domesticOnly = false
            }

            FilterChip {
                objectName: "domesticChip"
                label: qsTr("Только отечественные")
                active: root.domesticOnly
                onClicked: root.domesticOnly = true
            }

            Item { Layout.fillWidth: true }

            Text {
                Layout.alignment: Qt.AlignVCenter
                textFormat: Text.PlainText
                text: root.settings ? root.settings.installedSummary : ""
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontModelFooterSize
                renderType: Text.NativeRendering
            }
        }

        Column {
            width: content.width
            spacing: Theme.spaceGroupCaptionGap
            visible: root.installedEntries.length > 0

            Item {
                width: parent.width
                height: installedCaption.implicitHeight + Theme.modelCardFirstGroupTopGap - content.spacing

                GroupCaption {
                    id: installedCaption
                    objectName: "installedCaption"
                    anchors.bottom: parent.bottom
                    text: qsTr("Установленные · %1").arg(root.installedEntries.length)
                }
            }

            Column {
                width: parent.width
                spacing: Theme.modelCardMarginBottom

                Repeater {
                    model: root.installedEntries
                    CatalogCard {
                        entry: modelData
                        bridge: root.settings
                        removalSection: root
                    }
                }
            }
        }

        Item {
            visible: root.installedEntries.length > 0 && root.availableEntries.length > 0
            width: 1
            height: Theme.spaceGroupTopGap - 2 * content.spacing
        }

        Column {
            width: content.width
            spacing: Theme.spaceGroupCaptionGap
            visible: root.availableEntries.length > 0

            Item {
                width: parent.width
                height: availableCaption.implicitHeight + (root.installedEntries.length > 0 ? 0 : Theme.modelCardFirstGroupTopGap - content.spacing)

                GroupCaption {
                    id: availableCaption
                    objectName: "availableCaption"
                    anchors.bottom: parent.bottom
                    text: qsTr("Доступные · %1").arg(root.availableEntries.length)
                }
            }

            Column {
                width: parent.width
                spacing: Theme.modelCardMarginBottom

                Repeater {
                    model: root.availableEntries
                    CatalogCard {
                        entry: modelData
                        bridge: root.settings
                        removalSection: root
                    }
                }
            }
        }

        Column {
            width: content.width
            visible: root.entries.length === 0
            spacing: 0

            Text {
                id: emptyNote
                width: parent.width
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

            Text {
                id: revocationUnknownNote
                objectName: "revocationUnknownNote"
                width: parent.width
                visible: root.settings && root.settings.revocationUnknown
                textFormat: Text.PlainText
                text: qsTr("Проверить, не отозвана ли текущая версия модели, сейчас нельзя — программа работает с той моделью, что была выбрана раньше.")
                color: Theme.warningInk
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }
        }

        // Добор до отступа строки итога от списка (§10.2).
        Item {
            width: 1
            height: Theme.onboardingSummaryLineMarginTop - content.spacing
        }

        // Строка итога и «Скачать выбранное» — под списком (§5.6).
        RowLayout {
            id: summary
            width: content.width
            spacing: 9 // Макет шага 2: зазор между частями итога.

            Text {
                objectName: "selectionSummary"
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
                objectName: "selectionMessage"
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
                font.pixelSize: root.settings && root.settings.selectionMessage !== ""
                    ? Theme.onboardingSummaryLineSize : Theme.onboardingSummaryLineFreeSize
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
    }

    AvDialog {
        id: removeDialog
        parent: root.Overlay.overlay ? root.Overlay.overlay : root
        property string modelId: ""
        property string modelName: ""
        property string modelSize: ""
        property var bridge: null
        iconName: "trash"
        note: qsTr("Скачать модель заново можно в любой момент.")
        confirmText: qsTr("Удалить")
        cancelText: qsTr("Отмена")
        onConfirmed: {
            var id = removeDialog.modelId;
            removeDialog.modelId = "";
            if (id !== "" && removeDialog.bridge)
                removeDialog.bridge.removeModel(id);
        }
        onCancelled: removeDialog.modelId = ""
    }

}
