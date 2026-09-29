// Раздел «Сеть и обновления» — design/spec.md §6; референсы design/refs/04-network.png и
// 04-update-panel.png (+ -dark). M7-ядро v0.2: тумблер проверки обновлений утилиты,
// офлайн-режим (перекрывает тумблер и скачивание), «Проверить сейчас» и панель «Что нового».
// Не показываем до своих вех: тумблер обновлений моделей (v1.0), «Источники» (M10),
// «Обновить из файла…» и доверенные ключи (M8).
// Без мостов раздел открывается с дефолтами: тумблеры выключены, панели нет.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Column {
    id: root

    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    readonly property var updates: (typeof updatesBridge !== "undefined" && updatesBridge !== null) ? updatesBridge : null
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

    // Панель показывает результат: ручная проверка идёт или закончилась, есть версия,
    // источник недоступен или версия пропущена (§6.1, подмножество v0.2).
    readonly property string panelState: {
        switch (root.updateState) {
        case "checking":
        case "uptodate":
            return root.manualResult ? root.updateState : "";
        case "available":
        case "unavailable":
        case "skipped":
            return root.updateState;
        default:
            return "";
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

    UpdatePanel {
        objectName: "updatePanel"
        width: root.width
        visible: root.panelState !== ""
        height: visible ? implicitHeight : 0
        panelState: root.panelState !== "" ? root.panelState : "available"
        version: root.updates ? root.updates.version : ""
        notes: root.updates ? root.updates.notes : ""
        checkedText: root.updates ? root.updates.checkedText : ""
        releasePageAvailable: root.updates ? root.updates.releasePageAvailable : false
        canCheckNow: root.canCheckNow
        autoCheck: root.settings ? root.settings.checkAppUpdates && !root.offlineOn : false
        onReleasePageRequested: { if (root.updates) root.updates.openReleasePage(); }
        onSkipRequested: { if (root.updates) root.updates.skipVersion(); }
        onRemindLaterRequested: { if (root.updates) root.updates.remindLater(); }
        onCheckRequested: { if (root.updates) root.updates.checkNow(); }
        onShowSkippedRequested: { if (root.updates) root.updates.clearSkip(); }
    }

    // Макет 04-update-panel: между панелью и группами 16 = 8 зазора колонки + 8.
    Item {
        width: 1
        height: Theme.spaceGroupGap
        visible: root.panelState !== ""
    }

    SettingGroup {
        width: root.width
        title: qsTr("Сетевые проверки")

        SettingRow {
            id: appRow
            width: parent.width
            divider: false
            label: qsTr("Проверять обновления утилиты")
            sub: qsTr("Не чаще раза в сутки · github.com/ArrivaRUS/astra-voice")
            toggle: appUpdates
            locked: root.isLocked("check_app_updates") || root.networkRefusal === "admin"
            rowEnabled: !locked && !root.offlineOn

            AvToggle {
                id: appUpdates
                objectName: "checkAppUpdatesToggle"
                Layout.alignment: Qt.AlignVCenter
                checked: root.settings ? root.settings.checkAppUpdates : false
                locked: appRow.locked
                // Офлайн перекрывает тумблер: положение сохраняется, переключить нельзя.
                enabled: !appRow.locked && !root.offlineOn
                onCheckedChanged: {
                    if (root.settings && enabled && root.settings.checkAppUpdates !== checked)
                        root.settings.checkAppUpdates = checked
                }
            }
        }

        SettingRow {
            id: offlineRow
            width: parent.width
            label: qsTr("Офлайн-режим")
            sub: root.offlineForced && root.networkRefusal === "offline"
                ? qsTr("Включён при запуске программы — выключить здесь нельзя")
                : root.offlineOn
                    ? qsTr("Все сетевые кнопки скрыты; работает только установка из файла")
                    : qsTr("Полностью запрещает сетевые запросы; «Установить из файла» остаётся")
            toggle: offlineToggle
            locked: root.isLocked("offline") || root.networkRefusal === "admin"
            rowEnabled: !locked && !root.offlineForced

            AvToggle {
                id: offlineToggle
                objectName: "offlineToggle"
                Layout.alignment: Qt.AlignVCenter
                checked: root.offlineOn
                locked: offlineRow.locked || root.offlineForced
                onCheckedChanged: {
                    if (root.settings && !locked && root.settings.offline !== checked)
                        root.settings.offline = checked
                }
            }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Вручную")

        SettingRow {
            width: parent.width
            divider: false
            showHint: false
            label: qsTr("Проверить обновления сейчас")
            sub: root.checkRefusal === "offline" ? qsTr("Недоступно: включён офлайн-режим")
                : root.checkRefusal === "admin" || root.checkRefusal === "policy"
                    ? qsTr("Недоступно: задано администратором")
                    : qsTr("Ручная проверка работает и при выключенных тумблерах — это ваше явное действие")
            rowEnabled: root.canCheckNow || root.updates === null

            AvButton {
                objectName: "checkNowButton"
                small: true
                iconName: "refresh"
                text: qsTr("Проверить сейчас")
                enabled: root.canCheckNow && root.updateState !== "checking"
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.updates) root.updates.checkNow(); }
            }
        }
    }
}
