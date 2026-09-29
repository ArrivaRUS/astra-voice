// Раздел «О программе» — референс design/refs/06-about.png (+ -dark), спека §3.1–3.2,
// PRD F13 (M9-а v0.2). Экран для ИБ-службы и поддержки: версии, лицензии, приватность,
// данные на диске, статистика, правила администратора, даты проверок обновлений.
// Не показываем до своих вех: «Обновить из файла…» (M8), «Пройти настройку заново» (US-1.8).
// Без мостов раздел открывается с версией из appInfo и честными «нет данных».
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import ".."
import "../components"

Column {
    id: root

    readonly property var info: (typeof appInfo !== "undefined" && appInfo !== null) ? appInfo : null
    readonly property var about: (typeof aboutBridge !== "undefined" && aboutBridge !== null) ? aboutBridge : null
    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    readonly property var updates: (typeof updatesBridge !== "undefined" && updatesBridge !== null) ? updatesBridge : null

    readonly property string appVersion: (root.about && root.about.version) ? root.about.version
        : (root.info && root.info.version) ? root.info.version : "0.1.0"
    readonly property string sessionKind: (root.info && root.info.sessionKind) ? root.info.sessionKind : ""
    readonly property string policyStatus: (root.info && root.info.policyStatus) ? root.info.policyStatus : ""

    function buildLine() {
        if (!root.about)
            return ""
        var parts = []
        if (root.about.buildDate !== "")
            parts.push(qsTr("Сборка от %1").arg(root.about.buildDate))
        parts.push(root.about.installKind === "deb" ? qsTr("пакет deb")
            : qsTr("запуск из исходного кода"))
        var line = parts.join(" · ")
        return line.charAt(0).toUpperCase() + line.slice(1)
    }

    function componentsLine() {
        if (!root.about)
            return ""
        function item(name, value) {
            return name + " " + (value !== "" ? value : qsTr("не найден"))
        }
        return [item("Python", root.about.pythonVersion),
                item("Qt", root.about.qtVersion),
                item("PyQt5", root.about.pyqtVersion),
                item("onnxruntime", root.about.onnxruntimeVersion),
                item("onnx-asr", root.about.onnxAsrVersion)].join(" · ")
    }

    // Без ревизии: технические идентификаторы — только в «Отладке» (сквозное правило 7).
    function modelLine() {
        var name = root.settings ? root.settings.activeModelName : ""
        return name !== "" ? name : qsTr("Не выбрана")
    }

    function sessionLine() {
        switch (root.sessionKind) {
        case "KDE":
            return qsTr("KDE Plasma")
        case "FLY":
            return qsTr("Fly")
        case "":
            return ""
        default:
            return qsTr("Другое окружение")
        }
    }

    function policyLine() {
        switch (root.policyStatus) {
        case "absent":
            return qsTr("Не заданы — все настройки в ваших руках")
        case "ok":
            return qsTr("Применены: строки с замком задал администратор")
        case "ignored":
            return qsTr("Не применены: файл правил небезопасен, программа работает без него")
        case "invalid":
            return qsTr("Не применены: файл правил не удалось прочитать, программа работает без него")
        default:
            return qsTr("Нет сведений")
        }
    }

    function updatesLine() {
        if (!root.about)
            return qsTr("Нет сведений")
        var attempt = root.about.lastAttemptText
        var success = root.about.lastSuccessText
        if (attempt === "" && success === "")
            return qsTr("Ещё не проверялись")
        return qsTr("Последняя попытка: %1 · последний успех: %2")
            .arg(attempt !== "" ? attempt : qsTr("не было"))
            .arg(success !== "" ? success : qsTr("не было"))
    }

    // Перечитываем при открытии раздела и при каждой активации окна: статистика,
    // размеры и даты проверок могли измениться, пока окно было скрыто.
    Component.onCompleted: {
        if (root.about)
            root.about.refresh()
    }

    Connections {
        target: root.Window.window
        function onActiveChanged() {
            if (root.Window.window && root.Window.window.active && root.about)
                root.about.refresh()
        }
    }

    spacing: Theme.spaceGroupGap

    // Значение справа в строке: моноширинное (сквозное правило 5), приглушённое.
    component ValueText: Text {
        textFormat: Text.PlainText
        color: Theme.fgMuted
        font.family: Theme.fontMono
        font.pixelSize: Theme.fontSettingSubSize
        // Трекинг `.mono` макета 06-about: +0.02em.
        font.letterSpacing: Theme.fontMonoInlineTracking * Theme.fontSettingSubSize
        renderType: Text.NativeRendering
        elide: Text.ElideRight
        Layout.alignment: Qt.AlignVCenter
    }

    SettingGroup {
        width: root.width
        title: qsTr("Версии")

        SettingRow {
            width: parent.width
            divider: false
            showHint: false
            label: qsTr("Astra Voice")
            sub: root.buildLine()

            Text {
                objectName: "aboutVersion"
                textFormat: Text.PlainText
                text: root.appVersion
                color: Theme.fg
                font.family: Theme.fontMono
                font.pixelSize: Theme.fontSettingLabelSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                objectName: "aboutCheckUpdates"
                small: true
                iconName: "refresh"
                text: qsTr("Проверить обновления")
                visible: root.updates !== null
                enabled: root.updates !== null && root.updates.canCheckNow
                    && root.updates.state !== "checking"
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.updates) root.updates.checkNow(); }
            }
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Активная модель")

            ValueText {
                objectName: "aboutModel"
                text: root.modelLine()
                Layout.maximumWidth: root.width / 2
            }
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Компоненты")
            sub: root.componentsLine()
            visible: root.about !== null
            height: visible ? implicitHeight : 0
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Лицензии и приватность")

        SettingRow {
            width: parent.width
            divider: false
            showHint: false
            label: qsTr("Лицензия программы")
            sub: qsTr("Исходный код открыт: github.com/ArrivaRUS/astra-voice")

            Text {
                textFormat: Text.PlainText
                text: "GPL-3.0-or-later"
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                objectName: "aboutOpenLicense"
                small: true
                iconName: "file"
                text: qsTr("Лицензия программы")
                enabled: root.about !== null && root.about.licenseAvailable
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.about) root.about.openLicense(); }
            }
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Библиотеки")
            sub: qsTr("Qt 5 — LGPL-3.0 · The Qt Company; PyQt5 — GPL-3.0 · Riverbank Computing; onnxruntime — MIT · Microsoft")
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Лицензии компонентов и моделей")
            sub: qsTr("GigaAM — MIT · Сбер; Whisper large-v3-turbo — MIT, Whisper small и base — Apache-2.0 · OpenAI; Vosk — Apache-2.0 · Alpha Cephei; NeMo FastConformer — CC-BY-4.0 · NVIDIA")

            AvButton {
                objectName: "aboutOpenNotice"
                small: true
                iconName: "file"
                text: qsTr("Лицензии компонентов")
                enabled: root.about !== null && root.about.noticeAvailable
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.about) root.about.openNotice(); }
            }
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Приватность и сетевые хосты")
            sub: qsTr("Звук и текст не покидают компьютер и не сохраняются на диск. Сеть нужна только для скачивания моделей с huggingface.co (или с зеркала администратора) и проверки новой версии на github.com — по кнопке или раз в сутки, если вы это включили.")

            AvButton {
                objectName: "aboutOpenPrivacy"
                small: true
                iconName: "shield"
                text: qsTr("Приватность")
                enabled: root.about !== null && root.about.privacyAvailable
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.about) root.about.openPrivacy(); }
            }
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Товарные знаки")
            sub: qsTr("Astra Linux — товарный знак ПАО «Группа Астра»; GigaAM — обозначение Сбера (СберДевайсы); Whisper — OpenAI; Vosk — Alpha Cephei; T-one — АО «ТБанк»; NeMo — NVIDIA. Программа разработана независимо и не аффилирована с правообладателями, не одобрена ими; названия указывают только на совместимость и происхождение компонентов.")
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Данные на диске")

        SettingRow {
            width: parent.width
            divider: false
            showHint: false
            label: qsTr("Настройки")
            sub: root.about ? root.about.settingsPath : ""
            subMono: true

            AvButton {
                objectName: "aboutOpenSettings"
                small: true
                iconName: "folder"
                text: qsTr("Открыть")
                enabled: root.about !== null && root.about.settingsFolderAvailable
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.about) root.about.openSettingsFolder(); }
            }
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Модели")
            sub: root.about ? root.about.modelsPath : ""
            subMono: true

            ValueText {
                objectName: "aboutModelsSize"
                text: root.about ? root.about.modelsSize : ""
                visible: text !== ""
            }

            AvButton {
                objectName: "aboutOpenModels"
                small: true
                iconName: "folder"
                text: qsTr("Открыть")
                enabled: root.settings !== null
                    && (root.about === null || root.about.modelsFolderAvailable)
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.settings) root.settings.openModelsFolder(); }
            }
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Журналы")
            sub: root.about ? root.about.logsPath : ""
            subMono: true

            ValueText {
                objectName: "aboutLogsSize"
                text: root.about ? root.about.logsSize : ""
                visible: text !== ""
            }

            AvButton {
                objectName: "aboutOpenLogs"
                small: true
                iconName: "folder"
                text: qsTr("Открыть")
                enabled: root.about !== null && root.about.logsFolderAvailable
                Layout.alignment: Qt.AlignVCenter
                onClicked: { if (root.about) root.about.openLogsFolder(); }
            }
        }

        SettingRow {
            id: statsRow
            readonly property bool hasStats: root.about !== null && root.about.statsCount > 0
            width: parent.width
            showHint: false
            label: qsTr("Статистика распознавания")
            // Статистики ещё нет — строка целиком в виде «выключено» (макет 06-about, empty).
            rowEnabled: statsRow.hasStats
            sub: statsRow.hasStats ? qsTr("Только на этом компьютере, никуда не отправляется")
                : qsTr("Появится после первой диктовки")

            Text {
                objectName: "aboutStats"
                textFormat: Text.PlainText
                text: statsRow.hasStats ? root.about.statsText : qsTr("Пока нет данных")
                color: statsRow.hasStats ? Theme.fgMuted : Theme.fgDisabled
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                elide: Text.ElideRight
                Layout.maximumWidth: root.width / 2
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                objectName: "aboutClearStats"
                small: true
                text: qsTr("Очистить")
                enabled: statsRow.hasStats && root.about.statsAvailable
                Layout.alignment: Qt.AlignVCenter
                onClicked: clearDialog.open()
            }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Окружение")

        SettingRow {
            width: parent.width
            divider: false
            showHint: false
            label: qsTr("Рабочий стол")
            visible: root.sessionLine() !== ""
            height: visible ? implicitHeight : 0

            ValueText {
                objectName: "aboutSession"
                text: root.sessionLine()
            }
        }

        SettingRow {
            width: parent.width
            divider: root.sessionLine() !== ""
            showHint: false
            label: qsTr("Правила администратора")
            sub: root.policyLine()
        }

        SettingRow {
            width: parent.width
            showHint: false
            label: qsTr("Проверка обновлений")
            sub: root.updatesLine()
        }
    }

    AvDialog {
        id: clearDialog
        objectName: "aboutClearDialog"
        parent: root.Overlay.overlay ? root.Overlay.overlay : root
        heading: qsTr("Очистить статистику?")
        iconName: "trash"
        message: qsTr("Вся локальная статистика начнётся заново: диктовки, время распознавания, проверки обновлений, ошибки микрофона.")
        confirmText: qsTr("Очистить")
        cancelText: qsTr("Отмена")
        onConfirmed: { if (root.about) root.about.clearStats(); }
    }
}
