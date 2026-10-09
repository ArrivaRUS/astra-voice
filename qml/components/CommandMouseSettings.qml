// Local button assignment is a setting gesture, never a recording gesture.
// Logical buttons and capture validation belong to SettingsBridge.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import ".."

Column {
    id: root
    property var bridge: null
    readonly property bool supported: bridge !== null
        && typeof bridge.commandMouseEnabled !== "undefined"
        && typeof bridge.commandMouseCanEdit !== "undefined"
        && typeof bridge.beginCommandMouseCapture === "function"
    readonly property bool installed: bridge ? bridge.commandInstalled : false
    readonly property bool canEdit: supported && installed && bridge.commandMouseCanEdit
    readonly property bool mouseEnabled: supported ? bridge.commandMouseEnabled : false
    readonly property int button: supported ? bridge.commandMouseButton : 2
    readonly property string buttonLabel: supported ? bridge.commandMouseButtonLabel : qsTr("Средняя кнопка")
    readonly property string status: supported ? bridge.commandMouseStatus : "unavailable"
    readonly property string captureState: supported ? bridge.commandMouseCaptureState : "idle"
    readonly property int pendingButton: supported ? bridge.commandMousePendingButton : 0
    readonly property string pendingLabel: supported ? bridge.commandMousePendingButtonLabel : ""
    readonly property string captureMessage: supported ? bridge.commandMouseCaptureMessage : ""
    readonly property bool captureActive: captureState !== "idle"
    readonly property bool localCapture: captureState === "capturing" || captureState === "pressed"
        || (captureState === "error" && pendingButton === 0)
    readonly property bool keyboardCapture: bridge && bridge.captureState !== "idle" && bridge.captureRole === "command"
    readonly property bool canChoose: canEdit && !captureActive && !keyboardCapture
    readonly property var choices: button === 2 || button === 8 || button === 9
        ? [2, 8, 9] : [2, 8, 9, button]
    readonly property string statusMessage: !bridge ? qsTr("Проверяю Astra Cowork…")
        : !installed ? qsTr("Голосовые команды доступны после установки Astra Cowork")
        : !supported ? qsTr("Настройка кнопки мыши пока недоступна")
        : !bridge.commandEnabled ? qsTr("Не действует: голосовые команды выключены")
        : bridge.commandMouseStatusMessage
    readonly property var shell: root.Window.window
    property bool openingCapture: false
    property bool requestedCaptureFocus: false
    property bool hadCapture: false
    property var returnFocus: null
    width: parent ? parent.width : 580

    function choiceNames() {
        var names = [qsTr("Средняя кнопка"), qsTr("Дополнительная кнопка 1"), qsTr("Дополнительная кнопка 2")]
        if (choices.length > 3)
            names.push(qsTr("Кнопка мыши %1").arg(button))
        return names
    }
    function restoreFocus() {
        if (returnFocus && root.visible && (!shell || shell.active))
            returnFocus.forceActiveFocus(Qt.TabFocusReason)
        returnFocus = null
    }
    function cancelCapture(restore) {
        openingCapture = false
        requestedCaptureFocus = false
        if (!restore)
            returnFocus = null
        if (supported && captureActive)
            bridge.cancelCommandMouseCapture()
        if (restore)
            restoreFocus()
        else
            returnFocus = null
    }
    function beginCapture() {
        if (!canChoose)
            return
        returnFocus = chooseButton
        openingCapture = true
        // clicked is emitted after the opening release; don't arm in its handler.
        Qt.callLater(function() {
            if (!openingCapture || !root.visible || !root.canChoose)
                return
            openingCapture = false
            requestedCaptureFocus = true
            if (root.bridge.beginCommandMouseCapture()) {
                if (root.localCapture)
                    captureField.forceActiveFocus(Qt.TabFocusReason)
            } else {
                requestedCaptureFocus = false
                restoreFocus()
            }
        })
    }
    function selectCandidate(logical, caller) {
        if (!canChoose)
            return
        returnFocus = caller
        // Selection only stages a candidate. The separate Apply button saves it.
        bridge.selectCommandMouseButton(logical)
    }
    onCaptureStateChanged: {
        if (captureActive)
            hadCapture = true
        if (requestedCaptureFocus && localCapture) {
            requestedCaptureFocus = false
            captureField.forceActiveFocus(Qt.TabFocusReason)
        }
        if (!captureActive && hadCapture) {
            hadCapture = false
            restoreFocus()
        }
    }
    onVisibleChanged: if (!visible) cancelCapture(false)
    onInstalledChanged: if (!installed) cancelCapture(false)
    Component.onDestruction: cancelCapture(false)
    Connections {
        target: root.shell
        function onActiveChanged() { if (!root.shell.active) root.cancelCapture(false) }
        function onVisibleChanged() { if (!root.shell.visible) root.cancelCapture(false) }
        function onVisibilityChanged() {
            if (root.shell.visibility === Window.Minimized || root.shell.visibility === Window.Hidden)
                root.cancelCapture(false)
        }
    }

    SettingRow {
        width: parent.width
        label: qsTr("Команда кнопкой мыши")
        sub: qsTr("Удерживайте кнопку и говорите; отпустите — отправить")
        showHint: false
        toggle: mouseToggle
        rowEnabled: mouseToggle.enabled
        AvToggle {
            id: mouseToggle
            objectName: "commandMouseToggle"
            // Turning off stays available while a command owns the input.
            enabled: root.supported && root.installed && (root.mouseEnabled || root.canChoose)
            Accessible.name: checked ? qsTr("Команда кнопкой мыши, включено") : qsTr("Команда кнопкой мыши, выключено")
            Binding { target: mouseToggle; property: "checked"; value: root.mouseEnabled }
            onToggled: {
                if (root.supported)
                    root.bridge.commandMouseEnabled = checked
                // A failed save/grab must not leave the switch optimistically on.
                checked = root.mouseEnabled
            }
        }
    }

    SettingRow {
        id: buttonRow
        width: parent.width
        label: qsTr("Кнопка мыши")
        sub: root.mouseEnabled && root.bridge && root.bridge.commandEnabled && root.status === "ready"
            ? qsTr("Только удержание, независимо от режима клавиатуры")
            : root.mouseEnabled ? root.statusMessage
            : qsTr("Выключено — обычные действия кнопки сохранены")
        showHint: false
        rowEnabled: root.canChoose
        AvSelect {
            id: buttonSelect
            objectName: "commandMouseSelect"
            Layout.preferredWidth: 236
            Layout.alignment: Qt.AlignVCenter
            enabled: root.canChoose
            model: root.choiceNames()
            popupMaxWidth: buttonRow.width - Theme.cardRowPaddingX * 2
            Accessible.name: qsTr("Кнопка мыши, %1").arg(root.buttonLabel)
            Accessible.description: qsTr("Дополнительные кнопки обычно находятся сбоку. При изменённой раскладке мыши используйте «Выбрать другую».")
            readonly property int savedButtonIndex: Math.max(0, root.choices.indexOf(root.button))

            function syncSavedSelection() {
                // ComboBox resets its index when its model is replaced. Apply the
                // saved index after that reset, including a newly added custom button.
                if (savedButtonIndex < count)
                    currentIndex = savedButtonIndex
            }
            onModelChanged: Qt.callLater(syncSavedSelection)
            onCountChanged: Qt.callLater(syncSavedSelection)
            onSavedButtonIndexChanged: Qt.callLater(syncSavedSelection)
            Component.onCompleted: Qt.callLater(syncSavedSelection)
            onActivated: {
                var candidate = root.choices[index]
                if (candidate !== root.button)
                    root.selectCandidate(candidate, buttonSelect)
                syncSavedSelection()
                Qt.callLater(syncSavedSelection)
            }
            ToolTip.visible: hovered && !popup.visible
            ToolTip.delay: 500
            ToolTip.text: Accessible.description
        }
        AvButton {
            id: chooseButton
            objectName: "commandMouseChoose"
            text: qsTr("Выбрать другую")
            small: true
            enabled: root.canChoose && !root.openingCapture
            Accessible.name: qsTr("Выбрать кнопку мыши для команды")
            onClicked: root.beginCapture()
        }
    }

    Item {
        width: parent.width
        implicitHeight: content.y + content.height + Theme.cardRowPaddingY
        height: implicitHeight
        Column {
            id: content
            y: root.captureActive ? Theme.fieldGap : 0
            x: Theme.cardRowPaddingX
            width: parent.width - Theme.cardRowPaddingX * 2
            spacing: Theme.fieldGap

            FocusScope {
                id: captureField
                objectName: "commandMouseCaptureField"
                width: parent.width
                implicitHeight: captureContent.height + Theme.cardRowPaddingY * 2
                height: visible ? implicitHeight : 0
                visible: root.localCapture
                activeFocusOnTab: true
                Accessible.role: Accessible.Pane
                Accessible.name: qsTr("Выбор кнопки мыши")
                Accessible.description: qsTr("Нажмите среднюю или дополнительную кнопку внутри этого поля. Esc — отмена")
                Keys.onEscapePressed: { root.cancelCapture(true); event.accepted = true }
                Rectangle {
                    anchors.fill: parent
                    radius: Theme.selectRadius
                    color: Theme.bgSurface2
                    border.width: Theme.fieldBorder
                    border.color: Theme.border
                }
                Rectangle {
                    anchors.fill: parent
                    anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                    radius: Theme.focusRadius
                    color: "transparent"
                    border.width: Theme.focusWidth
                    border.color: Theme.stateFocusRing
                    visible: captureField.activeFocus
                }
                MouseArea {
                    anchors.fill: parent
                    enabled: root.localCapture
                    acceptedButtons: Qt.AllButtons
                    preventStealing: true
                    onPressed: {
                        // Forward the Qt button unchanged: mapping and validation are backend-owned.
                        root.bridge.commandMouseCapturePressed(mouse.button)
                        mouse.accepted = true
                    }
                    onReleased: {
                        root.bridge.commandMouseCaptureReleased(mouse.button, mouse.buttons)
                        mouse.accepted = true
                    }
                    onWheel: {
                        // Qt wheel is not a button press and cannot become an assignment.
                        wheel.accepted = true
                        root.bridge.commandMouseCapturePressed(Qt.NoButton)
                    }
                    onCanceled: root.cancelCapture(false)
                }
                RowLayout {
                    id: captureContent
                    x: Theme.cardRowPaddingX
                    y: Theme.cardRowPaddingY
                    width: parent.width - Theme.cardRowPaddingX * 2
                    spacing: Theme.fieldGap
                    Column {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                        spacing: Theme.fieldGap
                        Text {
                            width: parent.width
                            text: root.captureState === "pressed"
                                ? root.pendingLabel !== "" ? qsTr("%1 — отпустите её").arg(root.pendingLabel)
                                    : root.captureMessage || qsTr("Кнопка нажата — отпустите её")
                                : qsTr("Нажмите среднюю или дополнительную кнопку")
                            textFormat: Text.PlainText
                            color: Theme.fg
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontSettingSubSize
                            wrapMode: Text.WordWrap
                            renderType: Text.NativeRendering
                        }
                        Text {
                            width: parent.width
                            text: root.captureMessage || qsTr("Внутри этого поля. Esc — отмена")
                            textFormat: Text.PlainText
                            color: root.captureState === "error" ? Theme.dangerInk : Theme.fgMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontSettingSubSize
                            lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                            lineHeightMode: Text.FixedHeight
                            wrapMode: Text.WordWrap
                            renderType: Text.NativeRendering
                            Accessible.role: Accessible.StaticText
                        }
                    }
                    AvButton {
                        objectName: "commandMouseCaptureCancel"
                        text: qsTr("Отмена")
                        small: true
                        Layout.alignment: Qt.AlignVCenter
                        onClicked: root.cancelCapture(true)
                    }
                }
            }

            NoteBanner {
                width: parent.width
                visible: root.captureActive && !root.localCapture
                height: visible ? implicitHeight : 0
                variant: root.captureState === "ready" ? "warn"
                    : root.captureState === "error" ? "error" : "info"
                title: root.captureState === "ready" ? qsTr("Выбрана: %1").arg(root.pendingLabel)
                    : root.captureState === "error" ? qsTr("Не удалось назначить кнопку мыши") : qsTr("Проверяю кнопку…")
                body: root.captureMessage || (root.captureState === "ready" ? qsTr("Назначение ещё не сохранено") : "")
                AvButton {
                    objectName: "commandMouseApply"
                    text: qsTr("Использовать эту кнопку")
                    small: true
                    variant: "primary"
                    visible: root.captureState === "ready"
                    enabled: root.captureState === "ready"
                    onClicked: if (root.supported) root.bridge.applyCommandMouseButton()
                }
                AvButton {
                    text: qsTr("Отмена")
                    small: true
                    onClicked: root.cancelCapture(true)
                }
            }

            NoteBanner {
                width: parent.width
                visible: !root.captureActive && (root.status === "busy" || (root.status === "unavailable" && root.supported))
                height: visible ? implicitHeight : 0
                variant: root.status === "busy" ? "warn" : "error"
                title: root.status === "busy" ? qsTr("Кнопка мыши занята другой программой") : qsTr("Кнопка мыши недоступна — выберите другую")
                body: root.statusMessage
                AvButton {
                    text: qsTr("Повторить попытку")
                    small: true
                    enabled: root.canChoose
                    onClicked: root.selectCandidate(root.button, chooseButton)
                }
            }

            Text {
                width: parent.width
                visible: !root.captureActive && root.statusMessage !== ""
                    && root.status !== "busy" && !(root.status === "unavailable" && root.supported)
                height: visible ? implicitHeight : 0
                text: root.statusMessage
                textFormat: Text.PlainText
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                lineHeightMode: Text.FixedHeight
                wrapMode: Text.WordWrap
                renderType: Text.NativeRendering
                Accessible.role: Accessible.StaticText
            }
            Text {
                width: parent.width
                text: root.button === 2
                    ? root.mouseEnabled && root.bridge && root.bridge.commandEnabled && root.status === "ready"
                        ? qsTr("Средняя кнопка занята голосовыми командами во всех приложениях. Вставка выделенного текста и другие действия средней кнопки недоступны. Чтобы вернуть их, выключите эту опцию.")
                        : qsTr("Если включить, средняя кнопка будет занята голосовыми командами во всех приложениях. Вставка выделенного текста и другие действия средней кнопки станут недоступны.")
                    : root.mouseEnabled && root.bridge && root.bridge.commandEnabled && root.status === "ready"
                        ? qsTr("Выбранная кнопка занята голосовыми командами во всех приложениях. Её обычное действие, например переход назад или вперёд, недоступно. Чтобы вернуть его, выключите эту опцию.")
                        : qsTr("Если включить, выбранная кнопка будет занята голосовыми командами во всех приложениях. Её обычное действие, например переход назад или вперёд, станет недоступно.")
                textFormat: Text.PlainText
                color: Theme.warningInk
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                lineHeightMode: Text.FixedHeight
                wrapMode: Text.WordWrap
                renderType: Text.NativeRendering
            }
        }
    }
}
