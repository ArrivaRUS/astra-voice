// design/spec.md §4.5, §7: поле захвата и семь состояний назначения.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Item {
    id: root

    property string state7: "idle"
    property string hotkey: qsTr("Ctrl + Space")
    property string conflictOwner: ""
    property string captureMessage: ""
    property string pendingCombo: ""
    property var freeCandidates: []

    readonly property string displayedCombo: pendingCombo !== "" ? pendingCombo : hotkey
    readonly property string candidatesHint: freeCandidates.length > 0
        ? "\n" + qsTr("Свободны: %1").arg(freeCandidates.join(", ")) : ""

    signal changeRequested()
    signal cancelRequested()
    signal keepRequested()
    signal comboCaptured(string combo)
    signal chooseAnotherRequested()
    signal toggleModeRequested()
    signal retryRequested()

    readonly property bool captureActive: state7 === "capturing" || state7 === "captured"
    readonly property bool hasResult: state7 === "success" || state7 === "conflict"
        || state7 === "duplicate" || state7 === "not-grabbed"

    function escPressed() {
        if (!captureActive)
            return false
        cancelRequested()
        return true
    }

    // Формат моста задан в src/astra_voice/platform/x11.py::parse_combo:
    // модификаторы и имя keysym разделяются «+» без пробелов.
    // Для v0.1 решено фиксировать комбинацию по нажатию (мост endCapture сам переводит в captured и сразу пробует); набор клавиш ограничен буквами, цифрами, Space и F1–F12 — имена keysym для прочих клавиш из QML безопасно не собрать.
    function buildCombo(event) {
        var key = ""
        if (event.key === Qt.Key_Space)
            key = "Space"
        else if (event.key >= Qt.Key_F1 && event.key <= Qt.Key_F12)
            key = "F" + (event.key - Qt.Key_F1 + 1)
        else if (event.key >= Qt.Key_A && event.key <= Qt.Key_Z)
            key = String.fromCharCode(event.key - Qt.Key_A + 65)
        else if (event.key >= Qt.Key_0 && event.key <= Qt.Key_9)
            key = String(event.key - Qt.Key_0)
        else
            return ""

        var parts = []
        if (event.modifiers & Qt.ControlModifier)
            parts.push("Ctrl")
        if (event.modifiers & Qt.ShiftModifier)
            parts.push("Shift")
        if (event.modifiers & Qt.AltModifier)
            parts.push("Alt")
        if (event.modifiers & Qt.MetaModifier)
            parts.push("Super")
        parts.push(key)
        return parts.join("+")
    }

    function updateCaptureFocus() {
        if (captureActive)
            forceActiveFocus()
    }

    implicitWidth: Math.max(idleRow.implicitWidth, captureLine.implicitWidth + Theme.fieldPaddingX * 2)
    implicitHeight: content.implicitHeight
    width: parent ? parent.width : implicitWidth
    height: implicitHeight
    focus: captureActive

    // Loader должен завершить создание поля до установки активного фокуса.
    onCaptureActiveChanged: Qt.callLater(root.updateCaptureFocus)
    Component.onCompleted: Qt.callLater(root.updateCaptureFocus)

    Keys.onPressed: {
        if (event.isAutoRepeat) {
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_Control || event.key === Qt.Key_Shift
                || event.key === Qt.Key_Alt || event.key === Qt.Key_Meta
                || event.key === Qt.Key_Super_L || event.key === Qt.Key_Super_R
                || event.key === Qt.Key_AltGr) {
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_Escape) {
            event.accepted = root.escPressed()
            return
        }
        if (!root.captureActive)
            return
        var combo = root.buildCombo(event)
        if (combo !== "")
            root.comboCaptured(combo)
        event.accepted = true
    }

    Column {
        id: content
        width: root.width
        spacing: 8 // design/spec.md §7: ряд и отступ перед баннером.

        RowLayout {
            id: idleRow
            visible: root.state7 === "idle" || root.hasResult
            height: visible ? implicitHeight : 0
            spacing: 8 // design/spec.md §7: чип + «Изменить».

            KeyChip {
                text: root.hotkey
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                text: qsTr("Изменить")
                small: true
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.changeRequested()
            }
        }

        Rectangle {
            id: field
            width: parent.width
            implicitHeight: captureLine.implicitHeight + Theme.fieldPaddingY * 2
            height: visible ? implicitHeight : 0
            visible: root.captureActive
            radius: Theme.fieldRadius
            border.width: Theme.fieldBorder
            border.color: Theme.primary
            color: Theme.bgSurface
            antialiasing: true

            // CSS box-shadow лежит снаружи поля, не участвуя в раскладке.
            Rectangle {
                anchors.fill: parent
                anchors.margins: -2 // design/spec.md §4.5: кольцо 0 0 0 2px.
                radius: Theme.fieldRadius + 2 // design/spec.md §4.5: внешний радиус кольца.
                color: "transparent"
                border.width: 2 // design/spec.md §4.5: толщина кольца.
                border.color: Theme.primaryBg
                antialiasing: true
            }

            RowLayout {
                id: captureLine
                x: Theme.fieldPaddingX
                y: Theme.fieldPaddingY
                width: Math.max(0, field.width - Theme.fieldPaddingX * 2)
                spacing: Theme.fieldGap

                Text {
                    text: root.state7 === "captured" ? root.displayedCombo : qsTr("Нажмите комбинацию…")
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.fontHotkeyCaptureSize
                    color: Theme.fg
                    textFormat: Text.PlainText
                    renderType: Text.NativeRendering
                    Layout.alignment: Qt.AlignVCenter
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: root.state7 === "captured" ? qsTr("Отпустите клавиши") : qsTr("Esc — отмена")
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontCaptionSize
                    color: Theme.fgMuted
                    renderType: Text.NativeRendering
                    Layout.alignment: Qt.AlignVCenter
                }
            }
        }

        NoteBanner {
            visible: root.state7 === "success"
            height: visible ? implicitHeight : 0
            variant: "ok"
            iconName: "check"
            title: qsTr("Комбинация назначена")
            body: qsTr("Теперь диктовка работает по %1.").arg(root.hotkey)
        }

        NoteBanner {
            visible: root.state7 === "conflict"
            height: visible ? implicitHeight : 0
            variant: "warn"
            iconName: "alert"
            title: qsTr("Комбинация %1 занята в KDE").arg(root.displayedCombo)
            body: (root.captureMessage !== "" ? root.captureMessage
                : root.conflictOwner.trim() !== ""
                    ? qsTr("%1 уже назначена на действие «%2». Оставить её можно, но диктовка может не сработать — система заберёт нажатие себе.").arg(root.displayedCombo).arg(root.conflictOwner)
                    : qsTr("%1 уже занята другой программой. Оставить её можно, но диктовка может не сработать — система заберёт нажатие себе.").arg(root.displayedCombo))
                + root.candidatesHint

            AvButton {
                text: qsTr("Оставить %1").arg(root.displayedCombo)
                variant: "secondary"
                onClicked: root.keepRequested()
            }

            AvButton {
                text: qsTr("Выбрать другую")
                variant: "primary"
                onClicked: root.chooseAnotherRequested()
            }
        }

        NoteBanner {
            visible: root.state7 === "duplicate"
            height: visible ? implicitHeight : 0
            variant: "warn"
            iconName: "alert"
            title: qsTr("Эта комбинация уже назначена: %1").arg(root.displayedCombo)
            body: (root.captureMessage !== "" ? root.captureMessage
                : qsTr("Она занята другой настройкой Astra Voice. Выберите другое сочетание или измените режим."))
                + root.candidatesHint

            AvButton {
                text: qsTr("Выбрать другую")
                variant: "primary"
                onClicked: root.chooseAnotherRequested()
            }

            AvButton {
                text: qsTr("Открыть „нажать-нажать“")
                variant: "ghost"
                onClicked: root.toggleModeRequested()
            }
        }

        NoteBanner {
            visible: root.state7 === "not-grabbed"
            height: visible ? implicitHeight : 0
            variant: "error"
            iconName: "alert"
            title: qsTr("Горячая клавиша не захвачена")
            body: (root.captureMessage !== "" ? root.captureMessage
                : qsTr("Другая программа держит это сочетание. Диктовка не заработает, пока не выбрано свободное."))
                + root.candidatesHint

            AvButton {
                text: qsTr("Выбрать другую")
                variant: "primary"
                onClicked: root.chooseAnotherRequested()
            }

            AvButton {
                text: qsTr("Повторить попытку")
                variant: "secondary"
                onClicked: root.retryRequested()
            }
        }
    }
}
