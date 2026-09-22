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
    readonly property string modelSelfcheck: root.settings ? root.settings.modelSelfcheck : ""

    // Громкость микрофона (PRD 0.9 F6.6/F6.7, ступени 1–2): читаем по открытию
    // раздела и после нажатия — сама программа системную громкость не крутит.
    readonly property bool canRaiseMic: root.settings ? root.settings.canRaiseMicrophone : false
    readonly property bool micMuted: root.settings ? root.settings.microphoneMuted : false
    readonly property int micVolume: root.settings ? root.settings.microphoneVolume : -1

    Component.onCompleted: {
        if (root.settings)
            root.settings.refreshMicrophone()
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
            }
        }

        SettingRow {
            width: parent.width
            label: qsTr("Режим")
            // Подпись вернулась вместе с переездом модели в раздел «Модели»:
            // без строки модели раздел снова помещается без прокрутки.
            sub: qsTr("Удерживать — самый предсказуемый вариант")
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

        SettingRow {
            width: parent.width
            label: qsTr("Микрофон")
            locked: root.isLocked("device")

            AvSelect {
                id: deviceSelector
                Layout.preferredWidth: 236  // §4.4: типовая ширина списка в строке настройки
                Layout.alignment: Qt.AlignVCenter
                enabled: !root.isLocked("device")
                model: (root.settings && root.settings.device
                    && root.settings.device !== qsTr("Системный по умолчанию"))
                    ? [root.settings.device, qsTr("Системный по умолчанию")]
                    : [qsTr("Системный по умолчанию")]

                Binding {
                    target: deviceSelector
                    property: "currentIndex"
                    value: Math.max(0, deviceSelector.model.indexOf(
                        root.settings ? root.settings.device : ""))
                }

                onCurrentIndexChanged: {
                    if (root.settings && !root.isLocked("device")
                            && currentIndex >= 0 && currentIndex < model.length
                            && root.settings.device !== model[currentIndex])
                        root.settings.device = model[currentIndex]
                }
            }
        }

        // Строка только при проблеме (выключен или тише 30 %): в норме «Общие»
        // помещаются без прокрутки (решение заказчика 22.09, как «Переустановить»).
        SettingRow {
            width: parent.width
            label: qsTr("Громкость микрофона")
            sub: root.micMuted
                ? qsTr("Звук микрофона выключен в системе — вас не слышно")
                : qsTr("Громкость слишком низкая — вас плохо слышно")
            visible: root.canRaiseMic && (root.micMuted || (root.micVolume >= 0 && root.micVolume < 30))
            height: visible ? implicitHeight : 0

            Text {
                textFormat: Text.PlainText
                text: root.micMuted ? qsTr("Выключен")
                    : root.micVolume >= 0 ? qsTr("Сейчас %1 %").arg(root.micVolume)
                    : qsTr("Не удалось узнать")
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                text: qsTr("Поднять")
                small: true
                Layout.alignment: Qt.AlignVCenter
                onClicked: {
                    if (root.settings)
                        root.settings.raiseMicrophoneVolume()
                }
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
            width: parent.width
            label: qsTr("Звук начала и конца записи")
            toggle: soundToggle

            AvToggle {
                id: soundToggle
                checked: false
                Layout.alignment: Qt.AlignVCenter
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
