// «Сеть и обновления»: проверка и результат в одном постоянно видимом блоке.
// AppImage: обе проверки включены по умолчанию; deb: выключены. Политика главнее.
// Тумблер моделей сохраняет выбор для будущей проверки каталога.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Column {
    id: root

    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    readonly property var updates: (typeof updatesBridge !== "undefined" && updatesBridge !== null) ? updatesBridge : null
    function updateValue(name, fallback) {
        return root.updates && typeof root.updates[name] !== "undefined" ? root.updates[name] : fallback;
    }
    function updateCall(name) {
        if (root.updates && typeof root.updates[name] === "function") root.updates[name]();
    }
    function focusUpdatePanel(keyboard) { return updatePanel.focusHeading(keyboard); }
    Component.onCompleted: root.updateCall("refreshCapabilities")
    onUpdatesChanged: Qt.callLater(function() { root.updateCall("refreshCapabilities"); })
    readonly property string downloadPhase: root.updateValue("downloadPhase", "idle")
    readonly property string saveError: root.settings ? root.settings.saveError : ""

    readonly property string updateState: root.updates ? root.updates.state : "disabled"
    readonly property bool manualResult: root.updates ? root.updates.manual : false
    // Отказ гейта для всей сети: "admin" — политика, "offline" — офлайн-режим или окружение.
    readonly property string networkRefusal: root.updates ? root.updates.networkRefusal : ""
    readonly property string checkRefusal: root.updates ? root.updates.checkRefusal : ""
    readonly property bool userOffline: root.settings ? root.settings.offline : false
    readonly property bool offlineOn: root.userOffline || root.networkRefusal !== ""
    // Офлайн включён не пользователем (администратор или окружение запуска) — выключить нельзя.
    readonly property bool offlineForced: root.networkRefusal !== "" && !root.userOffline
    readonly property bool canCheckNow: root.updates ? root.updates.canCheckNow : false

    // Плановая и ручная проверки используют тот же постоянно видимый блок.
    readonly property string panelState: {
        if (root.downloadPhase !== "idle" && root.updateState !== "skipped") return "available";
        switch (root.updateState) {
        case "checking":
            return "checking";
        case "uptodate":
            return root.manualResult ? "uptodate" : "idle";
        case "available":
        case "error-net":
        case "unavailable":
        case "skipped":
            return root.updateState;
        default:
            return "idle";
        }
    }

    function isLocked(name) {
        return settings && settings.lockedSettings
            ? settings.lockedSettings.indexOf(name) >= 0 : false
    }

    spacing: Theme.spaceGroupGap

    NoteBanner {
        width: root.width
        variant: "error"
        iconName: "alert"
        title: root.saveError
        body: qsTr("Проверьте, что файл настроек доступен для записи, и попробуйте ещё раз.")
        visible: root.saveError !== ""
        height: visible ? implicitHeight : 0
    }

    // Макет 04-update-panel: панель → группа 16 = 8 внутри обёртки + 8 зазора колонки
    // (Column пропускает распорки нулевой высоты).
    Item {
        width: root.width
        height: updatePanel.height + Theme.spaceGroupGap

        UpdatePanel {
            id: updatePanel
            objectName: "updatePanel"
            width: root.width
            panelState: root.panelState
            version: root.updates ? root.updates.version : ""
            notes: root.updates ? root.updates.notes : []
            currentVersion: root.updates ? root.updates.currentVersion : ""
            checkedText: root.updates ? root.updates.checkedText : ""
            canCheckNow: root.canCheckNow
            bridgeAvailable: root.updates !== null
            checkRefusal: root.checkRefusal
            checkBlocked: root.updateValue("downloadBusy", false) || root.downloadPhase === "installing"
            downloadPhase: root.downloadPhase
            artifactTrack: root.updateValue("artifactTrack", "")
            artifactSizeText: root.updateValue("artifactSizeText", "")
            downloadPercent: root.updateValue("downloadPercent", 0)
            downloadReceived: root.updateValue("downloadReceived", 0)
            downloadTotal: root.updateValue("downloadTotal", 0)
            downloadBusy: root.updateValue("downloadBusy", false)
            downloadCancelling: root.updateValue("downloadCancelling", false)
            canDownload: root.updateValue("canDownload", false)
            canRetryDownload: root.updateValue("canRetryDownload", false)
            canCancelDownload: root.updateValue("canCancelDownload", false)
            canOpenFolder: root.updateValue("canOpenFolder", false)
            canInstallAndRestart: root.updateValue("canInstallAndRestart", false)
            canSkipVersion: root.updateValue("canSkipVersion", root.updateState === "available")
            downloadError: root.updateValue("downloadError", "")
            downloadErrorText: root.updateValue("downloadErrorText", "")
            downloadRetryText: root.updateValue("downloadRetryText", "")
            folderPath: root.updateValue("folderPath", "")
            adminInstruction: root.updateValue("adminInstruction", "")
            installRefusal: root.updateValue("installRefusal", "")
            installRefusalText: root.updateValue("installRefusalText", "")
            networkRefusal: root.networkRefusal
            onDownloadRequested: root.updateCall("download")
            onCancelRequested: root.updateCall("cancelDownload")
            onRetryRequested: root.updateCall("retryDownload")
            onOpenFolderRequested: root.updateCall("openFolder")
            onInstallRequested: root.updateCall("installAndRestart")
            autoCheck: root.settings ? root.settings.checkAppUpdates && !root.offlineOn : false
            onSkipRequested: { if (root.updates) root.updates.skipVersion(); }
            onCheckRequested: { if (root.updates) root.updates.checkNow(); }
            onShowSkippedRequested: { if (root.updates) root.updates.clearSkip(); }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Сетевые проверки")

        SettingRow {
            id: appRow
            width: parent.width
            divider: false
            label: qsTr("Проверять обновления утилиты")
            sub: locked && !appUpdates.checked ? qsTr("Администратор отключил проверку обновлений на этом компьютере.")
                : root.offlineOn ? qsTr("Не работает, пока включён офлайн-режим.")
                : qsTr("Раз в сутки. Скачивание — только по вашему нажатию.")
            toggle: appUpdates
            locked: root.isLocked("check_app_updates") || root.networkRefusal === "admin"
            rowEnabled: !locked && !root.offlineOn

            AvToggle {
                id: appUpdates
                objectName: "checkAppUpdatesToggle"
                Layout.alignment: Qt.AlignVCenter
                locked: appRow.locked
                // Офлайн перекрывает тумблер: положение сохраняется, переключить нельзя.
                enabled: !appRow.locked && !root.offlineOn

                // Как у автозапуска в «Общих»: положение всегда из моста, клик его не отвязывает.
                Binding {
                    target: appUpdates
                    property: "checked"
                    value: root.settings ? root.settings.checkAppUpdates : false
                }

                onCheckedChanged: {
                    if (!root.settings || !enabled || root.settings.checkAppUpdates === checked)
                        return
                    root.settings.checkAppUpdates = checked
                    // Запись не удалась (saveError) — вернуть положение из моста.
                    if (root.settings.checkAppUpdates !== checked)
                        checked = root.settings.checkAppUpdates
                }
            }
        }

        SettingRow {
            id: modelRow
            width: parent.width
            label: qsTr("Проверять обновления моделей")
            sub: locked && !modelUpdates.checked ? qsTr("Администратор отключил проверку обновлений на этом компьютере.")
                : root.offlineOn ? qsTr("Не работает, пока включён офлайн-режим.")
                : qsTr("Скачивание — только по вашему нажатию.")
            toggle: modelUpdates
            locked: root.isLocked("check_model_updates") || root.networkRefusal === "admin"
            rowEnabled: !locked && !root.offlineOn

            AvToggle {
                id: modelUpdates
                objectName: "checkModelUpdatesToggle"
                Layout.alignment: Qt.AlignVCenter
                locked: modelRow.locked
                enabled: !locked && !root.offlineOn
                Binding {
                    target: modelUpdates
                    property: "checked"
                    value: root.settings ? root.settings.checkModelUpdates : false
                }
                onCheckedChanged: {
                    if (!root.settings || !enabled || root.settings.checkModelUpdates === checked)
                        return
                    root.settings.checkModelUpdates = checked
                    if (root.settings.checkModelUpdates !== checked)
                        checked = root.settings.checkModelUpdates
                }
            }
        }

        SettingRow {
            id: offlineRow
            width: parent.width
            label: qsTr("Офлайн-режим")
            sub: root.offlineForced && root.networkRefusal === "offline"
                ? qsTr("Включён при запуске программы — выключить здесь нельзя")
                : locked ? qsTr("Задано администратором")
                : root.offlineOn ? qsTr("Сетевые кнопки скрыты. Работает установка модели из файла.")
                : qsTr("Полностью запрещает сетевые запросы. Установка модели из файла останется.")
            toggle: offlineToggle
            locked: root.isLocked("offline") || root.networkRefusal === "admin"
            rowEnabled: !locked && !root.offlineForced

            AvToggle {
                id: offlineToggle
                objectName: "offlineToggle"
                Layout.alignment: Qt.AlignVCenter
                locked: offlineRow.locked || root.offlineForced

                Binding {
                    target: offlineToggle
                    property: "checked"
                    value: root.offlineOn
                }

                onCheckedChanged: {
                    if (!root.settings || locked || root.settings.offline === checked)
                        return
                    root.settings.offline = checked
                    // Показываем фактическое состояние из моста: запись могла не удаться, а
                    // офлайн может остаться включённым окружением запуска (тогда он заблокирован).
                    if (checked !== root.offlineOn)
                        checked = root.offlineOn
                }
            }
        }
    }

}
