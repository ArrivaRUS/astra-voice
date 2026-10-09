// Astra Cowork: shared keyboard editor and optional mouse hold/PTT.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."
import "../components"

Column {
    id: root
    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    property bool settingsReady: false
    property var refreshedSettings: null
    signal keyboardModeRequested()
    spacing: Theme.spaceGroupGap

    function initCommandStatus() {
        if (!settingsReady || settings === refreshedSettings)
            return
        refreshedSettings = settings
        if (settings)
            settings.refreshCommandStatus()
    }
    function cancelEditors() {
        mouseSettings.cancelCapture(false)
        if (settings && settings.captureRole === "command")
            settings.cancelCapture()
    }
    onSettingsChanged: initCommandStatus()
    onVisibleChanged: if (!visible) cancelEditors()
    Component.onCompleted: { settingsReady = true; initCommandStatus() }
    Component.onDestruction: cancelEditors()

    Shortcut {
        sequence: "Esc"
        enabled: mouseSettings.captureActive
        onActivated: mouseSettings.cancelCapture(true)
    }

    Timer {
        interval: Theme.durationUptodateMessage
        running: root.settings && root.settings.captureRole === "command" && root.settings.captureState === "success"
        repeat: false
        onTriggered: if (root.settings) root.settings.cancelCapture()
    }

    NoteBanner {
        width: root.width
        visible: root.settings !== null && root.settings.saveError !== ""
        height: visible ? implicitHeight : 0
        variant: "error"
        iconName: "alert"
        title: root.settings ? root.settings.saveError : ""
        body: qsTr("Проверьте, что файл настроек доступен для записи, и попробуйте ещё раз.")
    }

    SettingGroup {
        width: root.width
        title: qsTr("ASTRA COWORK")

        CommandHotkeySettings {
            bridge: root.settings
            rowLabel: qsTr("Клавиша команды")
            editingEnabled: !mouseSettings.captureActive
            onKeyboardModeRequested: root.keyboardModeRequested()
        }
        SettingRow {
            width: parent.width
            label: qsTr("Голосовые команды")
            sub: qsTr("Передавать команды в Astra Cowork")
            showHint: false
            toggle: commandEnabledToggle
            rowEnabled: commandEnabledToggle.enabled
            AvToggle {
                id: commandEnabledToggle
                enabled: root.settings ? root.settings.commandInstalled : false
                muted: !enabled
                Accessible.name: qsTr("Голосовые команды")
                Binding { target: commandEnabledToggle; property: "checked"; value: root.settings ? root.settings.commandEnabled : true }
                onToggled: if (root.settings) root.settings.commandEnabled = checked
            }
        }
        CommandMouseSettings {
            id: mouseSettings
            bridge: root.settings
        }
        SettingRow {
            width: parent.width
            label: qsTr("Показывать команду перед отправкой")
            sub: qsTr("Три секунды для отмены. Esc — не отправлять")
            showHint: false
            toggle: commandPreviewToggle
            rowEnabled: commandPreviewToggle.enabled
            AvToggle {
                id: commandPreviewToggle
                enabled: root.settings ? root.settings.commandInstalled : false
                muted: !enabled
                Accessible.name: qsTr("Показывать команду перед отправкой")
                Binding { target: commandPreviewToggle; property: "checked"; value: root.settings ? root.settings.commandPreview : false }
                onToggled: if (root.settings) root.settings.commandPreview = checked
            }
        }
    }
}
