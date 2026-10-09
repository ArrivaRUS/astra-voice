// F17: один редактор командной клавиши для мастера и «Продвинутых».
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15
import ".."

Column {
    id: root
    property var bridge: null
    property bool wizard: false
    property string rowLabel: qsTr("Команда помощнику")
    property bool editingEnabled: true
    signal keyboardModeRequested()
    readonly property string keyLabel: root.bridge ? (root.bridge.commandHotkey === "Super_L" ? qsTr("Левая Win")
        : root.bridge.commandHotkey === "Super_R" ? qsTr("Правая Win") : root.bridge.commandHotkey) : qsTr("Левая Win")
    readonly property bool installed: bridge ? bridge.commandInstalled : false
    width: parent ? parent.width : 580
    spacing: Theme.fieldGap

    function escPressed() { return commandCapture.escPressed() }
    function chooseWin(key) { if (bridge && editingEnabled) bridge.commandHotkey = key }

    SettingRow {
        width: parent.width
        divider: false
        label: root.rowLabel
        sub: root.bridge ? root.bridge.commandStatus : qsTr("Проверяю Astra Cowork…")
        showHint: false
        rowEnabled: root.installed && root.editingEnabled

        KeyChip {
            text: root.keyLabel
            Accessible.name: qsTr("Клавиша команды, %1").arg(root.keyLabel)
            Layout.alignment: Qt.AlignVCenter
        }
        AvButton {
            text: qsTr("Выбрать другую")
            small: true
            enabled: root.installed && root.editingEnabled
            Accessible.name: qsTr("Выбрать клавишу команды")
            onClicked: if (root.bridge) root.bridge.beginCommandCapture()
        }
    }

    // SettingRow owns its padding; only the body shares the card content inset.
    Item {
        width: parent.width
        visible: root.installed || !root.wizard || commandCapture.visible
        implicitHeight: content.height + Theme.cardRowPaddingY
        height: visible ? implicitHeight : 0

        Column {
            id: content
            x: Theme.cardRowPaddingX
            width: parent.width - Theme.cardRowPaddingX * 2
            spacing: Theme.fieldGap

            Column {
                width: parent.width
                visible: root.installed
                spacing: 0
                Text {
                    width: parent.width
                    text: root.bridge && root.bridge.commandHotkey !== "Super_L" && root.bridge.commandHotkey !== "Super_R"
                        ? qsTr("Эта клавиша запускает голосовую команду в Astra Cowork.")
                        : qsTr("Win используется для записи вместо меню приложений. Сочетания с Win работают до начала записи; во время записи с удерживаемой Win они недоступны. Рекомендуем «Удерживать».")
                    visible: root.installed
                    height: visible ? implicitHeight : 0
                    textFormat: Text.PlainText
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSettingSubSize
                    lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                    lineHeightMode: Text.FixedHeight
                    color: Theme.fgMuted
                    wrapMode: Text.WordWrap
                    renderType: Text.NativeRendering
                }

                AbstractButton {
                    visible: root.installed && !root.wizard
                    height: visible ? implicitHeight : 0
                    width: parent.width
                    padding: 0
                    implicitHeight: modeText.implicitHeight
                    Accessible.name: modeText.text
                    contentItem: Text {
                        id: modeText
                        text: root.bridge && root.bridge.hotkeyMode === "toggle"
                            ? qsTr("Режим клавиатуры: Нажать-нажать. Изменить в «Общих»")
                            : qsTr("Режим клавиатуры: Удерживать. Изменить в «Общих»")
                        textFormat: Text.PlainText
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSettingSubSize
                        lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                        lineHeightMode: Text.FixedHeight
                        color: Theme.primary
                        wrapMode: Text.WordWrap
                        renderType: Text.NativeRendering
                    }
                    background: Rectangle {
                        anchors.fill: parent
                        anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                        color: "transparent"
                        radius: Theme.focusRadius
                        border.width: Theme.focusWidth
                        border.color: Theme.stateFocusRing
                        visible: parent.visualFocus
                    }
                    onClicked: root.keyboardModeRequested()
                }
            }

            RowLayout {
                visible: root.installed
                height: visible ? implicitHeight : 0
                spacing: Theme.fieldGap
                AvButton { text: qsTr("Левая Win"); small: true; variant: "secondary"; enabled: root.editingEnabled; onClicked: root.chooseWin("Super_L") }
                AvButton { text: qsTr("Правая Win"); small: true; variant: "secondary"; enabled: root.editingEnabled; onClicked: root.chooseWin("Super_R") }
                AvButton { text: qsTr("Проверить снова"); small: true; variant: "ghost"; onClicked: if (root.bridge) root.bridge.refreshCommandStatus() }
            }

            Text {
                width: parent.width
                visible: !root.installed && !root.wizard && root.bridge !== null
                height: visible ? implicitHeight : 0
                text: qsTr("Голосовые команды доступны после установки Astra Cowork")
                textFormat: Text.PlainText
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                wrapMode: Text.WordWrap
                renderType: Text.NativeRendering
            }

            AvButton {
                visible: !root.installed && !root.wizard
                text: qsTr("Проверить снова")
                small: true
                variant: "ghost"
                onClicked: if (root.bridge) root.bridge.refreshCommandStatus()
            }

            CaptureField {
                id: commandCapture
                width: parent.width
                visible: state7 !== "idle"
                height: visible ? implicitHeight : 0
                showIdleRow: false
                state7: root.bridge && root.bridge.captureRole === "command" ? root.bridge.captureState : "idle"
                hotkey: root.bridge ? root.bridge.commandHotkey : "Super_L"
                captureMessage: root.bridge ? root.bridge.captureMessage : ""
                pendingCombo: root.bridge ? root.bridge.pendingCombo : ""
                freeCandidates: root.bridge ? root.bridge.freeCandidates : []
                roleLabel: root.rowLabel
                onChangeRequested: if (root.bridge) root.bridge.beginCommandCapture()
                onChooseAnotherRequested: if (root.bridge) root.bridge.beginCommandCapture()
                onRetryRequested: if (root.bridge) root.bridge.beginCommandCapture()
                onCancelRequested: if (root.bridge) root.bridge.cancelCapture()
                onComboCaptured: if (root.bridge) root.bridge.endCapture(combo)
                onKeepRequested: if (root.bridge) root.bridge.keepCombo()
                onState7Changed: {
                    if ((state7 === "conflict" || state7 === "duplicate" || state7 === "not-grabbed") && root.bridge)
                        root.bridge.refreshCandidates()
                }
            }
        }
    }
}
