// F17: один редактор командной клавиши для мастера и «Общих».
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Column {
    id: root
    property var bridge: null
    property bool wizard: false
    readonly property bool installed: bridge ? bridge.commandInstalled : false
    width: parent ? parent.width : 580
    spacing: 8

    function escPressed() { return commandCapture.escPressed() }
    function chooseWin(key) { if (bridge) bridge.commandHotkey = key }

    SettingRow {
        width: parent.width
        divider: false
        label: qsTr("Команда помощнику")
        sub: root.bridge ? root.bridge.commandStatus : qsTr("Astra Cowork не установлен")

        KeyChip {
            text: root.bridge ? (root.bridge.commandHotkey === "Super_L" ? qsTr("Левая Win")
                : root.bridge.commandHotkey === "Super_R" ? qsTr("Правая Win") : root.bridge.commandHotkey) : qsTr("Левая Win")
            Layout.alignment: Qt.AlignVCenter
        }
        AvButton {
            text: qsTr("Выбрать другую")
            small: true
            enabled: root.installed
            onClicked: if (root.bridge) root.bridge.beginCommandCapture()
        }
    }

    Text {
        width: parent.width
        text: qsTr("Win запускает запись команды вместо меню приложений. Для команд рекомендуем «Удерживать»: пока Win зажата, сочетания с ней не работают. После выхода меню снова доступно.")
        visible: root.installed
        height: visible ? implicitHeight : 0
        textFormat: Text.PlainText
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSettingSubSize
        color: Theme.fgMuted
        wrapMode: Text.WordWrap
        renderType: Text.NativeRendering
    }

    RowLayout {
        visible: root.installed
        height: visible ? implicitHeight : 0
        spacing: 8
        AvButton { text: qsTr("Левая Win"); small: true; variant: "secondary"; onClicked: root.chooseWin("Super_L") }
        AvButton { text: qsTr("Правая Win"); small: true; variant: "secondary"; onClicked: root.chooseWin("Super_R") }
        AvButton { text: qsTr("Проверить снова"); small: true; variant: "ghost"; onClicked: if (root.bridge) root.bridge.refreshCommandStatus() }
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
        roleLabel: qsTr("Команда помощнику")
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
