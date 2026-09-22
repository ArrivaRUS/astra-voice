// Раздел «Общие» — референс design/refs/01-general-base.png (+ -dark), спека §3.
// Восемь настроек в трёх группах. Без моста настроек используются дефолты M1.
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
    readonly property string activeModelState: root.settings ? root.settings.activeModelState : "none"
    // Пока модель качается или проверяется, действия над ней недоступны.
    readonly property bool busyWithModel: root.activeModelState === "downloading"
        || root.activeModelState === "verifying"
    // Модели нет вовсе — предлагаем поставить рекомендованную, а не «переустановить».
    readonly property bool canInstall: root.activeModelState === "none"

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

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Модель распознавания")
            // Подписи нет намеренно: имя модели стоит справа, у самих кнопок.
            // Со второй строкой раздел перестаёт помещаться без прокрутки.
            showHint: false

            Text {
                textFormat: Text.PlainText
                text: !root.settings || root.activeModelState === "none"
                        || root.settings.activeModelName === ""
                    ? qsTr("Модель не установлена")
                    : root.settings.activeModelSize !== ""
                        ? qsTr("%1 · %2").arg(root.settings.activeModelName)
                            .arg(root.settings.activeModelSize)
                        : root.settings.activeModelName
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                elide: Text.ElideRight
                Layout.maximumWidth: root.width / 3
                Layout.alignment: Qt.AlignVCenter
            }

            Text {
                textFormat: Text.PlainText
                text: {
                    if (!root.settings)
                        return ""
                    if (root.busyWithModel) {
                        var parts = []
                        if (root.settings.downloadTitle !== "")
                            parts.push(root.settings.downloadTitle)
                        if (root.settings.eta !== "")
                            parts.push(root.settings.eta)
                        return parts.join(" · ")
                    }
                    // Текст отказа приходит из моста готовой фразой (§4.4).
                    return root.settings.activeModelMessage
                }
                visible: text !== ""
                color: root.busyWithModel ? Theme.fgMuted : Theme.dangerInk
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                // Только одна строка с обрезкой: с переносом высота зависит от
                // ширины, ширина — от раскладки, и SettingRow зацикливает
                // расчёт высоты (Binding loop, тот же класс, что и 16.09).
                elide: Text.ElideRight
                Layout.maximumWidth: root.width / 4
                Layout.alignment: Qt.AlignVCenter
            }

            // Во время загрузки её можно прервать — иначе фоновая сеть
            // остаётся без управления (ИБ, У66).
            AvButton {
                text: qsTr("Отмена")
                small: true
                visible: root.busyWithModel
                Layout.alignment: Qt.AlignVCenter
                onClicked: if (root.settings) root.settings.cancelDownloads()
            }

            // В норме кнопки нет: рабочую модель трогать незачем, а соблазн
            // перекачать 226 МБ без причины — вред. Показываем только когда
            // с моделью что-то не так или её нет вовсе (решение 22.09).
            AvButton {
                text: root.canInstall ? qsTr("Установить") : qsTr("Переустановить")
                small: true
                iconName: root.canInstall ? "download" : "refresh"
                visible: !root.busyWithModel && root.activeModelState !== "ok"
                enabled: root.settings !== null
                    && (root.canInstall ? root.settings.canInstall : root.settings.canReinstall)
                Layout.alignment: Qt.AlignVCenter
                onClicked: {
                    if (!root.settings)
                        return
                    if (root.canInstall)
                        root.settings.installRecommendedModel()
                    else
                        root.settings.reinstallActiveModel()
                }
            }
        }

        // Кнопки «Повторить» нет по решению заказчика от 15.09.2026 (PRD 0.8 F2.11):
        // бэкенд выполняет автоповтор раз в 30 секунд.
        SettingRow {
            width: parent.width
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
            // Подписи нет: смысл виден по самим кнопкам, а раздел должен
            // помещаться без прокрутки (решение заказчика 22.09).
            showHint: false
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
