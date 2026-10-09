// Раздел «Общие» — референс design/refs/01-general-base.png (+ -dark), спека §3.
// Семь настроек в трёх группах; модель переехала в раздел «Модели».
// Без моста настроек используются дефолты M1.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Column {
    id: root

    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    readonly property string hotkeyStatus: settings ? settings.hotkeyStatus : "ok"
    readonly property string saveError: root.settings ? root.settings.saveError : ""
    readonly property string soundCuesStatus: root.settings ? root.settings.soundCuesStatus : ""
    readonly property string micError: root.settings ? root.settings.microphoneError : ""
    readonly property string modelSelfcheck: root.settings ? root.settings.modelSelfcheck : ""

    // Громкость микрофона (PRD 0.9 F6.6/F6.7, ревизия 19): читаем по открытию
    // раздела и после действия; ползунок меняет громкость, кнопка возвращает прежнюю.
    readonly property bool canRaiseMic: root.settings ? root.settings.canRaiseMicrophone : false
    readonly property bool canRestoreMic: root.settings ? root.settings.canRestoreMicrophoneVolume : false
    readonly property bool canOpenSoundSettings: root.settings ? root.settings.canOpenSoundSettings : false
    readonly property bool micMuted: root.settings ? root.settings.microphoneMuted : false
    readonly property int micVolume: root.settings ? root.settings.microphoneVolume : -1

    // SettingsBridge подключается и после engine.load(): первый показ должен
    // прочитать состояние, а ранний мост — сделать это только один раз.
    property bool settingsReady: false
    property var refreshedSettings: null

    function refreshSettings() {
        if (!root.settingsReady || root.settings === root.refreshedSettings)
            return
        root.refreshedSettings = root.settings
        if (root.settings) {
            root.settings.refreshDevices()
            root.settings.refreshMicrophone()
        }
    }

    onSettingsChanged: root.refreshSettings()
    Component.onCompleted: {
        root.settingsReady = true
        root.refreshSettings()
    }
    Component.onDestruction: {
        if (root.settings)
            root.settings.cancelCapture()
    }

    Timer {
        interval: Theme.durationUptodateMessage
        running: root.settings && root.settings.captureState === "success"
        repeat: false
        onTriggered: {
            if (root.settings)
                root.settings.cancelCapture()
        }
    }

    // Список микрофонов из моста (как в шаге «Микрофон» мастера): имена в списке,
    // идентификаторы — в объектах; выбор ищем по идентификатору.
    readonly property var devices: (root.settings && root.settings.devices.length > 0)
        ? root.settings.devices
        : [{ id: "", name: qsTr("Системный по умолчанию") }]

    function deviceNames() {
        var names = []
        for (var i = 0; i < root.devices.length; ++i)
            names.push(root.devices[i].name)
        return names
    }

    function deviceIndex(deviceId) {
        for (var i = 0; i < root.devices.length; ++i) {
            if (root.devices[i].id === deviceId)
                return i
        }
        return -1
    }

    function deviceIdAt(index) {
        return index >= 0 && index < root.devices.length ? root.devices[index].id : ""
    }

    function focusKeyboardMode() {
        modeSelector.forceActiveFocus(Qt.TabFocusReason)
        return modeSelector
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

    NoteBanner {
        width: root.width
        variant: "error"
        iconName: "alert"
        title: root.micError
        visible: root.micError !== ""
        height: visible ? implicitHeight : 0
    }

    Text {
        width: root.width
        text: root.modelSelfcheck === "running" ? qsTr("Проверяю модель…")
            : root.modelSelfcheck === "failed" ? qsTr("Распознавание на этом компьютере не работает. Обратитесь к администратору")
            : ""
        visible: root.modelSelfcheck === "running" || root.modelSelfcheck === "failed"
        height: visible ? implicitHeight : 0
        color: root.modelSelfcheck === "failed" ? Theme.dangerInk : Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSettingSubSize
        lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
    }

    SettingGroup {
        width: root.width
        title: qsTr("Диктовка")

        // Кнопки «Повторить» нет по решению заказчика от 15.09.2026 (PRD 0.8 F2.11):
        // бэкенд выполняет автоповтор раз в 30 секунд.
        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Горячая клавиша")
            sub: qsTr("Удерживайте и говорите — текст появится там, где курсор")
            locked: root.isLocked("hotkey")

            Text {
                textFormat: Text.PlainText
                text: root.hotkeyStatus === "busy" ? qsTr("Занята другой программой")
                    : root.hotkeyStatus === "not-grabbed" ? qsTr("Горячая клавиша не захвачена")
                    : root.hotkeyStatus === "bad-combo" ? qsTr("Такое сочетание не подходит — выберите другое")
                    : ""
                visible: text !== ""
                color: root.hotkeyStatus === "not-grabbed" ? Theme.dangerInk : Theme.warningInk
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
                Layout.maximumWidth: root.width / 4
                Layout.alignment: Qt.AlignVCenter
            }

            KeyChip {
                text: root.settings ? root.settings.hotkey : qsTr("Ctrl + Space")
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                text: qsTr("Изменить")
                small: true
                enabled: !root.isLocked("hotkey")
                Layout.alignment: Qt.AlignVCenter
                onClicked: {
                    if (root.settings && root.settings.beginCapture)
                        root.settings.beginCapture()
                }
            }
        }

        CaptureField {
            id: hotkeyCapture
            width: parent.width
            visible: state7 !== "idle"
            height: visible ? implicitHeight : 0
            showIdleRow: false
            state7: root.settings && root.settings.captureRole === "text" ? root.settings.captureState : "idle"
            hotkey: root.settings ? root.settings.hotkey : qsTr("Ctrl + Space")
            captureMessage: root.settings ? root.settings.captureMessage : ""
            pendingCombo: root.settings ? root.settings.pendingCombo : ""
            freeCandidates: root.settings ? root.settings.freeCandidates : []

            onState7Changed: {
                if ((state7 === "conflict" || state7 === "duplicate" || state7 === "not-grabbed")
                        && root.settings && root.settings.refreshCandidates)
                    root.settings.refreshCandidates()
            }
            onChangeRequested: {
                if (root.settings && root.settings.beginCapture)
                    root.settings.beginCapture()
            }
            onCancelRequested: {
                if (root.settings && root.settings.cancelCapture)
                    root.settings.cancelCapture()
            }
            onComboCaptured: {
                if (root.settings && root.settings.endCapture)
                    root.settings.endCapture(combo)
            }
            onChooseAnotherRequested: {
                if (root.settings && root.settings.beginCapture)
                    root.settings.beginCapture()
            }
            onRetryRequested: {
                if (root.settings && root.settings.beginCapture)
                    root.settings.beginCapture()
            }
            onKeepRequested: {
                if (root.settings && root.settings.keepCombo)
                    root.settings.keepCombo()
            }
            onToggleModeRequested: {
                if (root.settings)
                    root.settings.hotkeyMode = "toggle"
            }
        }

        SettingRow {
            width: parent.width
            label: qsTr("Режим")
            // Подпись вернулась вместе с переездом модели в раздел «Модели»:
            // без строки модели раздел снова помещается без прокрутки.
            sub: qsTr("Удерживать — самый удобный и предсказуемый вариант")
            locked: root.isLocked("hotkey_mode")

            AvSegmented {
                id: modeSelector
                options: [qsTr("Удерживать"), qsTr("Нажать-нажать")]
                enabled: !root.isLocked("hotkey_mode")
                Layout.alignment: Qt.AlignVCenter

                Binding {
                    target: modeSelector
                    property: "currentIndex"
                    value: (root.settings && root.settings.hotkeyMode === "toggle") ? 1 : 0
                }

                onCurrentIndexChanged: {
                    var mode = currentIndex === 1 ? "toggle" : "ptt"
                    if (root.settings && root.settings.hotkeyMode !== mode)
                        root.settings.hotkeyMode = mode
                }
            }
        }

        // Local microphone row: one fixed control axis, secondary messages below it.
        // Other SettingRow instances keep their existing layout.
        Item {
            id: deviceRow
            readonly property string label: qsTr("Микрофон")
            readonly property bool locked: root.isLocked("device")
            width: parent.width
            implicitHeight: Theme.spaceCardRowDivider + Theme.cardRowPaddingY * 2
                + micAxis.height + (policyBadge.visible ? 4 + policyBadge.height : 0)
                + (micStatus.visible ? 4 + micStatus.implicitHeight : 0)
            height: implicitHeight

            Rectangle {
                width: parent.width
                height: Theme.spaceCardRowDivider
                color: Theme.borderSoft
            }

            Item {
                id: micAxis
                x: Theme.cardRowPaddingX
                y: Theme.spaceCardRowDivider + Theme.cardRowPaddingY
                width: parent.width - Theme.cardRowPaddingX * 2
                height: deviceSelector.implicitHeight
                readonly property int controlCount: 2 + (micSlider.visible ? 2 : 0)
                    + (raiseMic.visible ? 1 : 0) + (restoreMic.visible ? 1 : 0)
                    + (soundSettings.visible ? 1 : 0)
                readonly property real fixedWidth: micLabel.width
                    + (volumeText.visible ? volumeText.width : 0)
                    + (raiseMic.visible ? raiseMic.width : 0)
                    + (restoreMic.visible ? restoreMic.width : 0)
                    + (soundSettings.visible ? soundSettings.width : 0)
                    + (controlCount - 1) * Theme.fieldGap
                readonly property real available: Math.max(0, width - fixedWidth)
                readonly property real selectorWidth: Math.min(236,
                    Math.max(120, available - (micSlider.visible ? 80 : 0)))
                readonly property real sliderWidth: Math.min(120, Math.max(80, available - selectorWidth))
                readonly property real stretch: Math.max(0, available - selectorWidth
                    - (micSlider.visible ? sliderWidth : 0))
                function after(item) { return item.x + item.width + Theme.fieldGap }
                readonly property real volumeEnd: micSlider.visible
                    ? after(volumeText) : after(deviceSelector)

                Text {
                    id: micLabel
                    width: Math.ceil(implicitWidth)
                    text: deviceRow.label
                    textFormat: Text.PlainText
                    color: Theme.fg
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSettingLabelSize
                    lineHeight: Math.round(Theme.fontSettingLabelSize * Theme.fontSettingLabelLineHeight)
                    lineHeightMode: Text.FixedHeight
                    renderType: Text.NativeRendering
                    anchors.verticalCenter: parent.verticalCenter
                }
                AvSelect {
                    id: deviceSelector
                    x: micAxis.after(micLabel) + micAxis.stretch
                    width: micAxis.selectorWidth
                    popupMaxWidth: micAxis.width
                    // The selector shares its row with actions on the right. Keep a
                    // wide popup inside the whole microphone area, not its own edge.
                    popup.x: Math.max(-deviceSelector.x,
                        Math.min(deviceSelector.width - deviceSelector.popup.width,
                            micAxis.width - deviceSelector.x - deviceSelector.popup.width))
                    anchors.verticalCenter: parent.verticalCenter
                    enabled: !deviceRow.locked
                    model: root.deviceNames()
                    // Apply the saved index after ComboBox's model reset; this is
                    // programmatic reconciliation, never a setting write.
                    onModelChanged: Qt.callLater(function() {
                        deviceSelector.currentIndex = Math.max(0,
                            root.deviceIndex(root.settings ? root.settings.device : ""))
                    })
                    Binding {
                        target: deviceSelector
                        property: "currentIndex"
                        value: Math.max(0, root.deviceIndex(root.settings ? root.settings.device : ""))
                    }

                    onActivated: {
                        if (root.settings && !root.isLocked("device")
                                && currentIndex >= 0 && currentIndex < root.devices.length
                                && root.settings.device !== root.deviceIdAt(currentIndex))
                            root.settings.device = root.deviceIdAt(currentIndex)
                    }
                }
                AvSlider {
                    id: micSlider
                    x: micAxis.after(deviceSelector)
                    width: micAxis.sliderWidth
                    property real draggedValue: value
                    property bool userMoved: false
                    visible: root.canRaiseMic && root.micVolume >= 0 && !root.micMuted
                    anchors.verticalCenter: parent.verticalCenter
                    Accessible.name: qsTr("Громкость микрофона")
                    Binding {
                        target: micSlider
                        property: "value"
                        value: root.micVolume
                    }

                    onValueChanged: {
                        if (pressed)
                            draggedValue = value
                    }
                    onPressedChanged: {
                        if (pressed) {
                            userMoved = false
                            draggedValue = value
                        } else {
                            var previousVolume = root.micVolume
                            if (userMoved && root.settings && Math.round(draggedValue) !== previousVolume)
                                root.settings.setMicrophoneVolume(Math.round(draggedValue))
                            if (root.micVolume === previousVolume)
                                micSlider.value = root.micVolume
                            userMoved = false
                        }
                    }
                    onMoved: {
                        userMoved = true
                        if (!pressed) {
                            var previousVolume = root.micVolume
                            if (root.settings && Math.round(value) !== previousVolume)
                                root.settings.setMicrophoneVolume(Math.round(value))
                            if (root.micVolume === previousVolume)
                                micSlider.value = root.micVolume
                            userMoved = false
                        }
                    }
                }
                Text {
                    id: volumeText
                    x: micAxis.after(micSlider)
                    width: Math.ceil(percentMeasure.implicitWidth)
                    text: qsTr("%1 %").arg(micSlider.pressed
                        ? Math.round(micSlider.value) : root.micVolume)
                    visible: micSlider.visible
                    textFormat: Text.PlainText
                    color: Theme.fgMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSettingSubSize
                    renderType: Text.NativeRendering
                    anchors.verticalCenter: parent.verticalCenter
                }
                Text {
                    id: percentMeasure
                    visible: false
                    text: qsTr("100 %")
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSettingSubSize
                    renderType: Text.NativeRendering
                }
                AvButton {
                    id: raiseMic
                    x: micAxis.volumeEnd
                    width: implicitWidth
                    text: qsTr("Поднять")
                    small: true
                    visible: root.canRaiseMic && (root.micMuted
                        || (root.micVolume >= 0 && root.micVolume < 30))
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: if (root.settings) root.settings.raiseMicrophoneVolume()
                }
                AvButton {
                    id: restoreMic
                    x: raiseMic.visible ? micAxis.after(raiseMic) : micAxis.volumeEnd
                    width: implicitWidth
                    text: qsTr("Вернуть")
                    small: true
                    visible: root.canRaiseMic && root.canRestoreMic
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: if (root.settings) root.settings.restoreMicrophoneVolume()
                }
                AvButton {
                    id: soundSettings
                    x: restoreMic.visible ? micAxis.after(restoreMic)
                        : raiseMic.visible ? micAxis.after(raiseMic) : micAxis.volumeEnd
                    width: implicitWidth
                    text: qsTr("Настройки звука…")
                    small: true
                    visible: root.canOpenSoundSettings
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: if (root.settings) root.settings.openSoundSettings()
                }
            }
            Rectangle {
                id: policyBadge
                x: Theme.cardRowPaddingX
                y: micAxis.y + micAxis.height + 4
                width: policyContent.implicitWidth + Theme.lockBadgePaddingX * 2
                height: Theme.badgeHeight
                radius: Theme.lockBadgeRadius
                antialiasing: true
                color: Theme.lockBadgeBg
                visible: deviceRow.locked
                RowLayout {
                    id: policyContent
                    anchors.centerIn: parent
                    spacing: Theme.badgeGap
                    Icon {
                        name: "lock"
                        size: Theme.lockBadgeIcon
                        color: Theme.lockBadgeFg
                        Layout.alignment: Qt.AlignVCenter
                    }
                    Text {
                        text: qsTr("Задано администратором")
                        textFormat: Text.PlainText
                        color: Theme.lockBadgeFg
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.badgeSize
                        renderType: Text.NativeRendering
                        Layout.alignment: Qt.AlignVCenter
                    }
                }
            }
            Text {
                id: micStatus
                x: Theme.cardRowPaddingX
                y: (policyBadge.visible ? policyBadge.y + policyBadge.height
                    : micAxis.y + micAxis.height) + 4
                width: micAxis.width
                visible: root.canRaiseMic && !micSlider.visible
                text: root.micMuted
                    ? qsTr("Звук микрофона выключен в системе — вас не слышно")
                    : qsTr("Не удалось узнать громкость микрофона")
                textFormat: Text.PlainText
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Индикация")

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Индикатор записи")
            sub: qsTr("Показывает, что идёт запись")
            locked: root.isLocked("pill_enabled")

            AvSelect {
                id: pillSelector
                Layout.preferredWidth: 236
                Layout.alignment: Qt.AlignVCenter
                model: [qsTr("Пилюля снизу экрана"), qsTr("Только значок в трее")]
                enabled: !root.isLocked("pill_enabled")

                Binding {
                    target: pillSelector
                    property: "currentIndex"
                    value: !root.settings || root.settings.pillEnabled === true ? 0 : 1
                }

                onCurrentIndexChanged: {
                    if (currentIndex < 0)
                        return
                    var pillEnabled = currentIndex === 0
                    if (root.settings && root.settings.pillEnabled !== pillEnabled)
                        root.settings.pillEnabled = pillEnabled
                }
            }
        }

        SettingRow {
            objectName: "soundCuesRow"
            width: parent.width
            label: qsTr("Звук начала и конца записи")
            sub: root.soundCuesStatus
            toggle: soundToggle
            rowEnabled: soundToggle.enabled

            AvToggle {
                id: soundToggle
                objectName: "soundCuesToggle"
                enabled: root.settings !== null
                Accessible.name: qsTr("Звук начала и конца записи")
                Accessible.description: root.soundCuesStatus
                Layout.alignment: Qt.AlignVCenter

                Binding {
                    target: soundToggle
                    property: "checked"
                    value: root.settings ? root.settings.soundCuesEnabled : false
                }

                onCheckedChanged: {
                    if (root.settings && root.settings.soundCuesEnabled !== checked)
                        root.settings.soundCuesEnabled = checked
                    // Сохранение может откатиться; показываем подтверждённое значение.
                    checked = root.settings ? root.settings.soundCuesEnabled : false
                }
            }
        }

        // Выключить нельзя — иначе запись стала бы скрытой (PRD §9.5).
        // Запрет показан ПОДПИСЬЮ, а не только серым цветом (сквозное правило 3).
        SettingRow {
            width: parent.width
            label: qsTr("Значок в системном трее")
            sub: qsTr("Запись всегда видна — это требование приватности")
            rowEnabled: false

            Text {
                text: qsTr("Выключить нельзя")
                // Подпись объясняет запрет и должна читаться: fg-muted, а не fg-disabled.
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }

            // Включён и заблокирован: положение читается цветом primary, запрет — подписью.
            AvToggle {
                checked: true
                locked: true
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Запуск")

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Автозапуск при входе в систему")
            toggle: autostartToggle
            locked: root.isLocked("autostart")
            rowEnabled: !root.isLocked("autostart")

            AvToggle {
                id: autostartToggle
                locked: root.isLocked("autostart")
                Layout.alignment: Qt.AlignVCenter

                Binding {
                    target: autostartToggle
                    property: "checked"
                    value: root.settings ? root.settings.autostart : true
                }

                onCheckedChanged: {
                    if (root.settings && root.settings.autostart !== checked)
                        root.settings.autostart = checked
                }
            }
        }
    }
}
