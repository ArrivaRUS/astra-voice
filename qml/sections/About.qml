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
    readonly property var management: (typeof appImageManagement !== "undefined" && appImageManagement !== null) ? appImageManagement : null
    readonly property bool isAppImage: root.about !== null
        && (root.about.installKind === "appimage-installed" || root.about.installKind === "appimage-portable")
    readonly property string managementState: root.management ? root.management.state : "unavailable"
    property Item managementOpener: null
    property bool managementViewReady: false

    function refreshManagement() {
        if (!root.managementViewReady || !root.visible || !root.isAppImage || !root.management)
            return
        // Consent and operations own their snapshot. Keep failures visible until Retry.
        if (root.management.confirmationId !== "") return
        if (root.managementState === "ready" || root.managementState === "absent"
                || root.managementState === "busy")
            root.management.refresh()
    }

    function managementStatus() {
        if (!root.management)
            return qsTr("Не удалось проверить установку. Управление программой недоступно.")
        if (root.management.errorText !== "") return root.management.errorText
        switch (root.managementState) {
        case "checking": return qsTr("Проверяем установку…")
        case "unregistering": return qsTr("Убираем регистрацию…")
        case "removing": return qsTr("Готовим удаление…")
        case "busy": return root.management.busyReason
        }
        return root.management.resultText
    }

    function requestManagement(action, opener) {
        if (!root.management || !root.isAppImage) return
        root.managementOpener = opener
        root.management.requestAction(action)
    }

    function syncConfirmation() {
        if (!managementDialog) return
        if (root.isAppImage && root.management && root.managementState === "confirming"
                && root.management.confirmationId !== "") {
            if (managementDialog.token === root.management.confirmationId && managementDialog.visible) return
            if (managementDialog.visible) managementDialog.close()
            managementDialog.token = root.management.confirmationId
            managementDialog.owner = root.management
            managementDialog.heading = root.management.confirmationAction === "remove"
                ? qsTr("Удалить программу из домашней папки?")
                : qsTr("Убрать AppImage из меню и автозапуска?")
            managementDialog.confirmText = root.management.confirmationAction === "remove"
                ? qsTr("Удалить программу и выйти") : qsTr("Убрать регистрацию")
            managementDialog.message = root.management.confirmationMessage
            managementDialog.open()
        } else if (managementDialog.visible) {
            managementDialog.close()
        }
    }

    onManagementChanged: {
        // Context replacement invalidates several bindings together; wait for their values.
        Qt.callLater(root.syncConfirmation)
        Qt.callLater(root.refreshManagement)
    }
    onVisibleChanged: if (visible) {
        Qt.callLater(root.syncConfirmation)
        Qt.callLater(root.refreshManagement)
    }
    Connections {
        target: root.management
        function onChanged() { root.syncConfirmation() }
    }

    readonly property string appVersion: (root.about && root.about.version) ? root.about.version
        : (root.info && root.info.version) ? root.info.version : "0.1.0"
    readonly property string sessionKind: (root.info && root.info.sessionKind) ? root.info.sessionKind : ""
    readonly property string policyStatus: (root.info && root.info.policyStatus) ? root.info.policyStatus : ""
    readonly property string policyWarning: (root.info && root.info.policyWarning) ? root.info.policyWarning : ""

    function buildLine() {
        if (!root.about)
            return ""
        var parts = []
        if (root.about.buildDate !== "")
            parts.push(qsTr("Сборка от %1").arg(root.about.buildDate))
        switch (root.about.installKind) {
        case "appimage-installed": parts.push(qsTr("AppImage · установлена в домашнюю папку")); break
        case "appimage-portable": parts.push(qsTr("AppImage · переносной запуск")); break
        case "deb": parts.push(qsTr("пакет deb")); break
        case "source": parts.push(qsTr("запуск из исходного кода")); break
        default: parts.push(qsTr("Тип установки не определён"))
        }
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
        var line
        switch (root.policyStatus) {
        case "absent":
            line = qsTr("Не заданы — все настройки в ваших руках")
            break
        case "ok":
            line = qsTr("Применены: строки с замком задал администратор")
            break
        case "ignored":
            line = qsTr("Не применены: файл правил небезопасен, программа работает без него")
            break
        case "invalid":
            line = qsTr("Не применены: файл правил не удалось прочитать, программа работает без него")
            break
        default:
            line = qsTr("Нет сведений")
        }
        return root.policyWarning !== "" ? line + ". " + root.policyWarning : line
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
        root.managementViewReady = true
        if (root.about)
            root.about.refresh()
        root.syncConfirmation()
        root.refreshManagement()
    }

    Connections {
        target: root.Window.window
        function onActiveChanged() {
            if (root.Window.window && root.Window.window.active) {
                if (root.about) root.about.refresh()
                root.refreshManagement()
            }
        }
    }

    spacing: Theme.spaceGroupGap

    component ManagementButton: AvButton {
        id: control
        small: true
        implicitWidth: buttonLabel.implicitWidth + leftPadding + rightPadding
        implicitHeight: Math.max(Theme.buttonHeightSm,
            buttonLabel.implicitHeight + topPadding + bottomPadding)
        contentItem: Text {
            id: buttonLabel
            text: control.text
            textFormat: Text.PlainText
            font: control.font
            color: control.fgColor
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            wrapMode: Text.Wrap
            renderType: Text.NativeRendering
        }
    }

    component ManagementRow: Item {
        id: row
        property string label: ""
        property string description: ""
        property string actionText: ""
        property string actionName: ""
        property bool actionEnabled: false
        property bool divider: true
        signal triggered(var button)
        readonly property real available: Math.max(0, width - 28)
        readonly property bool stacked: copyLabel.implicitWidth + action.implicitWidth + 10 > available
            || copySub.implicitWidth + action.implicitWidth + 10 > available
        implicitHeight: Math.max(55, 14 + (stacked
            ? copyBlock.height + 10 + action.height : Math.max(copyBlock.height, action.height)))
        height: implicitHeight
        Rectangle {
            visible: row.divider
            width: parent.width
            height: Theme.borderHairline
            color: Theme.borderSoft
        }
        Column {
            id: copyBlock
            x: 14
            y: row.stacked ? 7 : (row.height - height) / 2
            width: row.stacked ? row.available : Math.max(0, row.available - action.width - 10)
            spacing: 2
            Text {
                id: copyLabel
                width: parent.width
                text: row.label
                textFormat: Text.PlainText
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingLabelSize
                color: Theme.fg
                wrapMode: Text.Wrap
                renderType: Text.NativeRendering
            }
            Text {
                id: copySub
                width: parent.width
                text: row.description
                textFormat: Text.PlainText
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                color: Theme.fgMuted
                wrapMode: Text.Wrap
                renderType: Text.NativeRendering
            }
        }
        ManagementButton {
            id: action
            objectName: row.actionName
            text: row.actionText
            Accessible.name: row.actionText
            Accessible.description: row.description + (row.actionName === "aboutRemoveAppImage"
                ? qsTr(" После подтверждения программа завершит работу.") : "")
            enabled: row.actionEnabled
            width: Math.min(implicitWidth, row.available)
            x: row.stacked ? 14 : row.width - 14 - width
            y: row.stacked ? copyBlock.y + copyBlock.height + 10 : (row.height - height) / 2
            onClicked: row.triggered(action)
        }
    }

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
            sub: qsTr("Qt 5 — LGPL-3.0 · The Qt Company; PyQt5 — GPL-3.0 · Riverbank Computing; onnxruntime — MIT · Microsoft; onnx-asr — MIT")
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

    Column {
        id: managementGroup
        objectName: "aboutAppImageManagement"
        width: root.width
        visible: root.isAppImage
        height: visible ? implicitHeight : 0
        spacing: Theme.spaceGroupCaptionGap
        Text {
            width: parent.width
            text: qsTr("Программа в домашней папке")
            textFormat: Text.PlainText
            color: Theme.fgMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontGroupCapsSize
            font.weight: Font.Medium
            font.capitalization: Font.AllUppercase
            font.letterSpacing: Theme.fontGroupCapsTracking * Theme.fontGroupCapsSize
            wrapMode: Text.Wrap
            leftPadding: 2
            renderType: Text.NativeRendering
        }
        Rectangle {
            width: parent.width
            height: managementRows.height + Theme.cardBorder * 2
            radius: Theme.cardRadius
            color: Theme.bgSurface
            border.width: Theme.cardBorder
            border.color: Theme.border
            Column {
                id: managementRows
                x: Theme.cardBorder
                y: Theme.cardBorder
                width: parent.width - 2 * Theme.cardBorder
                ManagementRow {
                    width: parent.width
                    divider: false
                    label: qsTr("Меню и автозапуск")
                    description: root.management && root.managementState === "ready" && !root.management.canUnregister
                        ? qsTr("Регистрация AppImage уже убрана") : qsTr("Копии программы и ваши данные останутся.")
                    actionName: "aboutUnregisterAppImage"
                    actionText: qsTr("Убрать из меню и автозапуска")
                    actionEnabled: root.management !== null && root.management.canUnregister
                    onTriggered: root.requestManagement("unregister", button)
                }
                ManagementRow {
                    width: parent.width
                    label: qsTr("Копии программы")
                    description: qsTr("Модели, настройки и журналы останутся.")
                    actionName: "aboutRemoveAppImage"
                    actionText: qsTr("Удалить программу из домашней папки")
                    actionEnabled: root.management !== null && root.management.canRemove
                    onTriggered: root.requestManagement("remove", button)
                }
                Item {
                    width: parent.width
                    height: pathColumn.height + 14
                    Rectangle {
                        width: parent.width
                        height: Theme.borderHairline
                        color: Theme.borderSoft
                    }
                    Column {
                        id: pathColumn
                        x: 14
                        y: 7
                        width: parent.width - 28
                        spacing: 2
                        Text {
                            width: parent.width
                            text: qsTr("Папка программы")
                            textFormat: Text.PlainText
                            color: Theme.fg
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontSettingLabelSize
                            wrapMode: Text.Wrap
                            renderType: Text.NativeRendering
                        }
                        TextEdit {
                            id: managementPath
                            objectName: "aboutAppImagePath"
                            width: parent.width
                            height: implicitHeight
                            text: root.managementState === "absent"
                                ? qsTr("В домашней папке нет установленных копий программы")
                                : root.management && root.management.appPath !== "" ? root.management.appPath : qsTr("Нет сведений")
                            textFormat: TextEdit.PlainText
                            readOnly: true
                            selectByMouse: true
                            activeFocusOnTab: true
                            wrapMode: TextEdit.Wrap
                            font.family: Theme.fontMono
                            font.pixelSize: Theme.fontSettingSubSize
                            color: Theme.fgMuted
                            selectedTextColor: Theme.selectionFg
                            selectionColor: Theme.selectionBg
                            renderType: Text.NativeRendering
                            Accessible.name: qsTr("Папка программы")
                            Rectangle {
                                anchors.fill: parent
                                anchors.margins: -2
                                color: "transparent"
                                border.width: 2
                                border.color: Theme.stateFocusRing
                                visible: managementPath.activeFocus
                            }
                        }
                    }
                }
            }
        }
        Rectangle {
            width: parent.width
            visible: root.managementStatus() !== ""
            height: visible ? statusColumn.height + 24 : 0
            radius: Theme.cardRadius
            color: root.managementState === "error" ? Theme.dangerBg : Theme.bgSurface2
            Column {
                id: statusColumn
                x: 14
                y: 12
                width: parent.width - 28
                spacing: 8
                RowLayout {
                    width: parent.width
                    spacing: 8
                    BusyIndicator {
                        visible: root.managementState === "checking" || root.managementState === "unregistering"
                            || root.managementState === "removing"
                        running: visible
                        implicitWidth: 18
                        implicitHeight: 18
                        Layout.alignment: Qt.AlignTop
                    }
                    Text {
                        objectName: "aboutAppImageStatus"
                        Accessible.role: Accessible.StaticText
                        Accessible.name: text
                        text: root.managementState === "error" ? qsTr("Ошибка: %1").arg(root.managementStatus()) : root.managementStatus()
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        color: root.managementState === "error" ? Theme.dangerInk : Theme.fgSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSmallSize
                        wrapMode: Text.Wrap
                        renderType: Text.NativeRendering
                    }
                }
                ManagementButton {
                    objectName: "aboutRetryAppImage"
                    visible: root.managementState === "error"
                    text: qsTr("Повторить")
                    width: Math.min(implicitWidth, parent.width)
                    onClicked: {
                        if (root.management) {
                            root.managementOpener = this
                            root.management.retry()
                        }
                    }
                }
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
        id: managementDialog
        objectName: "aboutAppImageDialog"
        property string token: ""
        property var owner: null
        parent: root.Overlay.overlay ? root.Overlay.overlay : root
        safeConfirmation: true
        returnFocusItem: root.managementOpener
        fallbackFocusItem: managementPath
        iconName: "trash"
        cancelText: qsTr("Отмена")
        confirmEnabled: root.management !== null && owner === root.management
            && token !== "" && token === root.management.confirmationId
        onConfirmed: {
            if (owner && owner === root.management) owner.confirmAction(token)
        }
        onCancelled: {
            if (owner && owner === root.management) owner.cancelConfirmation(token)
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
